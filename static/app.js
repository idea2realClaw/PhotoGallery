"use strict";

const $ = (sel) => document.querySelector(sel);

const els = {
  pathInput: $("#pathInput"),
  scanBtn: $("#scanBtn"),
  status: $("#status"),
  logbody: $("#logbody"),
  clearLog: $("#clearLog"),
  loghead: $("#loghead"),
  logFold: $("#logFold"),
  shareBox: $("#shareBox"),
  ftpUrl: $("#ftpUrl"),
  shareUrl: $("#shareUrl"),
  copyFtp: $("#copyFtp"),
  browseFtp: $("#browseFtp"),
  copyShare: $("#copyShare"),
  browseShare: $("#browseShare"),
  netLinks: $("#netLinks"),
  netFtp: $("#netFtp"),
  netHttp: $("#netHttp"),
  copyNetFtp: $("#copyNetFtp"),
  browseNetFtp: $("#browseNetFtp"),
  copyNetHttp: $("#copyNetHttp"),
  browseNetHttp: $("#browseNetHttp"),
  healthBanner: $("#healthBanner"),
  faceSearch: $("#faceSearch"),
};

// ---------- 前端事件上报 ----------
async function reportEvent(message) {
  try {
    await fetch("/api/event", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
  } catch (_) { /* 日志上报失败不影响主流程 */ }
}

// ---------- 后台健康检查 ----------
async function pingHealth() {
  try {
    const res = await fetch("/api/health", { cache: "no-store" });
    return res.ok;
  } catch (_) {
    return false;
  }
}
function showHealthBanner(show) {
  if (els.healthBanner) els.healthBanner.hidden = !show;
}
async function checkHealth() {
  const ok = await pingHealth();
  showHealthBanner(!ok);
  if (els.scanBtn) {
    els.scanBtn.disabled = !ok;
    els.scanBtn.title = ok ? "" : "后台未连接，请先运行启动器（start.command / python app.py）并刷新页面";
  }
  return ok;
}

// ---------- 实时日志（SSE） ----------
function connectLog() {
  const es = new EventSource("/api/logstream");
  es.onmessage = (e) => appendLog(e.data);
  es.onerror = () => { /* 浏览器会自动重连 */ };
}
function appendLog(line) {
  const div = document.createElement("div");
  div.className = "logline";
  div.textContent = line;
  els.logbody.appendChild(div);
  while (els.logbody.childElementCount > 500) els.logbody.removeChild(els.logbody.firstChild);
  els.logbody.scrollTop = els.logbody.scrollHeight;
}

// ---------- 扫描并生成 ----------
els.scanBtn.addEventListener("click", async () => {
  const path = els.pathInput.value.trim();
  if (!path) {
    els.status.textContent = "请输入服务器可访问的完整目录路径。";
    return;
  }
  await scanByPath(path);
});
// 用户在路径框输入完成（失焦/回车）后上报，便于追溯扫描了哪个目录
els.pathInput.addEventListener("change", () => {
  const v = els.pathInput.value.trim();
  if (v) reportEvent("用户输入目录路径：" + v);
});

async function scanByPath(path) {
  els.status.textContent = `正在通知后台扫描：${path} …`;
  reportEvent(`用户提交路径：${path}（服务器绝对路径），后台开始扫描`);
  // 先快速探活：后台没启动就直接提示，避免无谓等待与“连不上”的困惑
  if (!(await pingHealth())) {
    showHealthBanner(true);
    els.status.textContent = "❌ 后台未连接：请先运行启动器（start.command / python app.py）并刷新页面。";
    reportEvent("扫描前健康检查失败：后台未启动");
    return;
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10 * 60 * 1000); // 10 分钟超时保护
  try {
    const res = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, dirName: "" }),
      signal: controller.signal,
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      els.status.textContent = `✅ 已生成 gallery.html（${data.count} 张），已自动打开。`;
      reportEvent(`后台扫描完成：${data.count} 张照片，已自动打开 gallery.html`);
      showShare(data);
    } else {
      els.status.textContent = "❌ " + (data.error || "扫描失败");
      reportEvent("扫描失败：" + (data.error || "未知错误"));
    }
  } catch (err) {
    if (err && err.name === "AbortError") {
      els.status.textContent = "⏳ 后台仍在处理中（目录可能较大）。请查看下方日志，处理完成后画廊会自动打开。";
      reportEvent("扫描请求超时（后台仍在处理）");
    } else {
      showHealthBanner(true);
      els.status.textContent = "❌ 无法连接后台：请确认启动器仍在运行，并刷新页面后重试。";
      reportEvent("扫描时网络错误");
    }
  } finally {
    clearTimeout(timer);
  }
}

els.clearLog.addEventListener("click", () => { els.logbody.innerHTML = ""; reportEvent("用户点击清空日志"); });

