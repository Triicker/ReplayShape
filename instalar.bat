@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==============================================================
echo  REPLAY SHAPE - instalacao (Windows)
echo ==============================================================

where python >nul 2>nul
if errorlevel 1 (
  echo [XX] Python nao encontrado.
  echo      Instale o Python 3.12 e marque "Add python.exe to PATH":
  echo      winget install Python.Python.3.12
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Criando ambiente Python...
  python -m venv .venv || (echo [XX] Falha ao criar o ambiente & pause & exit /b 1)
)
echo Instalando dependencias...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r backend\requirements.txt || (echo [XX] Falha ao instalar & pause & exit /b 1)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo [!!] FFmpeg nao encontrado. Tentando instalar com o winget...
  winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
  echo      Feche esta janela e abra de novo para o Windows reconhecer o FFmpeg.
) else (
  echo [OK] FFmpeg encontrado.
)

where cloudflared >nul 2>nul
if errorlevel 1 (
  echo.
  echo Opcional: para acessar pelo 4G com link HTTPS, instale o cloudflared:
  echo      winget install Cloudflare.cloudflared
)

echo.
echo [OK] Instalacao concluida. Proximo passo: configurar_camera.bat
pause
