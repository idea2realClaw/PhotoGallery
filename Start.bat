@echo off
chcp 65001 >nul
title PhotoGallery WebUI
echo ============================================
echo   PhotoGallery WebUI 启动器
echo   首页: http://127.0.0.1:2026/
echo   后台日志会持续输出在下方窗口
echo ============================================
echo.

REM 进入脚本所在目录
cd /d "%~dp0"

REM 若依赖未安装则自动安装
python -c "import flask, PIL" 2>nul
if errorlevel 1 (
    echo [安装依赖 flask + Pillow ...]
    python -m pip install flask pillow
)

echo [启动中 ... 按 Ctrl+C 停止]
echo.
python app.py
echo.
echo 服务已停止。按任意键关闭窗口。
pause >nul
