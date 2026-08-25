@echo off
rem Helm autostart: start damselfish + SearXNG (via start-all.sh, idempotent).
rem Helm root = parent dir of this .bat.
cd /d "%~dp0\.."

rem Find Git Bash bash.exe (common locations).
set "BASH="
if exist "D:\APP\Git\usr\bin\bash.exe" set "BASH=D:\APP\Git\usr\bin\bash.exe"
if "%BASH%"=="" if exist "%ProgramFiles%\Git\usr\bin\bash.exe" set "BASH=%ProgramFiles%\Git\usr\bin\bash.exe"
if "%BASH%"=="" if exist "%ProgramFiles(x86)%\Git\usr\bin\bash.exe" set "BASH=%ProgramFiles(x86)%\Git\usr\bin\bash.exe"
if "%BASH%"=="" (
  echo [autostart] bash.exe not found in common Git locations 1>&2
  exit /b 1
)

"%BASH%" -c "bash scripts/start-all.sh >> data/start-all.log 2>&1"
