@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

set "TOOL_DIR=%~dp0"
set "PYTHON=%TOOL_DIR%\.venv\Scripts\python.exe"
set "BOOTSTRAP=%TOOL_DIR%\.python\cpython-3.12.14-windows-x86_64-none\python.exe"

if not exist "%TOOL_DIR%\pixiv_downloader.py" goto missing
if not exist "%TOOL_DIR%\requirements.txt" goto missing
if not exist "%PYTHON%" goto rebuild
"%PYTHON%" -c "import requests" >nul 2>nul
if errorlevel 1 goto rebuild
goto run

:rebuild
echo.
echo The Python environment is missing or damaged.
echo Rebuilding it now. This may take a few minutes...
echo.
if exist "%BOOTSTRAP%" goto rebuild_bootstrap
where py >nul 2>nul
if not errorlevel 1 goto rebuild_py
where python >nul 2>nul
if not errorlevel 1 goto rebuild_python
goto no_python

:rebuild_bootstrap
"%BOOTSTRAP%" -m venv --clear "%TOOL_DIR%\.venv"
if errorlevel 1 goto failed
goto install

:rebuild_py
py -3 -m venv --clear "%TOOL_DIR%\.venv"
if errorlevel 1 goto failed
goto install

:rebuild_python
python -m venv --clear "%TOOL_DIR%\.venv"
if errorlevel 1 goto failed
goto install

:install
"%PYTHON%" -m pip install -r "%TOOL_DIR%\requirements.txt"
if errorlevel 1 goto failed
"%PYTHON%" -c "import requests" >nul 2>nul
if errorlevel 1 goto failed

:run
pushd "%TOOL_DIR%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
"%PYTHON%" "pixiv_downloader.py" --menu
set "EXITCODE=%ERRORLEVEL%"
popd
echo.
if "%EXITCODE%"=="0" (
    echo Pixiv downloader closed normally.
) else (
    echo Pixiv downloader exited with code %EXITCODE%.
)
echo.
pause
exit /b %EXITCODE%

:no_python
echo.
echo Python was not found.
echo Install Python 3.11 or newer, then run this file again.
echo.
pause
exit /b 1

:missing
echo.
echo Cannot find the downloader files:
echo %TOOL_DIR%
echo.
pause
exit /b 1

:failed
echo.
echo Environment setup failed.
echo Check the network connection and try again.
echo.
pause
exit /b 1
