@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Replay Shape - piloto
if not exist ".venv\Scripts\python.exe" (
  echo Rode instalar.bat primeiro.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\iniciar_piloto.py %*
pause
