@echo off
title HPWD Data Manager - Server

pushd "%~dp0" 2>nul
if errorlevel 1 goto NOCD
if not exist "run.py" goto NOFILE

python --version >nul 2>&1
if errorlevel 1 goto NOPY

python run.py --share
popd
echo.
echo [Server stopped]
pause
exit /b 0

:NOCD
echo [ERROR] Cannot change directory: %~dp0
echo         If this is a network drive, copy to a local disk.
pause
exit /b 1

:NOFILE
echo [ERROR] run.py not found.
echo         Current folder: %CD%
echo         Put run.bat in the same folder as run.py
popd
pause
exit /b 1

:NOPY
echo.
echo [INFO] Python is not installed or not in PATH.
echo.
choice /c YN /n /m "Install Python 3.11 automatically? (Y/N): "
if errorlevel 2 goto NOPYMANUAL
echo.
echo   Downloading installer (about 25MB)...
set "PYEXE=%TEMP%\python-3.11.9-amd64.exe"
curl -L -o "%PYEXE%" https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
if errorlevel 1 goto DLFAIL
if not exist "%PYEXE%" goto DLFAIL
echo   Installing... (2-3 min, window may look frozen)
"%PYEXE%" /passive InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_launcher=1
del /q "%PYEXE%" >nul 2>&1
for /f "skip=2 tokens=2,*" %%a in ('reg query "HKCU\Environment" /v PATH 2^>nul') do set "UPATH=%%b"
if defined UPATH set "PATH=%UPATH%;%PATH%"
python --version >nul 2>&1
if errorlevel 1 goto PYRESTART
echo.
echo   Python installed. Continuing...
echo.
popd
"%~f0"
exit /b 0

:PYRESTART
echo.
echo   Python installed. Please close this window and run run.bat again.
popd
pause
exit /b 0

:DLFAIL
echo.
echo [ERROR] Download failed (network or corporate security policy).
echo         Install manually from:
echo         https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
echo         Check "Add Python to PATH" during setup.
popd
pause
exit /b 1

:NOPYMANUAL
echo.
echo   Install Python 3.11 from:
echo   https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
echo   Check "Add Python to PATH" during setup.
popd
pause
exit /b 1
