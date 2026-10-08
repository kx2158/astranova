@echo off
rem Run AstraNova from source without building the exe. Run build_installer.bat once first (it sets up .venv).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run build_installer.bat once first - it sets up the Python environment.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" main.py %*
