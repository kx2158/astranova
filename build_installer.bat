@echo off
rem Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
setlocal EnableExtensions
cd /d "%~dp0"
if defined CI set "AN_NEUTRAL=1"
rem Build from a neutral drive letter so no folder path (which contains your Windows user name) ends up in the
rem built program files.
if not defined AN_NEUTRAL (
  set "AN_NEUTRAL=1"
  for %%L in (R S T U V W Q P) do (
    if not exist "%%L:\" (
      subst %%L: "%~dp0." >nul 2>nul && (
        call "%%L:\build_installer.bat"
        subst %%L: /d >nul 2>nul
        exit /b
      )
    )
  )
  echo Couldn't map a neutral drive letter, building from this folder instead.
)
cd /d "%~dp0"
rem Builds the public AstraNova release:
rem   release\AstraNova-Setup.exe          the installer people run (no .bat, no Python needed on their PC)
rem   release\AstraNova-<version>.zip      the same installer zipped, ready to share
set "APPNAME=AstraNova"
title Building the AstraNova installer

set "VENV=%~dp0.venv"
set "VPY=%~dp0.venv\Scripts\python.exe"

rem ---------------------------------------------------------------------------
rem 1. Find a supported Python (3.10 - 3.13). Prefer 3.12.
rem ---------------------------------------------------------------------------
set "BASEPY="
for %%v in (3.12 3.13 3.11 3.10) do (
  if not defined BASEPY (
    py -%%v -c "import sys" >nul 2>nul && set "BASEPY=py -%%v"
  )
)
if not defined BASEPY (
  python -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)" >nul 2>nul && set "BASEPY=python"
)
if not defined BASEPY (
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set BASEPY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not defined BASEPY (
  where winget >nul 2>nul && (
    echo No supported Python found. Installing Python 3.12 with winget...
    winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set BASEPY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  )
)
if not defined BASEPY (
  echo.
  echo AstraNova's build needs Python 3.12 or 3.13 ^(3.14 is not supported yet by the window library^).
  echo Download it from https://www.python.org/downloads/windows/
  echo During setup tick "Add python.exe to PATH", then run build.bat again.
  if not defined CI pause
  exit /b 1
)
echo Using Python: %BASEPY%

rem ---------------------------------------------------------------------------
rem 2. Project environment: reuse only if it works AND belongs to this folder.
rem ---------------------------------------------------------------------------
set "NEEDVENV=1"
if exist "%VPY%" (
  "%VPY%" -c "import sys, os; ok = os.path.normcase(os.path.abspath(sys.prefix)) == os.path.normcase(os.path.abspath(r'%VENV%')) and (3,10) <= sys.version_info[:2] <= (3,13); sys.exit(0 if ok else 1)" >nul 2>nul && set "NEEDVENV=0"
)
if "%NEEDVENV%"=="1" (
  echo [1/6] Creating a fresh Python environment...
  if exist "%VENV%" rmdir /s /q "%VENV%"
  %BASEPY% -m venv "%VENV%"
)
if not exist "%VPY%" (
  echo Could not create the Python environment in "%VENV%".
  goto :fail
)
if "%NEEDVENV%"=="0" echo [1/6] Python environment OK.

rem ---------------------------------------------------------------------------
rem 3. Packages (always through the environment's own python - no PATH needed)
rem ---------------------------------------------------------------------------
echo [2/6] Installing packages - the first time takes a few minutes...
"%VPY%" -m pip install --upgrade pip >nul 2>nul
"%VPY%" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :fail
"%VPY%" -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  echo PyInstaller did not install correctly.
  goto :fail
)

rem Generate the Windows UI Automation bindings now, so they are bundled into the exe.
echo [3/6] Preparing Windows UI Automation...
"%VPY%" -c "import sys; sys.coinit_flags=0; import pywinauto.uia_defines, comtypes.gen.UIAutomationClient; print('UI Automation ready')"
if errorlevel 1 goto :fail

rem ---------------------------------------------------------------------------
rem 4. Build the app
rem ---------------------------------------------------------------------------
echo [4/6] Building AstraNova.exe...
taskkill /im AstraNova.exe /f >nul 2>nul
for /d /r astra %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d"
if exist build rmdir /s /q build
if exist "dist\AstraNova" rmdir /s /q "dist\AstraNova"
"%VPY%" -m PyInstaller --noconfirm --clean AstraNova.spec
if errorlevel 1 goto :fail
if not exist "dist\AstraNova\AstraNova.exe" (
  echo The build finished but dist\AstraNova\AstraNova.exe is missing.
  goto :fail
)
if defined CI goto :skipselftest
call :sign "dist\AstraNova\AstraNova.exe"
if errorlevel 1 goto :fail
start "" /wait "dist\AstraNova\AstraNova.exe" --selftest
set "ST=%errorlevel%"
if exist "dist\AstraNova\selftest.txt" type "dist\AstraNova\selftest.txt"
echo.
if not "%ST%"=="0" (
  echo The self-test failed. Send the text above to get it fixed.
  goto :fail
)
del /q "dist\AstraNova\selftest.txt" >nul 2>nul
:skipselftest

rem ---------------------------------------------------------------------------
rem 5. Installer: a small uninstaller goes inside the app, then the setup carries the whole app
rem ---------------------------------------------------------------------------
echo [5/6] Building the installer...
"%VPY%" -c "import astra; open('build/version.txt','w').write(astra.__version__)"
"%VPY%" installer\make_version_info.py build\version_setup.txt "AstraNova Setup" >nul
"%VPY%" installer\make_version_info.py build\version_uninst.txt "AstraNova Uninstaller" >nul
"%VPY%" -m PyInstaller --noconfirm --onefile --windowed --name uninstall --icon "%~dp0assets\astra.ico" --version-file "%~dp0build\version_uninst.txt" --distpath build\uninst --workpath build\uninst-work --specpath build ^
  --add-data "%~dp0installer\setup.html;." --add-data "%~dp0build\version.txt;." --hidden-import webview installer\setup_app.py
if errorlevel 1 goto :fail
call :sign "build\uninst\uninstall.exe"
if errorlevel 1 goto :fail
copy /y "build\uninst\uninstall.exe" "dist\AstraNova\uninstall.exe" >nul
echo Packing the app...
"%VPY%" -c "import zipfile,os; z=zipfile.ZipFile('build/payload.zip','w',zipfile.ZIP_DEFLATED,compresslevel=6); r='dist/AstraNova'; [z.write(os.path.join(d,f), os.path.relpath(os.path.join(d,f), r)) for d,_,fs in os.walk(r) for f in fs]; z.close()"
if errorlevel 1 goto :fail
rem A small picture shows straight away while the onefile setup unpacks itself, so it never looks frozen.
set "SPLASH="
"%VPY%" -c "import tkinter" >nul 2>nul && set "SPLASH=--splash "%~dp0assets\setup-splash.png""
"%VPY%" -m PyInstaller --noconfirm --onefile --windowed %SPLASH% --name AstraNova-Setup --icon "%~dp0assets\astra.ico" --version-file "%~dp0build\version_setup.txt" --distpath release --workpath build\setup-work --specpath build ^
  --add-data "%~dp0installer\setup.html;." --add-data "%~dp0build\version.txt;." --add-data "%~dp0build\payload.zip;." --hidden-import webview installer\setup_app.py
if errorlevel 1 goto :fail

call :sign "release\AstraNova-Setup.exe"
if errorlevel 1 goto :fail

rem ---------------------------------------------------------------------------
rem 6. Zip it for sharing
rem ---------------------------------------------------------------------------
echo [6/6] Zipping the release...
"%VPY%" -c "import zipfile,astra; n='release/AstraNova-'+astra.__version__+'.zip'; z=zipfile.ZipFile(n,'w',zipfile.ZIP_STORED); z.write('release/AstraNova-Setup.exe','AstraNova-Setup.exe'); z.writestr('Read me.txt', open('installer/READ_ME.txt',encoding='utf-8').read()); z.close(); print('  '+n)"
if errorlevel 1 goto :fail

echo.
echo Done.
echo   Installer: release\AstraNova-Setup.exe
echo   To share:  the AstraNova zip in the release folder
if defined CI exit /b 0
explorer release
pause
exit /b 0

:sign
rem Signs a program as AIXENI when a code-signing certificate is set up (GitHub secrets SIGN_PFX + SIGN_PFX_PASSWORD,
rem or set SIGN_PFX to a .pfx file yourself). Without one this does nothing.
if not defined SIGN_PFX exit /b 0
set "SIGNTOOL="
for /f "delims=" %%S in ('where signtool 2^>nul') do if not defined SIGNTOOL set "SIGNTOOL=%%S"
if not defined SIGNTOOL for /f "delims=" %%S in ('where /r "%ProgramFiles(x86)%\Windows Kits\10\bin" signtool.exe 2^>nul ^| findstr /i "\\x64\\"') do set "SIGNTOOL=%%S"
if not defined SIGNTOOL (echo signtool not found, skipping signing & exit /b 0)
"%SIGNTOOL%" sign /f "%SIGN_PFX%" /p "%SIGN_PFX_PASSWORD%" /fd sha256 /tr http://timestamp.digicert.com /td sha256 /d "AstraNova" /du "https://aixeni.xyz/" "%~1"
if errorlevel 1 (echo Signing %~1 failed & exit /b 1)
echo   signed %~1
exit /b 0

:fail
echo.
echo Build failed - see the messages above.
if not defined CI pause
exit /b 1
