#!/bin/bash
# PhotoGallery WebUI 启动器 (macOS)
# 双击本文件即可在终端中启动，后台日志会持续输出。
cd "$(dirname "$0")" || exit 1

PY=/Users/zhuxiaodong/.workbuddy/binaries/python/envs/default/bin/python
if [ ! -x "$PY" ]; then
  PY=python3
fi

echo "============================================"
echo "  PhotoGallery WebUI 启动器"
echo "  首页: http://127.0.0.1:2026/"
echo "  后台日志会持续输出在下方"
echo "============================================"
echo
echo "[启动中 ... 按 Ctrl+C 停止]"
echo
exec "$PY" photogallery.py
