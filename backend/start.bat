@echo off
rem ============================================
rem 妙舆 启动/重启脚本
rem 用法：双击运行。会先杀掉占用 5000 端口的旧服务，再启动新服务。
rem 改完 .env 后运行一次本脚本即可生效（无需手动杀进程）。
rem ============================================
echo [1/2] 停止旧服务（占用 5000 端口的进程）...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":5000" ^| findstr LISTENING') do (
    taskkill /F /PID %%p >nul 2>&1
)
timeout /t 1 /nobreak >nul

echo [2/2] 启动新服务...
cd /d "%~dp0"
start "YuQing Service" python app.py --port 5000
timeout /t 3 /nobreak >nul
echo.
echo 服务已启动：http://localhost:5000  （局域网设备：http://本机IP:5000）
echo 本窗口可关闭，服务在后台运行。
pause
