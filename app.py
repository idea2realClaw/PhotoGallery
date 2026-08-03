"""
PhotoGallery — 本地 WebUI 照片画廊生成器（含局域网共享）

流程：
  * 首页 "欢迎使用PhotoGallery" + "选择目录" 按钮（浏览器目录选择器）。
  * 用户选择目录后，前端把图片发到后台（或填入服务器可访问的绝对路径）。
  * 后台扫描/接收图片：
      - 为每张生成缩略图文件，写入 thumbs/ 目录（网格显示用）；
      - 灯箱大图使用「原图」（全分辨率），用相对路径直接引用原始文件（零复制）。
  * 生成完成后后台自动用系统默认浏览器打开 gallery.html。
  * gallery.html 内含 "返回主页" 按钮，点击回到 WebUI 首页；
    点击任意缩略图弹出大图灯箱（先看缩略图，放大时才懒加载原图；滚轮缩放、左键拖动平移、
    右键保存原图、上一张/下一张、底部显示完整文件名）。

网络共享（供局域网其他用户访问）：
  * 应用启动即开启一个 FTP 服务（默认 0.0.0.0:2121，匿名只读），
    共享「当前选中的目录」，让其他用户用 FTP 客户端取走原图。
  * 生成 gallery.html 后，后台自动把该画廊通过内置 HTTP 的 /share 路由
    共享给网络其他客户，地址形如 http://<本机IP>:2026/share/gallery.html
    （内部相对引用的原图/缩略图也都经 /share 提供）。

运行：
  python app.py   ->  WebUI/FTP/共享 分别监听（见各端口常量）
后台会持续把「前端事件」与「后台工作进展」打印到控制台，
并可通过 /api/logstream (SSE) 实时推送到前端的日志面板。
"""

import os
import io
import re
import sys
import json
import hashlib
import base64
import queue
import logging
import socket
import threading
import webbrowser
import ftplib
import zipfile
import tempfile
import concurrent.futures
from html import escape as _html_escape
from urllib.parse import quote as _url_quote
from pathlib import Path

from flask import (
    Flask,
    request,
    jsonify,
    Response,
    send_from_directory,
)
from PIL import Image
import time
import numpy as np

try:
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer
    _HAS_FTP = True
except Exception:  # pragma: no cover
    _HAS_FTP = False

BASE_DIR = Path(__file__).resolve().parent
GALLERY_FILE = BASE_DIR / "gallery.html"        # 默认生成位置（无法写入源目录时的回退）
THUMBS_DIR = BASE_DIR / "thumbs"                 # 缩略图目录（回退模式用）
GALLERY_ASSETS_DIR = BASE_DIR / "gallery_assets"  # 上传/无写权限时的回退工作目录
FOLD_JSON = BASE_DIR / "fold.json"               # 记住最近扫描的目录，供下次启动/刷新 WebUI 预填
HOME_URL = "http://127.0.0.1:2026/"

# ---- 人脸索引相关配置 ----
FACES_DIR = BASE_DIR / "faces"               # 人脸裁剪图目录（由人脸索引生成，含隐私，已 gitignore）
FACES_JSON = BASE_DIR / "faces.json"         # 人脸数据库（聚类结果 + 标签，已 gitignore）
FACE_DET_SIZE = (640, 640)                   # 检测器输入尺寸
FACE_MATCH_THRESH = 0.4                       # ArcFace 余弦相似度聚类阈值（同人阈值，越大越严格）

# ---- 网络共享相关配置 ----
FTP_PORT = 2121          # FTP 服务端口（匿名只读，共享选中目录）
SHARE_HTTP_PORT = 2026   # HTTP 共享复用 WebUI 端口（/share 路由）
SHARED_DIR = None        # 当前共享目录（generate 时更新）
LOCAL_IP = ""            # 启动时获取的本机局域网 IP（供共享地址展示）
ftp_authorizer = None
ftp_server = None

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp",
    ".webp", ".tif", ".tiff", ".heic", ".heif",
}

MIME_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
    ".tif": "image/tiff", ".tiff": "image/tiff",
    ".heic": "image/heic", ".heif": "image/heif",
}


# --------------------------------------------------------------------------- #
# 日志系统：同时输出到控制台(SSE 也复用同一份队列)
# --------------------------------------------------------------------------- #
log_queue: "queue.Queue[str]" = queue.Queue(maxsize=2000)


class _QueueHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            log_queue.put_nowait(self.format(record))
        except queue.Full:
            pass


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    qh = _QueueHandler()
    qh.setFormatter(fmt)
    root.addHandler(qh)


app = Flask(__name__)
log = logging.getLogger("photogallery")


@app.after_request
def _add_cors(resp):
    """本地工具：允许跨域（含 file:// 打开的 gallery.html）向 /api/event 上报日志。"""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


# --------------------------------------------------------------------------- #
# 最近扫描目录的记忆（fold.json）
# --------------------------------------------------------------------------- #
def save_last_folder(path: str) -> None:
    """把最近一次成功扫描的目录路径写入 fold.json，供下次启动/刷新 WebUI 预填文本框。"""
    try:
        FOLD_JSON.write_text(json.dumps({"folder": path}, ensure_ascii=False), encoding="utf-8")
        log.info("BACKEND: 已把最近扫描目录写入 fold.json：%s", path)
    except Exception as exc:
        log.warning("BACKEND: 写入 fold.json 失败：%s", exc)


def load_last_folder() -> str:
    """读取 fold.json 中保存的最近目录；文件不存在/损坏/值非字符串则返回空串。"""
    try:
        if FOLD_JSON.exists():
            data = json.loads(FOLD_JSON.read_text(encoding="utf-8"))
            folder = data.get("folder", "")
            if isinstance(folder, str):
                return folder
    except Exception:
        pass
    return ""


# --------------------------------------------------------------------------- #
# 图片处理
# --------------------------------------------------------------------------- #
def save_original_file(data: bytes, dest_dir: Path, orig_name: str) -> str:
    """把原图字节原样写入 dest_dir/orig_name，返回相对文件名（供灯箱大图使用）。
    仅在「无法引用原始路径」的模式下调用（如浏览器上传 / 源目录无写权限）。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / orig_name
    out.write_bytes(data)
    return orig_name


def _rel_hash(rel_name: str) -> str:
    """相对路径的短哈希，让缩略图/副本文件名稳定且唯一（不随扫描顺序变化）。"""
    return hashlib.md5(rel_name.encode("utf-8")).hexdigest()[:8]


def orig_filename(rel_name: str) -> str:
    """按相对路径生成稳定的原图副本文件名（保留原扩展名），不依赖扫描序号。"""
    p = Path(rel_name)
    stem = re.sub(r'[:*?"<>|]', "_", p.stem.replace("/", "__").replace("\\", "__"))
    ext = p.suffix or ".jpg"
    return f"{_rel_hash(rel_name)}_{stem}{ext}"


def save_thumb_file(data: bytes, dest_dir: Path, thumb_name: str) -> str:
    """把图片字节缩放成缩略图，写入 dest_dir/thumb_name（dest_dir 即 <根>/thumbs），
    返回缩略图文件名（供网格显示）。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        img.thumbnail((480, 480))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=82)
    out = dest_dir / thumb_name
    out.write_bytes(buf.getvalue())
    return thumb_name


def thumb_filename(rel_name: str) -> str:
    """按相对路径生成稳定的缩略图文件名（不依赖扫描序号），便于增量复用。"""
    s = rel_name.replace("/", "__").replace("\\", "__")
    s = re.sub(r'[:*?"<>|]', "_", s)
    return f"{_rel_hash(rel_name)}_{s}.jpg"


def source_changed(src: Path, out: Path) -> bool:
    """判断源文件是否比已生成的产物（缩略图/副本）更新。
    产物不存在、为空、或源文件 mtime 晚于产物，则视为需要重新生成。"""
    try:
        if not out.exists():
            return True
        out_st = out.stat()
        if out_st.st_size == 0:
            return True
        return src.stat().st_mtime > out_st.st_mtime
    except OSError:
        return True


# 浏览器可直接渲染的原图格式；其余需要生成可预览的 JPEG 预览图
BROWSER_NATIVE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def is_browser_native(path: Path) -> bool:
    """判断该图片格式浏览器能否直接渲染（能则灯箱直接显示原图；否则用预览图）。"""
    return path.suffix.lower() in BROWSER_NATIVE_EXTS


