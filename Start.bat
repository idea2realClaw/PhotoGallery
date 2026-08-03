@echo off
chcp 65001 >nul
title PhotoGallery WebUI

REM ============================================================
REM Use the native ARM64 Python venv. PhotoGallery needs flask+Pillow+numpy,
REM and (optionally, for face indexing) insightface.
REM Most-suitable existing ARM64 venv on this machine (has all of them):
REM   D:\DiskD\GitHub\VideoSR\venv
REM Fall back to system "python" if that venv is absent.
REM ============================================================
SET "PYTHON=D:\DiskD\GitHub\VideoSR\venv\Scripts\python.exe"
IF NOT EXIST "%PYTHON%" SET "PYTHON=python"

echo ============================================
echo   PhotoGallery WebUI 启动器
echo   首页: http://127.0.0.1:2026/
echo   后台日志会持续输出在下方窗口
echo ============================================
echo.

REM 进入脚本所在目录
cd /d "%~dp0"

REM 若依赖未安装则自动安装（app.py 需要 flask+Pillow+numpy，人脸索引需要 insightface，可选 pyftpdlib 用于 FTP 共享）
"%PYTHON%" -c "import flask, PIL, numpy, insightface" 2>nul
if errorlevel 1 (
    echo [安装依赖 flask + Pillow + numpy + insightface + pyftpdlib ...]
    "%PYTHON%" -m pip install flask pillow numpy insightface pyftpdlib
)

echo [启动中 ... 按 Ctrl+C 停止]
echo.
"%PYTHON%" app.py
echo.
echo 服务已停止。按任意键关闭窗口。
pause >nul
