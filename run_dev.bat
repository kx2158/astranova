@echo off
rem Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
rem Run AstraNova from source without building the exe. Run build_installer.bat once first (it sets up .venv).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run build_installer.bat once first - it sets up the Python environment.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" main.py %*