def view_filename(rel_name: str) -> str:
    """浏览器不可直接显示格式（HEIC/TIFF 等）的高清预览图文件名（稳定、增量复用）。"""
    s = rel_name.replace("/", "__").replace("\\", "__")
    s = re.sub(r'[:*?"<>|]', "_", s)
    return f"{_rel_hash(rel_name)}_{s}.view.jpg"


def save_preview_file(data: bytes, dest_dir: Path, preview_name: str) -> str:
    """把不可直接显示的原始格式（HEIC/TIFF 等）转成高清 JPEG 预览图，供灯箱展示。
    上限 2000px 长边、质量 90，避免超大原图拖慢浏览器渲染。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        img.thumbnail((2000, 2000))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90)
    out = dest_dir / preview_name
    out.write_bytes(buf.getvalue())
    return preview_name


def scan_images_on_disk(root: Path):
    """在服务器可访问的目录里递归收集图片文件（按路径排序）。
    会跳过自身生成的 thumbs/ 目录与 gallery.html，避免重复收录缩略图/上一次生成的画廊。"""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        # 剪枝：不进入自己生成的 thumbs 目录（里面是缩略图/预览图，也是 jpg，会被误收录）
        if "thumbs" in dirnames:
            dirnames.remove("thumbs")
        for fn in filenames:
            if fn == "gallery.html":
                continue
            if Path(fn).suffix.lower() in IMAGE_EXTS:
                found.append(Path(dirpath) / fn)
    found.sort(key=lambda p: str(p).lower())
    return found


# --------------------------------------------------------------------------- #
# 生成 gallery.html
# --------------------------------------------------------------------------- #
def build_gallery_html(dir_name: str, photos: list) -> str:
    """
    photos: [{"name": str, "thumb": str(相对缩略图路径), "orig": str(相对原图路径)}, ...]
    返回自包含的 HTML 字符串（缩略图与原图均用相对路径引用文件，可用 file:// 直接打开）。
    含灯箱：点击缩略图先显示缩略图（快速），滚轮放大时再懒加载原图全分辨率；
    放大后左键拖动平移、右键保存原图、上一张/下一张、Esc 关闭，底部显示完整文件名。
    """
    cards = []
    for i, p in enumerate(photos):
        safe_name = (p["name"] or "").replace('"', "&quot;")
        cards.append(
            f'      <figure class="cell">\n'
            f'        <button class="thumb" type="button" data-index="{i}" '
            f'aria-label="查看 {safe_name}">\n'
            f'          <img loading="lazy" src="{p["thumb"]}" alt="{safe_name}" />\n'
            f'        </button>\n'
            f'        <figcaption>{safe_name}</figcaption>\n'
            f'      </figure>'
        )
    grid = "\n".join(cards) if cards else '      <p class="empty">该目录没有发现图片。</p>'

    # 给前端灯箱用的图片数据：src=下载用真实原图，view=显示用原图(浏览器不支持格式则为预览图)，
    # name=相对文件名(下载用)，full=含完整目录的绝对路径(展示用)；转义 </ 防止提前闭合 script
    photos_payload = [{"src": p["orig"], "view": p.get("view", p["orig"]),
                       "thumb": p["thumb"], "name": p["name"] or "",
                       "full": p.get("full", p["name"] or "")} for p in photos]
    photos_json = json.dumps(photos_payload, ensure_ascii=False).replace("</", "<\\/")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>PhotoGallery · {dir_name}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
    "Microsoft YaHei", sans-serif; background: #f5f6f8; color: #1f2430; min-height: 100vh; }}
  header {{ position: sticky; top: 0; z-index: 10; display: flex; align-items: center;
    justify-content: space-between; gap: 16px; padding: 14px 22px; background: #fff;
    border-bottom: 1px solid #e6e8ee; }}
  .title {{ font-weight: 700; font-size: 18px; }}
  .title small {{ color: #8a90a2; font-weight: 500; margin-left: 8px; font-size: 13px; }}
  .home-btn {{ border: 1px solid #3b6cff; background: #3b6cff; color: #fff;
    padding: 9px 16px; border-radius: 9px; font-size: 14px; cursor: pointer;
    text-decoration: none; display: inline-block; }}
  .home-btn:hover {{ filter: brightness(1.06); }}
  main {{ padding: 24px; }}
  .gridhint {{ color: #8a90a2; font-size: 13px; margin-bottom: 14px; }}
  .grid {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); }}
  .cell {{ background: #fff; border: 1px solid #e6e8ee; border-radius: 12px; overflow: hidden;
    box-shadow: 0 6px 20px rgba(20,30,60,.08); }}
  .thumb {{ display: block; width: 100%; padding: 0; border: none; background: #eef0f5;
    cursor: pointer; }}
  .thumb img {{ width: 100%; aspect-ratio: 1/1; object-fit: cover; display: block;
    background: #eef0f5; transition: transform .12s; }}
  .thumb:hover img {{ transform: scale(1.03); }}
  .cell figcaption {{ padding: 8px 10px; font-size: 12px; color: #8a90a2;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .empty {{ color: #8a90a2; text-align: center; padding: 80px 20px; }}

  /* 灯箱 */
  .lightbox {{ position: fixed; inset: 0; background: rgba(8,10,18,.94);
    z-index: 100; display: flex; align-items: center; justify-content: center; }}
  .lightbox[hidden] {{ display: none; }}
  #lbStage {{ position: absolute; inset: 0; display: flex; align-items: center;
    justify-content: center; overflow: hidden; }}
  .lightbox img {{ max-width: 92vw; max-height: 84vh; object-fit: contain;
    border-radius: 6px; box-shadow: 0 10px 40px rgba(0,0,0,.5);
    transform-origin: center center; transition: transform .08s ease-out; }}
  .lb-close {{ position: absolute; top: 18px; right: 22px; z-index: 120; width: 42px; height: 42px;
    border-radius: 50%; border: none; background: rgba(255,255,255,.15); color: #fff;
    font-size: 24px; cursor: pointer; }}
  .lb-close:hover {{ background: rgba(255,255,255,.28); }}
  .lb-nav {{ position: absolute; top: 50%; transform: translateY(-50%); z-index: 120;
    width: 54px; height: 54px; border-radius: 50%; border: none; background: rgba(255,255,255,.15);
    color: #fff; font-size: 32px; cursor: pointer; }}
  .lb-nav:hover {{ background: rgba(255,255,255,.28); }}
  .lb-prev {{ left: 20px; }}
  .lb-next {{ right: 20px; }}
  .lb-name {{ position: absolute; bottom: 18px; left: 0; right: 0; z-index: 120;
    text-align: center; color: #e8ebf2; font-size: 14px; padding: 0 20px; word-break: break-all; }}
  .lb-info {{ position: absolute; bottom: 18px; right: 22px; z-index: 120; color: #cfd4e2;
    font-size: 13px; background: rgba(0,0,0,.35); padding: 3px 10px; border-radius: 8px; }}

  /* 右键加载完原图后弹出的「下载原图」菜单（自定义，浏览器不允许代码触发原生菜单） */
  .lb-menu {{ position: fixed; z-index: 200; background: #fff; color: #1f2430;
    border: 1px solid #e6e8ee; border-radius: 10px; box-shadow: 0 8px 28px rgba(0,0,0,.28);
    padding: 6px; min-width: 180px; }}
  .lb-menu[hidden] {{ display: none; }}
  .lb-menu button {{ width: 100%; text-align: left; border: none; background: transparent;
    padding: 10px 12px; border-radius: 7px; font-size: 14px; cursor: pointer; color: #1f2430; }}
  .lb-menu button:hover {{ background: #eef2ff; }}
</style>
</head>
<body>
  <header>
    <div class="title">PhotoGallery<small>{dir_name} · {len(photos)} 张照片</small></div>
    <a class="home-btn" href="{HOME_URL}">← 返回主页</a>
  </header>
  <main>
    <p class="gridhint">点击下方任意缩略图可查看（先看缩略图，放大时才加载原图；右键先加载原图再弹「下载原图」菜单；← → 切换、Esc 关闭；文件名含完整目录路径）。</p>
    <div class="grid">
{grid}
    </div>
  </main>

  <div class="lightbox" id="lightbox" hidden>
    <div id="lbStage"><img id="lbImg" src="" alt="" draggable="false" /></div>
    <button class="lb-close" id="lbClose" aria-label="关闭">×</button>
    <button class="lb-nav lb-prev" id="lbPrev" aria-label="上一张">‹</button>
    <button class="lb-nav lb-next" id="lbNext" aria-label="下一张">›</button>
    <div class="lb-name" id="lbName"></div>
    <div class="lb-info" id="lbInfo">100%</div>
    <div class="lb-menu" id="lbMenu" hidden>
      <button type="button" id="lbMenuDownload">⬇️ 下载原图</button>
    </div>
  </div>

  <script>
    const PHOTOS = {photos_json};

    // 前端事件上报（同源/跨域均可；file:// 打开时若被 CORS 拦截则静默忽略）
    function reportEvent(message) {{
      try {{
        fetch("/api/event", {{ method: "POST", headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ message }}) }});
      }} catch (_) {{}}
    }}

    const lb = document.getElementById('lightbox');
    const lbStage = document.getElementById('lbStage');
    const lbImg = document.getElementById('lbImg');
    const lbName = document.getElementById('lbName');
    const lbInfo = document.getElementById('lbInfo');
    const lbMenu = document.getElementById('lbMenu');
    const lbMenuDownload = document.getElementById('lbMenuDownload');
    let cur = 0, scale = 1, tx = 0, ty = 0, panning = false, startX = 0, startY = 0,
        suppressClose = false, usingView = false, origLoading = false;

    function applyTransform() {{
      lbImg.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + scale + ')';
      lbInfo.textContent = Math.round(scale * 100) + '%';
      lbImg.style.cursor = scale > 1 ? (panning ? 'grabbing' : 'grab') : 'default';
    }}
    function show(i) {{
      const n = PHOTOS.length;
      cur = ((i % n) + n) % n;
      usingView = false; origLoading = false;
      hideMenu();
      lbImg.src = PHOTOS[cur].thumb;          // 先看缩略图（快速）
      lbName.textContent = PHOTOS[cur].full;  // 文件名带完整目录
      scale = 1; tx = 0; ty = 0; applyTransform();
    }}
    function openLB(i) {{ lb.hidden = false; show(i); reportEvent("打开灯箱：" + PHOTOS[i].full); }}
    function closeLB() {{ hideMenu(); lb.hidden = true; reportEvent("关闭灯箱"); }}

    // 加载「显示用原图」：浏览器原生格式=真实原图；HEIC/TIFF 等=高清预览图。
    // 加载成功才切换画面，失败则保留缩略图（不破图）并提示格式不支持。
    function loadView(onready) {{
      if (usingView) {{ onready && onready(); return; }}
      const url = PHOTOS[cur].view;
      if (!url) {{ onready && onready(); return; }}
      origLoading = true;
      lbInfo.textContent = Math.round(scale * 100) + '% · 原图加载中…';
      const loader = new Image();
      loader.onload = function () {{
        lbImg.src = url; usingView = true; origLoading = false; applyTransform();
        onready && onready();
      }};
      loader.onerror = function () {{
        origLoading = false;
        lbInfo.textContent = Math.round(scale * 100) + '% · 原图不可用（格式浏览器不支持）';
      }};
      loader.src = url;
    }}

    document.querySelectorAll('.thumb').forEach(function (b) {{
      b.addEventListener('click', function () {{
        const idx = parseInt(b.dataset.index, 10);
        openLB(idx);
      }});
    }});
    document.getElementById('lbClose').addEventListener('click', closeLB);
    document.getElementById('lbPrev').addEventListener('click', function (e) {{
      e.stopPropagation(); const n = PHOTOS.length; const p = PHOTOS[((cur - 1) % n + n) % n];
      reportEvent("灯箱上一张：" + p.full); show(cur - 1);
    }});
    document.getElementById('lbNext').addEventListener('click', function (e) {{
      e.stopPropagation(); const n = PHOTOS.length; const p = PHOTOS[((cur + 1) % n) % n];
      reportEvent("灯箱下一张：" + p.full); show(cur + 1);
    }});
    lb.addEventListener('click', function (e) {{ if ((e.target === lb || e.target === lbStage) && !suppressClose) closeLB(); }});

    // 滚轮缩放：上滚放大、下滚缩小；最小为初始大小。放大时才懒加载显示用原图。
    let lastZoomLog = 0;
    lbStage.addEventListener('wheel', function (e) {{
      e.preventDefault();
      if (e.deltaY < 0) scale *= 1.15; else scale /= 1.15;
      if (scale < 1) {{ scale = 1; tx = 0; ty = 0; }}
      if (scale > 1 && !usingView) loadView();
      applyTransform();
      const now = Date.now();
      if (now - lastZoomLog > 400) {{
        lastZoomLog = now;
        reportEvent("灯箱缩放：" + Math.round(scale * 100) + "%" + (usingView ? "" : "（加载原图中）"));
      }}
    }}, {{ passive: false }});

    // 遮罩区域屏蔽右键菜单（避免误触）；图片上的右键见下方 lbImg 监听
    lbStage.addEventListener('contextmenu', function (e) {{ if (e.target !== lbImg) e.preventDefault(); }});
    // 图片右键：已是显示用原图 → 放行原生「保存图片」菜单（存的就是原图）；
    // 否则先加载显示用原图，成功后再弹「下载原图」菜单（不直接下载）。
    lbImg.addEventListener('contextmenu', function (e) {{
      if (!usingView) {{
        e.preventDefault();
        reportEvent("灯箱右键：加载原图后提供下载");
        loadViewThenMenu(e.clientX, e.clientY);
      }}
    }});
    function loadViewThenMenu(x, y) {{
      if (origLoading) {{ showMenu(x, y); return; }}
      const url = PHOTOS[cur].view, name = PHOTOS[cur].name;
      const loader = new Image();
      showMenuLoading(x, y);
      loader.onload = function () {{
        lbImg.src = url; usingView = true; showMenu(x, y, name);
      }};
      loader.onerror = function () {{ hideMenu(); }};
      loader.src = url;
    }}
    function menuPos(x, y) {{
      const w = lbMenu.offsetWidth || 180, h = lbMenu.offsetHeight || 80;
      lbMenu.style.left = Math.max(8, Math.min(x, window.innerWidth - w - 8)) + 'px';
      lbMenu.style.top = Math.max(8, Math.min(y, window.innerHeight - h - 8)) + 'px';
    }}
    function showMenu(x, y, name) {{
      lbMenuDownload.textContent = '⬇️ 下载原图' + (name ? '（' + name.split('/').pop() + '）' : '');
      lbMenu.hidden = false; menuPos(x, y);
    }}
    function showMenuLoading(x, y) {{
      lbMenuDownload.textContent = '⏳ 正在加载原图…';
      lbMenu.hidden = false; menuPos(x, y);
    }}
    function hideMenu() {{ if (lbMenu) lbMenu.hidden = true; }}
    lbMenuDownload.addEventListener('click', function (e) {{
      e.stopPropagation();
      reportEvent("灯箱下载原图：" + PHOTOS[cur].full);
      downloadOriginal(PHOTOS[cur].src, PHOTOS[cur].name);
      hideMenu();
    }});
    document.addEventListener('click', function (e) {{
      if (lbMenu && !lbMenu.hidden && e.target !== lbMenuDownload) hideMenu();
    }});
    lbStage.addEventListener('wheel', function () {{ hideMenu(); }}, {{ passive: true }});
    function downloadOriginal(url, name) {{
      const a = document.createElement('a');
      a.href = url; a.download = name || 'image';
      document.body.appendChild(a); a.click(); a.remove();
    }}
    // 左键拖动平移（图片上按下且已放大后生效）
    lbStage.addEventListener('mousedown', function (e) {{
      if (e.button === 0 && e.target === lbImg && scale > 1) {{
        e.preventDefault();   // 避免浏览器原生拖拽 ghost
        panning = true; startX = e.clientX - tx; startY = e.clientY - ty; applyTransform();
        reportEvent("灯箱拖动平移开始");
      }}
    }});
    window.addEventListener('mousemove', function (e) {{
      if (!panning) return;
      tx = e.clientX - startX; ty = e.clientY - startY; applyTransform();
    }});
    window.addEventListener('mouseup', function () {{
      if (panning) {{ suppressClose = true; setTimeout(function () {{ suppressClose = false; }}, 0); }}
      panning = false; applyTransform();
    }});

    document.addEventListener('keydown', function (e) {{
      if (lb.hidden) return;
      if (e.key === 'Escape') closeLB();
      else if (e.key === 'ArrowLeft') {{ reportEvent("灯箱键盘上一张"); show(cur - 1); }}
      else if (e.key === 'ArrowRight') {{ reportEvent("灯箱键盘下一张"); show(cur + 1); }}
    }});
  </script>
</body>
</html>
"""
    return html


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return send_from_directory(BASE_DIR / "static", "index.html")


