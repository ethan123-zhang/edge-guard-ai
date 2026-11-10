@echo off
chcp 65001 >nul
rem ============================================================
rem 用“干净环境”启动 DeepSeek Harness：清掉继承来的代理变量
rem 背景：DSH 从父进程继承了 HTTP_PROXY/HTTPS_PROXY=127.0.0.1:7890，
rem       clash 核没跑时模型请求会 5 次 TRANSPORT 后失败。
rem 用法：先彻底退出 DSH（任务管理器确认无 DeepSeek Harness 进程），再双击本脚本。
rem ============================================================

set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "http_proxy="
set "https_proxy="
set "all_proxy="
set "NO_PROXY=localhost,127.0.0.1,::1,api.deepseek.com,maas.qianwenaiapi.com"
set "no_proxy=%NO_PROXY%"

echo 已清空代理环境变量，当前值：
echo   HTTP_PROXY = [%HTTP_PROXY%]
echo   HTTPS_PROXY= [%HTTPS_PROXY%]
echo   NO_PROXY   = [%NO_PROXY%]
echo.
echo 正在启动 DeepSeek Harness ...
start "" "D:\DeepSeek Harness\DeepSeek Harness.exe"
exit /b 0