// ---------- 生成后展示共享地址（FTP / HTTP） ----------
function showShare(data) {
  if (!data || !data.ftp_url) return;
  els.shareBox.hidden = false;
  els.ftpUrl.textContent = data.ftp_url;
  els.shareUrl.textContent = data.share_url;
  els.ftpUrl.dataset.val = data.ftp_url;
  els.shareUrl.dataset.val = data.share_url;
}
function copyText(btn, text) {
  if (!text) return;
  reportEvent("用户复制链接：" + String(text).slice(0, 60));
  navigator.clipboard.writeText(text).then(() => {
    const old = btn.textContent;
    btn.textContent = "已复制";
    setTimeout(() => (btn.textContent = old), 1200);
  }).catch(() => {});
}
els.copyFtp.addEventListener("click", () => copyText(els.copyFtp, els.ftpUrl.dataset.val || els.ftpUrl.textContent));
els.copyShare.addEventListener("click", () => copyText(els.copyShare, els.shareUrl.dataset.val || els.shareUrl.textContent));

// 浏览按钮：HTTP 直接打开网页；FTP 经 FTP 客户端以 HTTP 目录方式浏览
function openBrowse(btn, url) {
  reportEvent("用户点击浏览按钮：" + String(url).slice(0, 60));
  window.open(url, "_blank");
}
els.browseFtp.addEventListener("click", () => openBrowse(els.browseFtp, "/ftp/browse"));
els.browseNetFtp.addEventListener("click", () => openBrowse(els.browseNetFtp, "/ftp/browse"));
els.browseShare.addEventListener("click", () => openBrowse(els.browseShare, els.shareUrl.dataset.val || els.shareUrl.textContent));
els.browseNetHttp.addEventListener("click", () => openBrowse(els.browseNetHttp, els.netHttp.dataset.val || els.netHttp.textContent));

// ---------- WebUI 下方常驻展示局域网共享地址（FTP / HTTP） ----------
async function loadNetLinks() {
  try {
    const res = await fetch("/api/info");
    const data = await res.json();
    if (!data || !data.ok) return;
    const ip = data.ip;
    const ftp = `ftp://${ip}:${data.ftp_port}`;
    const http = `http://${ip}:${data.share_port}/share/gallery.html`;
    els.netLinks.hidden = false;
    els.netFtp.textContent = ftp;
    els.netFtp.href = ftp;
    els.netFtp.dataset.val = ftp;
    els.netHttp.textContent = http;
    els.netHttp.href = http;
    els.netHttp.dataset.val = http;
    reportEvent(`WebUI 展示局域网共享地址：FTP=${ftp}  HTTP=${http}`);
  } catch (_) { /* 获取失败不影响主流程 */ }
}
els.copyNetFtp.addEventListener("click", () => copyText(els.copyNetFtp, els.netFtp.dataset.val || els.netFtp.textContent));
els.copyNetHttp.addEventListener("click", () => copyText(els.copyNetHttp, els.netHttp.dataset.val || els.netHttp.textContent));

// ---------- 启动时预填最近扫描的目录 ----------
async function loadLastFolder() {
  try {
    const res = await fetch("/api/last-folder");
    const data = await res.json();
    const folder = (data && data.folder) || "";
    if (folder) {
      els.pathInput.value = folder;
      reportEvent(`预填最近扫描目录：${folder}`);
    }
  } catch (_) { /* 获取失败不影响主流程 */ }
}

// ---------- 日志面板折叠（缺省折叠，localStorage 记忆） ----------
const LOG_FOLD_KEY = "pg_log_collapsed";
function applyLogFold(collapsed) {
  els.logbody.hidden = collapsed;
  els.logFold.textContent = collapsed ? "展开 ▾" : "折叠 ▴";
}
function toggleLog() {
  const collapsed = !els.logbody.hidden;
  applyLogFold(collapsed);
  localStorage.setItem(LOG_FOLD_KEY, collapsed ? "1" : "0");
  reportEvent(collapsed ? "用户折叠日志" : "用户展开日志");
}
els.logFold.addEventListener("click", (e) => { e.stopPropagation(); toggleLog(); });
els.loghead.addEventListener("click", (e) => {
  if (e.target.closest("#clearLog")) return; // 清空按钮不触发折叠
  toggleLog();
});

// ---------- Init ----------
reportEvent("WebUI 首页已加载");
connectLog();
loadNetLinks();
loadLastFolder();
checkHealth();                 // 页面加载即探活，后台没起立刻提示
setInterval(checkHealth, 15000); // 每 15 秒复查后台连通性
// 人脸寻找：尝试新标签打开，被拦截（沙箱/弹窗拦截器）则当前标签兜底
if (els.faceSearch) {
  els.faceSearch.addEventListener("click", (e) => {
    e.preventDefault();
    const w = window.open("/faces", "_blank");
    if (!w) window.location.href = "/faces";
  });
}
// 缺省折叠（仅当 localStorage 显式为 "0" 时才默认展开）
applyLogFold(localStorage.getItem(LOG_FOLD_KEY) !== "0");