@app.route("/api/event", methods=["POST"])
def frontend_event():
    """前端把用户/页面事件发到这里，后台记录到日志。"""
    data = request.get_json(silent=True) or {}
    msg = str(data.get("message", "(无内容)"))
    log.info("FRONTEND: %s", msg)
    return jsonify(ok=True)


@app.route("/api/generate", methods=["POST"])
def generate():
    js = request.get_json(silent=True) or {}
    dir_name = (request.form.get("dirName") or js.get("dirName") or "").strip()
    raw_path = (request.form.get("path") or js.get("path") or "").strip()

    photos = []

    if raw_path:
        # 服务器可直接访问的绝对路径，后台扫描。
        # 优先把 gallery.html 写到源目录根，灯箱用相对路径直接引用原始文件（零复制）。
        root = Path(raw_path).expanduser()
        if not root.exists() or not root.is_dir():
            log.warning("BACKEND: 目录不存在或不是文件夹：%s", raw_path)
            return jsonify({"error": f"目录不存在或不是文件夹: {raw_path}"}), 400
        if not dir_name:
            dir_name = root.name or str(root)
        log.info("BACKEND: 服务器目录绝对路径：%s", raw_path)
        images = scan_images_on_disk(root)
        total = len(images)
        if not total:
            log.warning("BACKEND: 目录「%s」下未发现图片", dir_name)
            return jsonify({"error": f"该目录下没有发现图片: {raw_path}"}), 400
        if os.access(root, os.W_OK):
            out_html = root / "gallery.html"
            thumbs_dir = root / "thumbs"
            copy_mode = False
            log.info("BACKEND: 源目录可写，gallery.html 生成在源目录内，原图直接相对引用（不复制）")
        else:
            safe = re.sub(r'[:*?"<>|/\\]', "_", dir_name) or "photos"
            workdir = GALLERY_ASSETS_DIR / safe
            out_html = workdir / "gallery.html"
            thumbs_dir = workdir / "thumbs"
            copy_mode = True
            log.info("BACKEND: 源目录无写权限，回退到工作目录 %s（原图会暂存一份）", workdir)
        log.info("BACKEND: 开始扫描目录「%s」，共 %d 张图片（增量 + 并行生成：已复用缓存的将跳过）", dir_name, total)

        # 第一遍（快速、串行）：只判断每张图需要生成哪些产物，避免重复读取/编码
        tasks = []
        for img_path in images:
            rel = str(img_path.relative_to(root))
            tname = thumb_filename(rel)
            thumb_ok = not source_changed(img_path, thumbs_dir / tname)
            if copy_mode:
                oname = orig_filename(rel)
                orig_ok = not source_changed(img_path, out_html.parent / oname)
            else:
                oname = None
                orig_ok = True
            native = is_browser_native(img_path)
            do_view = (not native) and source_changed(img_path, thumbs_dir / view_filename(rel))
            tasks.append((img_path, rel, tname, thumb_ok, oname, orig_ok, do_view, native))

        def _needs_work(t):
            _, _, _, thumb_ok, _oname, orig_ok, do_view, _native = t
            if not thumb_ok:
                return True
            if copy_mode and not orig_ok:
                return True
            if do_view:
                return True
            return False

        skipped = sum(1 for t in tasks if not _needs_work(t))

        # 第二遍（并行）：只处理需要生成的图，其余直接复用缓存
        def worker(t):
            img_path, rel, tname, thumb_ok, oname, orig_ok, do_view, native = t
            try:
                data = img_path.read_bytes()
            except Exception as exc:
                return ("skip", img_path.name, str(exc))
            try:
                if not thumb_ok:
                    save_thumb_file(data, thumbs_dir, tname)
                if copy_mode and not orig_ok:
                    save_original_file(data, out_html.parent, oname)
                if do_view:
                    save_preview_file(data, thumbs_dir, view_filename(rel))
            except Exception as exc:
                return ("skip", img_path.name, str(exc))
            # 下载用原图(real) 永远指向真实文件；显示用原图(view) 对浏览器不可渲染格式换成预览图
            if copy_mode:
                orig = oname
            else:
                orig = rel
            view = orig if native else "thumbs/" + view_filename(rel)
            return ("ok", {"name": rel, "thumb": "thumbs/" + tname,
                           "orig": orig, "view": view, "full": str(img_path)})

        max_workers = min(8, (os.cpu_count() or 4) + 2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            for res in ex.map(worker, tasks):
                if res[0] == "skip":
                    log.warning("BACKEND: 跳过无法处理的图片 %s：%s", res[1], res[2])
                else:
                    photos.append(res[1])

        if skipped:
            log.info("BACKEND: 跳过 %d 张未改动的图片（缩略图/副本/预览已复用），实际新生成 %d 张",
                     skipped, total - skipped)
    else:
        log.warning("BACKEND: /api/generate 未收到任何图片或路径")
        return jsonify({"error": "请提供图片或目录路径"}), 400

    if not photos:
        log.warning("BACKEND: 没有可生成的图片，已终止")
        return jsonify({"error": "没有可生成的图片"}), 400

    html = build_gallery_html(dir_name, photos)
    out_html.write_text(html, encoding="utf-8")
    log.info("BACKEND: 已生成 gallery.html：%s（%d 张照片）", out_html, len(photos))
    log.info("BACKEND: 缩略图目录：%s", thumbs_dir)
    if raw_path and not copy_mode:
        log.info("BACKEND: 原图引用方式：直接相对引用原始文件（未复制任何原图）")

    # 自动用系统默认浏览器打开
    try:
        webbrowser.open(out_html.as_uri())
        log.info("BACKEND: 已自动打开 gallery.html")
    except Exception as exc:
        log.warning("BACKEND: 自动打开失败：%s", exc)

    # 共享给网络其他客户：切换 FTP 共享目录，并记录 HTTP 共享地址
    share_dir = out_html.parent
    update_ftp_share(share_dir)

    # 后台构建人脸索引（检测 + 聚类），不阻塞前台工作
    try:
        threading.Thread(
            target=build_face_index,
            args=(root, photos, share_dir, copy_mode),
            daemon=True,
        ).start()
        log.info("BACKEND: 已在后台启动人脸索引构建（检测 + 聚类）")
    except Exception as exc:
        log.warning("BACKEND: 启动人脸索引线程失败：%s", exc)
    ip = get_local_ip()
    ftp_url = f"ftp://{ip}:{FTP_PORT}"
    share_url = f"http://{ip}:{SHARE_HTTP_PORT}/share/gallery.html"
    log.info("BACKEND: 画廊已通过 HTTP 共享：%s", share_url)
    log.info("BACKEND: 选中目录已通过 FTP 共享：%s", ftp_url)

    # 记住最近扫描的目录，供下次启动/刷新 WebUI 预填文本框
    if raw_path:
        save_last_folder(raw_path)

    return jsonify(ok=True, path=str(out_html), count=len(photos), name=dir_name,
                   ftp_url=ftp_url, share_url=share_url)


@app.route("/api/logstream")
def log_stream():
    """SSE：把后台日志实时推送给前端日志面板。"""
    def gen():
        yield "retry: 1000\n\n"
        while True:
            try:
                line = log_queue.get(timeout=1)
                yield f"data: {line}\n\n"
            except queue.Empty:
                yield ": keepalive\n\n"
    return Response(gen(), mimetype="text/event-stream")


@app.route("/api/health")
def health():
    return jsonify(ok=True)


@app.route("/api/info")
def info():
    """返回本机局域网 IP 与共享端口，供前端在 WebUI 下方常驻展示共享链接。"""
    ip = LOCAL_IP or get_local_ip()
    return jsonify(ok=True, ip=ip, ftp_port=FTP_PORT, share_port=SHARE_HTTP_PORT)


@app.route("/api/last-folder")
def last_folder():
    """返回 fold.json 中保存的最近扫描目录，供前端启动时预填文本框。"""
    return jsonify(ok=True, folder=load_last_folder())


# --------------------------------------------------------------------------- #
# 局域网共享：FTP 服务 + HTTP /share 路由
# --------------------------------------------------------------------------- #
def get_local_ip() -> str:
    """获取本机局域网 IP（用于生成共享地址）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def init_ftp_server() -> bool:
    """在独立线程启动 FTP server（匿名只读），初始共享占位目录。返回是否启动。"""
    global ftp_authorizer, ftp_server
    if not _HAS_FTP:
        log.warning("BACKEND: 未安装 pyftpdlib，FTP 共享已禁用（请 pip install pyftpdlib）")
        return False
    placeholder = BASE_DIR / "ftp_share"
    placeholder.mkdir(parents=True, exist_ok=True)
    authorizer = DummyAuthorizer()
    authorizer.add_anonymous(str(placeholder), perm="elr")  # 只读：列表/下载/重命名
    handler = FTPHandler
    handler.authorizer = authorizer
    handler.banner = "PhotoGallery FTP — 只读共享选中目录"
    try:
        handler.passive_ports = range(60000, 60100)
    except Exception:
        pass
    server = FTPServer(("0.0.0.0", FTP_PORT), handler)
    ftp_authorizer = authorizer
    ftp_server = server
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("BACKEND: FTP 服务已启动，监听 0.0.0.0:%d（匿名只读），初始共享占位目录", FTP_PORT)
    return True


def update_ftp_share(directory: Path) -> None:
    """运行时把 FTP 共享目录切换为 directory（选中目录），并更新全局 SHARED_DIR。

    实现：重建一个匿名只读的 DummyAuthorizer 指向新目录，并替换
    FTPHandler.authorizer（类属性），后续新连接即使用新目录；已建立的连接不受影响。
    """
    global SHARED_DIR, ftp_authorizer
    d = str(directory)
    SHARED_DIR = directory
    if ftp_server is not None and _HAS_FTP:
        try:
            new_auth = DummyAuthorizer()
            new_auth.add_anonymous(d, perm="elr")   # 要求 d 已存在（生成时保证）
            ftp_authorizer = new_auth
            FTPHandler.authorizer = new_auth
            log.info("BACKEND: FTP 共享目录已切换为：%s", d)
        except Exception as exc:
            log.warning("BACKEND: 更新 FTP 共享目录失败：%s", exc)
    else:
        log.info("BACKEND: 共享目录已设为：%s（FTP 未启用）", d)


@app.route("/share/")
@app.route("/share/<path:filename>")
def share_files(filename="gallery.html"):
    """通过 HTTP 把当前共享目录（含 gallery.html/原图/缩略图）提供给网络其他客户。"""
    if SHARED_DIR is None:
        return "尚未生成画廊，无法共享。请先在 WebUI 选择目录并生成 gallery.html。", 404
    target = SHARED_DIR / filename
    if not target.exists():
        return f"文件不存在：{filename}", 404
    return send_from_directory(str(SHARED_DIR), filename)


# --------------------------------------------------------------------------- #
# 人脸索引：扫描完成后在后台检测/识别/聚类，建立人脸数据库与可检索的 WebUI
# --------------------------------------------------------------------------- #
FACE_INDEX = {
    "running": False, "ready": False,
    "faces": 0, "clusters": 0, "error": "",
}


def get_face_app():
    """懒加载 insightface FaceAnalysis（buffalo_l：SCRFD 检测 + ArcFace 512 维识别）。
    只构建一次（线程安全），后续复用。"""
    global _face_app
    if _face_app is None:
        with _face_app_lock:
            if _face_app is None:
                try:
                    from insightface.app import FaceAnalysis
                except Exception as exc:
                    raise RuntimeError(f"未安装 insightface：{exc}")
                a = FaceAnalysis(name="buffalo_l")
                a.prepare(ctx_id=-1, det_size=FACE_DET_SIZE)  # CPU 推理
                _face_app = a
                log.info("BACKEND: 人脸模型 buffalo_l 已加载")
    return _face_app


_face_app = None
_face_app_lock = threading.Lock()


def _analysis_image(photo: dict, root: Path, asset_dir: Path, copy_mode: bool):
    """为一张照片挑选最适合做人脸检测/识别的图像（BGR numpy），优先用原图，
    失败回退到预览图/缩略图。原图过大时缩放到最长边 <=1280 以提速且不影响识别。"""
    if copy_mode:
        candidates = [asset_dir / photo["orig"]]
    else:
        candidates = [root / photo["name"]]
    if photo.get("view"):
        candidates.append(asset_dir / photo["view"])
    candidates.append(asset_dir / photo["thumb"])
    for p in candidates:
        try:
            if not p.exists():
                continue
            with Image.open(p) as im:
                im = im.convert("RGB")
                w, h = im.size
                long = max(w, h)
                if long > 1280:
                    s = 1280.0 / long
                    im = im.resize((max(1, int(w * s)), max(1, int(h * s))))
                return np.asarray(im)[..., ::-1].copy()  # RGB -> BGR
        except Exception:
            continue
    return None


def cluster_faces(embs, thresh):
    """用余弦相似度阈值做贪心聚类：每张脸与之前最相似的脸比较，
    若 >= 阈值则归入同一聚类，否则新建聚类。返回 [[face_id,...], ...]。"""
    n = len(embs)
    if n == 0:
        return []
    E = np.stack(embs)  # n x 512（已 L2 归一化）
    assign = [-1] * n
    next_c = 0
    for i in range(n):
        if i > 0:
            sims = E[:i] @ E[i]  # 与之前所有脸的余弦相似度
            j = int(np.argmax(sims))
            if sims[j] >= thresh:
                assign[i] = assign[j]
                continue
        assign[i] = next_c
        next_c += 1
    groups = {}
    for i, a in enumerate(assign):
        groups.setdefault(a, []).append(i)
    return list(groups.values())


def build_face_index(root: Path, photos: list, asset_dir: Path, copy_mode: bool) -> None:
    """后台线程入口：对每张照片检测人脸、裁剪、提取特征，再聚类，
    结果写入 faces.json（聚类 + 标签），人脸裁剪图写入 faces/。"""
    FACE_INDEX["running"] = True
    FACE_INDEX["ready"] = False
    FACE_INDEX["faces"] = 0
    FACE_INDEX["clusters"] = 0
    FACE_INDEX["error"] = ""
    try:
        fa = get_face_app()
        FACES_DIR.mkdir(parents=True, exist_ok=True)
        faces = []
        embeddings = []
        fid = 0
        for photo in photos:
            img = _analysis_image(photo, root, asset_dir, copy_mode)
            if img is None:
                continue
            try:
                dets = fa.get(img)
            except Exception as exc:
                log.warning("BACKEND: 人脸检测失败 %s：%s", photo["name"], exc)
                continue
            for d in dets:
                try:
                    emb = np.asarray(d.embedding, dtype=np.float32)
                    nrm = np.linalg.norm(emb)
                    if nrm <= 0:
                        continue
                    emb = emb / nrm
                    x1, y1, x2, y2 = [int(v) for v in d.bbox]
                    ih, iw = img.shape[:2]
                    mw, mh = (x2 - x1), (y2 - y1)
                    x1 = max(0, int(x1 - 0.2 * mw)); y1 = max(0, int(y1 - 0.2 * mh))
                    x2 = min(iw, int(x2 + 0.2 * mw)); y2 = min(ih, int(y2 + 0.2 * mh))
                    if x2 <= x1 or y2 <= y1:
                        continue
                    crop = img[y1:y2, x1:x2]
                    if crop.size == 0:
                        continue
                    crop_pil = Image.fromarray(crop[..., ::-1]).resize((112, 112))
                    cpath = FACES_DIR / f"face_{fid}.jpg"
                    crop_pil.save(cpath, "JPEG", quality=90)
                    faces.append({
                        "id": fid, "photo": photo["name"],
                        "bbox": [x1, y1, x2 - x1, y2 - y1],
                        "crop": f"faces/face_{fid}.jpg",
                        "score": round(float(d.det_score), 3),
                        "cluster": -1,
                    })
                    embeddings.append(emb)
                    fid += 1
                except Exception as exc:
                    log.warning("BACKEND: 单张人脸处理失败：%s", exc)
        FACE_INDEX["faces"] = fid
        log.info("BACKEND: 人脸检测完成，共 %d 张人脸（来自 %d 张照片）", fid, len(photos))

        groups = cluster_faces(embeddings, FACE_MATCH_THRESH)
        cdict = {}
        for ci, members in enumerate(groups):
            rep = max(members, key=lambda i: faces[i]["score"])
            cdict[str(ci)] = {"label": "", "rep": rep, "count": len(members)}
            for i in members:
                faces[i]["cluster"] = ci
        FACE_INDEX["clusters"] = len(groups)
        data = {
            "version": 1,
            "root": str(root),
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "photos": photos,
            "faces": faces,
            "clusters": cdict,
        }
        FACES_JSON.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        FACE_INDEX["ready"] = True
        log.info("BACKEND: 人脸聚类完成，共 %d 个聚类（人物）", len(groups))
    except Exception as exc:
        FACE_INDEX["error"] = str(exc)
        log.exception("BACKEND: 人脸索引构建失败：%s", exc)
    finally:
        FACE_INDEX["running"] = False


def load_faces():
    """读取 faces.json；不存在/损坏返回 None。"""
    try:
        if FACES_JSON.exists():
            return json.loads(FACES_JSON.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


@app.route("/api/faces/status")
def faces_status():
    """返回人脸索引构建进度（前端按钮可据此提示）。"""
    return jsonify(ok=True, **FACE_INDEX)


@app.route("/api/faces/clusters")
def faces_clusters():
    """返回聚类列表（每个聚类含 pid、标签、代表脸图片地址、照片数）。"""
    d = load_faces()
    if not d:
        return jsonify(ok=False, reason="no-index")
    faces = d["faces"]
    clusters = []
    for cid, c in d["clusters"].items():
        rep = c.get("rep")
        rep_url = ("/faces/face/" + str(rep)) if (isinstance(rep, int) and 0 <= rep < len(faces)) else None
        clusters.append({
            "pid": int(cid), "label": c.get("label", ""),
            "count": c.get("count", 0), "rep_face_url": rep_url,
        })
    # 按出现次数（count）降序排列，次数相同则按 pid 升序保持稳定，避免刷新时顺序抖动
    clusters.sort(key=lambda x: (-x["count"], x["pid"]))
    return jsonify(ok=True, clusters=clusters, faces=len(faces), root=d.get("root", ""))


@app.route("/faces/face/<int:fid>")
def face_image(fid):
    """返回某张人脸裁剪图。"""
    p = FACES_DIR / f"face_{fid}.jpg"
    if not p.exists():
        return "404", 404
    return send_from_directory(str(FACES_DIR), f"face_{fid}.jpg")


@app.route("/api/faces/label", methods=["POST"])
def faces_label():
    """保存某个聚类（人物）的可编辑标签。"""
    js = request.get_json(silent=True) or {}
    pid = js.get("pid")
    label = str(js.get("label", ""))
    d = load_faces()
    if not d:
        return jsonify(ok=False, reason="no-index"), 404
    key = str(pid)
    if key not in d["clusters"]:
        return jsonify(ok=False, reason="bad-pid"), 404
    d["clusters"][key]["label"] = label
    try:
        FACES_JSON.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        return jsonify(ok=True)
    except Exception as exc:
        return jsonify(ok=False, reason=str(exc)), 500


def _faces_webui_html() -> str:
    """人脸寻找 WebUI：列出按聚类分组的人脸（代表脸 + 可编辑标签 + 查看链接）。"""
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>PhotoGallery · 人脸寻找</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    background: #f5f6f8; color: #1f2430; min-height: 100vh; }
  header { position: sticky; top: 0; z-index: 10; display: flex; align-items: center; justify-content: space-between;
    gap: 16px; padding: 14px 22px; background: #fff; border-bottom: 1px solid #e6e8ee; }
  .title { font-weight: 700; font-size: 18px; }
  .title small { color: #8a90a2; font-weight: 500; margin-left: 8px; font-size: 13px; }
  .home-btn { border: 1px solid #3b6cff; background: #3b6cff; color: #fff; padding: 9px 16px; border-radius: 9px;
    font-size: 14px; cursor: pointer; text-decoration: none; display: inline-block; }
  .home-btn:hover { filter: brightness(1.06); }
  main { padding: 24px; }
  .hint { color: #8a90a2; font-size: 13px; margin-bottom: 16px; }
  .status-banner { background: #eef2ff; border: 1px solid #cfd8ff; color: #2f57d6; padding: 14px 16px;
    border-radius: 10px; font-size: 14px; margin-bottom: 16px; }
  .grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); }
  .card { background: #fff; border: 1px solid #e6e8ee; border-radius: 12px; overflow: hidden;
    box-shadow: 0 6px 20px rgba(20,30,60,.08); display: flex; flex-direction: column; }
  .card img { width: 100%; aspect-ratio: 1/1; object-fit: cover; background: #eef0f5; display: block;
    cursor: pointer; transition: transform .12s; }
  .card img:hover { transform: scale(1.03); }
  .card .label { width: 100%; border: none; border-top: 1px solid #eef0f5; padding: 8px 10px; font-size: 13px;
    outline: none; color: #1f2430; }
  .card .label:focus { background: #f4f7ff; }
  .card .meta { padding: 6px 10px 4px; font-size: 12px; color: #8a90a2; display: flex; justify-content: space-between; }
  .card .view { display: block; text-align: center; padding: 8px 10px; font-size: 13px; color: #3b6cff;
    text-decoration: none; border-top: 1px solid #eef0f5; }
  .card .view:hover { background: #eef2ff; }
  .saved { color: #1fa971 !important; }
  .empty { color: #8a90a2; text-align: center; padding: 80px 20px; }
</style>
</head>
<body>
  <header>
    <div class="title">PhotoGallery<small>人脸寻找 · 按人物归类</small></div>
    <a class="home-btn" href="/">← 返回主页</a>
  </header>
  <main>
    <p class="hint">每张代表脸是该人物的一个人脸；下方标签可点击编辑（如姓名），回车或失焦自动保存。点击人脸或「查看所有照片」打开该人物的全部照片。</p>
    <div id="banner" class="status-banner" hidden></div>
    <div class="grid" id="grid"></div>
    <p class="empty" id="empty" hidden>尚未建立人脸索引。请先在首页输入目录并「扫描并生成」。</p>
  </main>
  <script>
    const grid = document.getElementById('grid');
    const banner = document.getElementById('banner');
    const empty = document.getElementById('empty');

    function showBanner(text) { banner.textContent = text; banner.hidden = false; }
    function hideBanner() { banner.hidden = true; }

    // 本地日志上报：/faces 是独立页面，不加载 app.js，这里自备 reportEvent
    function reportEvent(message) {
      try { fetch('/api/event', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: message }) }); } catch (_) {}
    }

    function renderClusters(clusters) {
      grid.innerHTML = '';
      if (!clusters.length) { empty.hidden = false; return; }
      empty.hidden = true;
      for (const c of clusters) {
        const card = document.createElement('div');
        card.className = 'card';
        const img = document.createElement('img');
        img.src = c.rep_face_url || '';
        img.alt = c.label || ('人物 ' + c.pid);
        img.addEventListener('click', function () {
          const w = window.open('/faces/person/' + c.pid, '_blank');
          if (!w) window.location.href = '/faces/person/' + c.pid;
        });
        const label = document.createElement('input');
        label.className = 'label';
        label.value = c.label || '';
        label.placeholder = '人物 ' + c.pid;
        label.dataset.pid = c.pid;
        const meta = document.createElement('div');
        meta.className = 'meta';
        meta.innerHTML = '<span>#' + c.pid + '</span><span>' + c.count + ' 张</span>';
        const view = document.createElement('a');
        view.className = 'view';
        view.textContent = '查看所有照片 →';
        view.href = '/faces/person/' + c.pid;
        view.addEventListener('click', function (e) {
          e.preventDefault();
          const w = window.open('/faces/person/' + c.pid, '_blank');
          if (!w) window.location.href = '/faces/person/' + c.pid;
        });
        function save() {
          const v = label.value;
          fetch('/api/faces/label', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pid: c.pid, label: v }) })
            .then(function (r) { return r.json(); })
            .then(function (j) {
              if (j.ok) { label.classList.add('saved'); setTimeout(function () { label.classList.remove('saved'); }, 800); }
            });
        }
        label.addEventListener('change', save);
        label.addEventListener('keydown', function (e) { if (e.key === 'Enter') { label.blur(); } });
        card.appendChild(img); card.appendChild(label); card.appendChild(meta); card.appendChild(view);
        grid.appendChild(card);
      }
    }

    function loadClusters() {
      fetch('/api/faces/clusters', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (j) {
        if (j.ok) { hideBanner(); renderClusters(j.clusters); }
        else { pollStatus(); }
      }).catch(function () { pollStatus(); });
    }

    var _lastFaces = -1, _lastClusters = -1;
    function pollStatus() {
      fetch('/api/faces/status', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (s) {
        if (s.ready) {
          reportEvent('人脸聚类完成，共 ' + s.clusters + ' 个聚类（人物）、' + s.faces + ' 张人脸');
          loadClusters();
        }
        else if (s.running) {
          if (s.faces !== _lastFaces || s.clusters !== _lastClusters) {
            _lastFaces = s.faces; _lastClusters = s.clusters;
            reportEvent('人脸索引构建中：已检测 ' + s.faces + ' 张人脸，聚类 ' + s.clusters + ' 组');
          }
          showBanner('⏳ 正在后台建立人脸索引（已检测 ' + s.faces + ' 张人脸，聚类 ' + s.clusters + ' 组）…'); setTimeout(pollStatus, 1500);
        }
        else if (s.error) { reportEvent('人脸索引构建失败：' + s.error); showBanner('人脸索引构建失败：' + s.error); }
        else { empty.hidden = false; showBanner('尚未建立人脸索引。请先在首页输入目录并「扫描并生成」。'); }
      }).catch(function () { showBanner('无法连接后端，请确认 PhotoGallery 正在运行。'); });
    }

    pollStatus();
  </script>
</body>
</html>
"""


@app.route("/faces")
def faces_webui():
    """人脸寻找入口页。"""
    return _faces_webui_html()


@app.route("/faces/person/<int:pid>")
def faces_person(pid):
    """该人物（聚类）的所有照片画廊，复用 build_gallery_html 的灯箱体验。"""
    d = load_faces()
    if not d:
        return "尚未建立人脸索引，请先扫描目录生成画廊。", 404
    c = d["clusters"].get(str(pid))
    if not c:
        return "未找到该人物。", 404
    member_faces = [f for f in d["faces"] if f.get("cluster") == pid]
    seen = set()
    names = []
    for f in member_faces:
        if f["photo"] not in seen:
            seen.add(f["photo"])
            names.append(f["photo"])
    photos_map = {p["name"]: p for p in d.get("photos", [])}
    sub = [photos_map[n] for n in names if n in photos_map]
    label = c.get("label") or f"人物 {pid}"
    # 把相对路径改成 /share/ 绝对路径（该页不在 /share 下）
    def _abs(p):
        return "/share/" + p if p and not p.startswith("/") else p
    sub2 = [{
        "name": p["name"],
        "thumb": _abs(p["thumb"]),
        "orig": _abs(p["orig"]),
        "view": _abs(p.get("view", p["orig"])),
        "full": p.get("full", p["name"]),
    } for p in sub]
    html = build_gallery_html(f"{label}（{len(sub2)} 张）", sub2)
    # 把画廊页的「返回主页」改成「返回人脸」（HOME_URL 在 build_gallery_html 中已被替换为真实地址）
    html = html.replace(f'class="home-btn" href="{HOME_URL}"', 'class="home-btn" href="/faces"')
    html = html.replace("← 返回主页", "← 返回人脸")
    return html


# --------------------------------------------------------------------------- #
# 局域网共享：FTP 客户端（以 HTTP 方式浏览 / 下载 FTP 共享目录）
# --------------------------------------------------------------------------- #
def _ftp_client():
    """新建一个连到本机 FTP 服务的匿名客户端（只读，目录已随生成时切换）。"""
    ftp = ftplib.FTP()
    ftp.connect("127.0.0.1", FTP_PORT)
    ftp.login()  # 匿名
    return ftp


def _ftp_list_dir(rel: str):
    """列出 FTP 共享目录 rel 下的条目，返回 [(name, is_dir, size), ...]。
    目录优先、再按名称排序。rel 以 '/' 开头（FTP 服务已 chroot 到共享目录）。"""
    if not rel.startswith("/"):
        rel = "/" + rel
    ftp = _ftp_client()
    try:
        entries = []
        try:
            for name, facts in ftp.mlsd(rel):
                if name in (".", ".."):
                    continue
                is_dir = facts.get("type") == "dir"
                try:
                    size = int(facts.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                entries.append((name, is_dir, size))
        except ftplib.error_perm:
            # 个别服务器不支持 MLSD，回退到 NLST（只有名字，无类型/大小）
            for name in ftp.nlst(rel):
                if name not in (".", ".."):
                    entries.append((name, False, 0))
        entries.sort(key=lambda e: (not e[1], e[0].lower()))
        return entries
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


def _fmt_size(n: int) -> str:
    if not n or n <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _ftp_file_stat(rel: str):
    """确认 rel 是 FTP 共享目录下的一个文件，返回其大小（int，可能为 0）；
    不存在或为目录则返回 None。优先用 MLSD（避免 SIZE 命令在 ASCII 模式下被拒的兼容问题）。"""
    if not rel.startswith("/"):
        rel = "/" + rel
    parent = rel.rstrip("/").rsplit("/", 1)[0] or "/"
    name = rel.rstrip("/").split("/")[-1]
    ftp = _ftp_client()
    try:
        try:
            for n, facts in ftp.mlsd(parent):
                if n == name:
                    try:
                        return int(facts.get("size") or 0)
                    except (TypeError, ValueError):
                        return 0
        except ftplib.error_perm:
            # 退回 NLST 仅确认是否存在（无大小信息）
            if name in ftp.nlst(parent):
                return 0
    finally:
        try:
            ftp.quit()
        except Exception:
            pass
    return None


def _ftp_collect_files(rel: str):
    """递归收集 rel 目录下所有文件的 FTP 相对路径（含子目录），返回列表。"""
    if not rel.startswith("/"):
        rel = "/" + rel
    result = []
    stack = [rel]
    while stack:
        cur = stack.pop()
        for name, is_dir, _ in _ftp_list_dir(cur):
            full = cur.rstrip("/") + "/" + name
            if is_dir:
                stack.append(full)
            else:
                result.append(full)
    return result


def _build_zip(rel: str) -> str:
    """把 rel 目录（含子目录）整体递归下载并打包成 zip，返回临时文件路径（调用方负责删除）。
    使用 zf.open 流式写入以控制内存占用；用单个 FTP 连接顺序下载所有文件。"""
    if not rel.startswith("/"):
        rel = "/" + rel
    tmp = tempfile.NamedTemporaryFile(prefix="pg_ftp_zip_", suffix=".zip", delete=False)
    tmp.close()
    base = rel.strip("/")
    ftp = _ftp_client()
    try:
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
            for fpath in _ftp_collect_files(rel):
                arcname = fpath.lstrip("/")
                if base and arcname.startswith(base + "/"):
                    arcname = arcname[len(base) + 1:]
                with zf.open(arcname, "w", force_zip64=True) as dst:
                    ftp.retrbinary("RETR " + fpath, dst.write, blocksize=65536)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass
    return tmp.name


def _send_and_remove(path: str, filename: str):
    """流式返回临时 zip 文件，请求结束（流耗尽）后删除临时文件。"""
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                yield chunk
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


@app.route("/ftp/zip")
def ftp_zip():
    """把 FTP 共享目录（rel）整体打包成 ZIP 供下载（递归包含全部子目录与文件）。"""
    if not _HAS_FTP or ftp_server is None:
        return "FTP 服务未启用，无法打包下载。", 503
    if SHARED_DIR is None:
        return "尚未生成画廊，FTP 未共享目录，无法打包下载。", 404
    rel = request.args.get("path", "/") or "/"
    if not rel.startswith("/"):
        rel = "/" + rel
    # 先确认目录存在，避免进入耗时的打包流程才报错
    try:
        _ftp_list_dir(rel)
    except Exception as exc:
        log.warning("BACKEND: FTP 打包目录不存在：%s", exc)
        return f"目录不存在或无法访问：{rel}", 404
    dl_name = rel.strip("/").replace("/", "_") or "gallery"
    try:
        zpath = _build_zip(rel)
    except Exception as exc:
        log.warning("BACKEND: FTP 打包失败：%s", exc)
        return f"打包失败：{exc}", 500
    log.info("BACKEND: FTP 整目录打包下载：%s -> %s.zip", rel, dl_name)
    return Response(
        _send_and_remove(zpath, dl_name + ".zip"),
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{_html_escape(dl_name)}.zip"'},
    )


def _ftp_browse_html(rel: str, entries, crumbs: list) -> str:
    """把 FTP 目录条目渲染成自包含的 HTTP 目录浏览页（可浏览子目录、可下载文件）。"""
    root = "/ftp/browse"
    rows = []
    if rel != "/":
        parent = rel.rstrip("/").rsplit("/", 1)[0] or "/"
        rows.append(
            f'<li class="up"><a href="{root}?path={_url_quote(parent)}">⬆️ 返回上级目录</a></li>'
        )
    for name, is_dir, size in entries:
        full = rel.rstrip("/") + "/" + name
        if is_dir:
            rows.append(
                f'<li class="dir"><a href="{root}?path={_url_quote(full)}">📁 {_html_escape(name)}</a></li>'
            )
        else:
            view = f"/ftp/file?path={_url_quote(full)}&dl=0"
            down = f"/ftp/file?path={_url_quote(full)}&dl=1"
            rows.append(
                f'<li class="file">'
                f'<span class="nm"><a href="{view}" target="_blank">{_html_escape(name)}</a></span>'
                f'<span class="sz">{_fmt_size(size)}</span>'
                f'<a class="dl" href="{down}">⬇️ 下载</a></li>'
            )
    crumb_html = f'<a href="{root}?path=/">根目录</a>'
    for cname, cpath in crumbs:
        crumb_html += f' / <a href="{root}?path={_url_quote(cpath)}">{_html_escape(cname)}</a>'

    body = "\n".join(rows) if rows else '<li class="empty">（空目录）</li>'
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>PhotoGallery · FTP 浏览</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
    "Microsoft YaHei", sans-serif; background: #f5f6f8; color: #1f2430; min-height: 100vh; }}
  header {{ position: sticky; top: 0; z-index: 10; padding: 14px 22px; background: #fff;
    border-bottom: 1px solid #e6e8ee; }}
  .title {{ font-weight: 700; font-size: 18px; }}
  .title small {{ color: #8a90a2; font-weight: 500; margin-left: 8px; font-size: 13px; }}
  .crumbs {{ margin-top: 8px; font-size: 13px; color: #3b6cff; word-break: break-all; }}
  .crumbs a {{ color: #3b6cff; text-decoration: none; }}
  .crumbs a:hover {{ text-decoration: underline; }}
  main {{ padding: 18px 22px; }}
  ul.listing {{ list-style: none; background: #fff; border: 1px solid #e6e8ee;
    border-radius: 12px; overflow: hidden; }}
  ul.listing li {{ display: flex; align-items: center; gap: 12px; padding: 11px 14px;
    border-bottom: 1px solid #eef0f5; }}
  ul.listing li:last-child {{ border-bottom: none; }}
  ul.listing li.up {{ background: #f0f4ff; }}
  ul.listing li.empty {{ color: #8a90a2; justify-content: center; }}
  ul.listing .nm {{ flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  ul.listing .nm a {{ color: #1f2430; text-decoration: none; }}
  ul.listing .nm a:hover {{ color: #3b6cff; text-decoration: underline; }}
  ul.listing .sz {{ color: #8a90a2; font-size: 13px; min-width: 90px; text-align: right; }}
  ul.listing .dl {{ color: #3b6cff; font-size: 13px; text-decoration: none;
    border: 1px solid #3b6cff; border-radius: 8px; padding: 4px 10px; }}
  ul.listing .dl:hover {{ background: #3b6cff; color: #fff; }}
  .tip {{ color: #8a90a2; font-size: 13px; margin-top: 12px; }}
  .dlall {{ margin-bottom: 14px; }}
  .dlall .btn {{ display: inline-block; background: #3b6cff; color: #fff; text-decoration: none;
    font-size: 14px; font-weight: 600; padding: 9px 16px; border-radius: 9px; }}
  .dlall .btn:hover {{ background: #2f57d6; }}
</style>
</head>
<body>
  <header>
    <div class="title">PhotoGallery<small>FTP 目录浏览（通过 FTP 客户端以 HTTP 方式呈现）</small></div>
    <div class="crumbs">位置：{crumb_html}</div>
  </header>
  <main>
    <div class="dlall"><a class="btn" href="/ftp/zip?path={_url_quote(rel)}">📦 下载整个目录（打包 ZIP）</a></div>
    <ul class="listing">
{body}
    </ul>
    <p class="tip">点击文件名可在新标签内联查看（图片/文档），点击「下载」按钮保存到本机；文件夹可继续进入；上方按钮可把当前目录（含子目录）整体打包下载。</p>
  </main>
</body>
</html>
"""
    return html


@app.route("/ftp/browse")
def ftp_browse():
    """用 FTP 客户端连接本机 FTP 服务，把共享目录以 HTTP 目录页形式呈现，可浏览与下载。"""
    if not _HAS_FTP or ftp_server is None:
        return "FTP 服务未启用，无法浏览。请先安装 pyftpdlib。", 503
    if SHARED_DIR is None:
        return "尚未生成画廊，FTP 未共享任何目录，无法浏览。请先在 WebUI 选择目录并生成。", 404
    rel = request.args.get("path", "/") or "/"
    if not rel.startswith("/"):
        rel = "/" + rel
    try:
        entries = _ftp_list_dir(rel)
    except Exception as exc:
        log.warning("BACKEND: FTP 浏览失败：%s", exc)
        return f"FTP 浏览失败：{exc}", 500
    parts = [p for p in rel.split("/") if p]
    crumbs = []
    acc = ""
    for p in parts:
        acc += "/" + p
        crumbs.append((p, acc))
    log.info("BACKEND: FTP 浏览目录：%s（%d 个条目）", rel, len(entries))
    return _ftp_browse_html(rel, entries, crumbs)


@app.route("/ftp/file")
def ftp_file():
    """用 FTP 客户端把 FTP 共享文件经 HTTP 流式下载：图片/文档内联预览，其他文件强制下载。"""
    if not _HAS_FTP or ftp_server is None:
        return "FTP 服务未启用，无法下载。", 503
    if SHARED_DIR is None:
        return "尚未生成画廊，FTP 未共享目录，无法下载。", 404
    rel = request.args.get("path", "") or ""
    if not rel:
        return "缺少 path 参数。", 400
    if not rel.startswith("/"):
        rel = "/" + rel
    dl = request.args.get("dl") == "1"
    fname = rel.rstrip("/").split("/")[-1]
    ext = Path(fname).suffix.lower()
    mime = MIME_BY_EXT.get(ext, "application/octet-stream")

    ftp = _ftp_client()
    # 先确认文件存在/可访问（失败直接 404，避免已经开始流式传输才报错）
    size = _ftp_file_stat(rel)
    if size is None:
        try:
            ftp.quit()
        except Exception:
            pass
        return f"文件不存在或无法访问：{rel}", 404

    q: "queue.Queue[bytes]" = queue.Queue(maxsize=30)
    err = {}

    def producer():
        try:
            ftp.retrbinary("RETR " + rel, lambda c: q.put(c), blocksize=65536)
        except Exception as e:  # 传输中断等
            err["e"] = e
        finally:
            q.put(None)

    def gen():
        t = threading.Thread(target=producer, daemon=True)
        t.start()
        while True:
            chunk = q.get()
            if chunk is None:
                break
            yield chunk
        try:
            ftp.quit()
        except Exception:
            pass

    headers = {}
    # 非浏览器可直接预览的类型，或明确点「下载」时，强制以附件形式保存
    if dl or mime == "application/octet-stream":
        headers["Content-Disposition"] = f'attachment; filename="{_html_escape(fname)}"'
    log.info("BACKEND: FTP 下载（经 HTTP 中转）：%s mime=%s dl=%s", rel, mime, dl)
    return Response(gen(), mimetype=mime, headers=headers)


if __name__ == "__main__":
    setup_logging()
    LOCAL_IP = get_local_ip()   # 启动即获取本机局域网 IP
    log.info("BACKEND: 本机局域网 IP：%s", LOCAL_IP)
    log.info("BACKEND: PhotoGallery 启动，监听端口 2026，首页 %s", HOME_URL)
    init_ftp_server()
    log.info("BACKEND: 等待用户通过 WebUI 选择目录 ...")
    app.run(host="0.0.0.0", port=2026, debug=False, threaded=True)
