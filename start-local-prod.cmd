@echo off
rem Start local production mode (SQLite + backend + frontend)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-all.ps1" -UseProductionEnv
