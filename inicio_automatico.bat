@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Liga/desliga o inicio automatico do piloto quando o usuario entra no Windows.
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LINK=%STARTUP%\ReplayShape-piloto.bat"
if exist "%LINK%" (
  del "%LINK%"
  echo [OK] Inicio automatico DESLIGADO.
) else (
  > "%LINK%" echo @echo off
  >> "%LINK%" echo cd /d "%~dp0"
  >> "%LINK%" echo start "Replay Shape - piloto" /min cmd /c iniciar_piloto.bat --repetir
  echo [OK] Inicio automatico LIGADO: o piloto abre sozinho ao entrar no Windows,
  echo      usando a ultima forma de acesso escolhida.
  echo      Rode este arquivo de novo para desligar.
)
pause
