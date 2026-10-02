@echo off
setlocal
cd /d "%~dp0"
title AI Doc Studio

rem --- 1. Make sure Ollama is running -------------------------------------
set "OLLAMA_URL=http://127.0.0.1:11434/api/version"

call :ollama_up
if not errorlevel 1 (
  echo [ok] Ollama is already running.
  goto start_app
)

where ollama >nul 2>&1
if errorlevel 1 (
  if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
    set "PATH=%LOCALAPPDATA%\Programs\Ollama;%PATH%"
  ) else (
    echo [warn] Ollama is not installed or not on PATH. Starting without AI features.
    goto start_app
  )
)

echo [..] Ollama is not running. Starting it...
start "Ollama" /min ollama serve

set /a tries=0
:wait_ollama
call :ollama_up
if not errorlevel 1 (
  echo [ok] Ollama is up.
  goto start_app
)
set /a tries+=1
if %tries% geq 30 (
  echo [warn] Ollama did not respond after 30 s. Starting the app anyway.
  goto start_app
)
timeout /t 1 /nobreak >nul
goto wait_ollama

rem --- 2. Start the application -------------------------------------------
:start_app
if not exist "backend\.venv\Scripts\python.exe" (
  echo [error] backend\.venv not found. Set it up first, see README.md.
  pause
  exit /b 1
)
echo [..] Starting AI Doc Studio...
backend\.venv\Scripts\python.exe -m adstudio.main %*
if errorlevel 1 pause
exit /b %errorlevel%

:ollama_up
curl -s -f -m 2 "%OLLAMA_URL%" >nul 2>&1
exit /b %errorlevel%
