@echo off
setlocal
cd /d "%~dp0"
title OpenPlag

if exist .venv\Scripts\python.exe goto run

set "PY="
rem Prefer well-supported versions first, then whatever the launcher has
for %%V in (3.12 3.13 3.11 3.14 3.10) do (
  if not defined PY ( py -%%V --version >nul 2>&1 && set "PY=py -%%V" )
)
if not defined PY ( py -3 --version >nul 2>&1 && set "PY=py -3" )
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo.
  echo  Python 3.10 or newer is not installed.
  echo.
  echo  1. Download it from https://www.python.org/downloads/
  echo  2. In the installer, tick "Add python.exe to PATH", then Install Now.
  echo  3. Close this window and double-click run.bat again.
  echo.
  echo  Or run in Command Prompt:  winget install Python.Python.3.13
  echo.
  pause
  exit /b 1
)

echo Setting up OpenPlag for the first time (about a minute)...
%PY% --version
%PY% -m venv .venv || (echo Could not create the virtual environment. & pause & exit /b 1)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt || (echo Installing packages failed - check your internet connection and try again. & rmdir /s /q .venv & pause & exit /b 1)

:run
.venv\Scripts\python app.py
pause
