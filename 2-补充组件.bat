@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 补充组件
set "PYTHONUTF8=1"
set "LOCALAPPDATA=%~dp0data\appdata"

rem 需要走代理下载时，把地址填在下面（留空则直连）
if not defined FP_PROXY set "FP_PROXY=http://127.0.0.1:3067"

echo.
echo   下载 Camoufox 运行时组件（fpgen 模型 + GeoIP 库）
echo   代理: %FP_PROXY%
echo.

"%~dp0runtime\python.exe" "%~dp0app\fetch_extras.py"
echo.
pause
