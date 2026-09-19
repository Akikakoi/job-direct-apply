@echo off
rem 简历直达 · 双击启动入口（窗口停留，能看到启动结果）
rem 传参透传：start-all.cmd -Prod / -Stop
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-all.ps1" %*
