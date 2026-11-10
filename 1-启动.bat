@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 指纹环境管理器（开发模式）
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

rem 开发模式入口：启动带窗口的 GUI（托盘 + 内嵌 WebView2），无控制台黑窗。
rem 出错时看 data\logs\error.log；正式使用请直接双击 dist\指纹环境管理器.exe。
rem
rem 注意：不要在这里改 LOCALAPPDATA。上一版改了它，系统 Edge 会把几百 MB
rem 用户配置写进项目目录，还会被误打进安装包。

"%~dp0runtime\pythonw.exe" "%~dp0app\main.py"
