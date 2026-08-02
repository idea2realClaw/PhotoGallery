"use strict";

const $ = (sel) => document.querySelector(sel);

const els = {
  pathInput: $("#pathInput"),
  scanBtn: $("#scanBtn"),
  status: $("#status"),
  logbody: $("#logbody"),
  clearLog: $("#clearLog"),
  shareBox: $("#shareBox"),
  ftpUrl: $("#ftpUrl"),
  shareUrl: $("#shareUrl"),
  copyFtp: $("#copyFtp"),
  copyShare: $("#copyShare"),
  netLinks: $("#netLinks"),
  netFtp: $("#netFtp"),
  netHttp: $("#netHttp"),
  copyNetFtp: $("#copyNetFtp"),
  copyNetHttp: $("#copyNetHttp"),
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
  try {
    const res = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, dirName: "" }),
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
    els.status.textContent = "❌ 无法连接后台。";
    reportEvent("扫描时网络错误");
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

// ---------- Init ----------
reportEvent("WebUI 首页已加载");
connectLog();
loadNetLinks();
