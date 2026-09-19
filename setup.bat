@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHON=%~dp0.venv\Scripts\python.exe"
set "BOOTSTRAP=%~dp0.python\cpython-3.12.14-windows-x86_64-none\python.exe"

if not exist "config.json" if exist "config.example.json" copy /y "config.example.json" "config.json" >nul

if exist "%BOOTSTRAP%" goto bootstrap
where py >nul 2>nul
if not errorlevel 1 goto py
where python >nul 2>nul
if not errorlevel 1 goto python
goto no_python

:bootstrap
"%BOOTSTRAP%" -m venv --clear ".venv"
if errorlevel 1 goto failed
goto install

:py
py -3 -m venv --clear ".venv"
if errorlevel 1 goto failed
goto install

:python
python -m venv --clear ".venv"
if errorlevel 1 goto failed
goto install

:install
"%PYTHON%" -m pip install -r "requirements.txt"
if errorlevel 1 goto failed
"%PYTHON%" -c "import requests" >nul 2>nul
if errorlevel 1 goto failed
echo.
echo Environment is ready.
echo.
pause
exit /b 0

:no_python
echo.
echo Python was not found. Install Python 3.11 or newer first.
echo.
pause
exit /b 1

:failed
echo.
echo Environment setup failed.
echo.
pause
exit /b 1
