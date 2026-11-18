@echo off
title HPWD - Data Diagnose
pushd "%~dp0" 2>nul
if errorlevel 1 goto NOCD

python --version >nul 2>&1
if errorlevel 1 goto NOPY

if "%~1"=="" goto ASK
python tools\diagnose.py %*
goto DONE

:ASK
echo.
echo  Drag and drop an AMS csv / .dat file or a case folder onto diagnose.bat,
echo  or paste the path below and press Enter.
echo.
set "TARGET="
set /p TARGET=Path: 
if not defined TARGET goto DONE
set TARGET=%TARGET:"=%
python tools\diagnose.py "%TARGET%"

:DONE
popd
echo.
pause
exit /b 0

:NOCD
echo [ERROR] Cannot change directory: %~dp0
pause
exit /b 1

:NOPY
echo [ERROR] Python not found. Run run.bat once first.
popd
pause
exit /b 1
