@echo off
chcp 65001 >nul
title AI 控制面板 (Windows 启动器)

echo =======================================================
echo           云微传媒 AI 服务控制面板 (Windows)
echo =======================================================
echo.

:: 切换到当前脚本所在目录
cd /d "%~dp0"

:: 净化系统代理环境变量（防止 Clash/VPN 劫持 127.0.0.1）
set HTTP_PROXY=
set HTTPS_PROXY=
set http_proxy=
set https_proxy=
set ALL_PROXY=
set all_proxy=
set no_proxy=*
set NO_PROXY=*

:: 检查 Python 是否已安装
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 3.10+ 并勾选 "Add Python to PATH"
    pause
    exit /b 1
)

:: 检查 9090 端口是否被占用，如有旧进程则清理
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":9090" ^| findstr "LISTENING"') do (
    echo [提示] 释放正在占用 9090 端口的旧进程 (PID: %%a)...
    taskkill /F /PID %%a >nul 2>&1
)

echo [1/2] 正在后台启动控制面板服务 (0.0.0.0:9090)...
start /B python server.py > "%TEMP%\ai-dashboard.log" 2>&1

:: 等待 2 秒等待服务就绪
timeout /t 2 /nobreak >nul

echo [2/2] 正在唤起默认浏览器打开控制台...
start http://localhost:9090

echo.
echo =======================================================
echo  控制面板已成功启动！
echo  - 本地访问:   http://localhost:9090
echo  - 局域网访问: 手机/平板同WiFi下可直接访问对应局域网IP
echo =======================================================
echo.
