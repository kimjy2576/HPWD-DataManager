@echo off
chcp 65001 >nul 2>&1
title HPWD Data Manager - Server

echo ══════════════════════════════════════════
echo   HPWD Data Manager  (서버 모드)
echo ══════════════════════════════════════════
echo.

rem ── 배치 파일 위치로 이동 (UNC 경로도 pushd로 처리) ──
pushd "%~dp0" 2>nul
if errorlevel 1 goto NOCD

if not exist "run.py" goto NOFILE

echo   작업 폴더: %CD%
echo.

rem ── 1. Python ──
python --version >nul 2>&1
if errorlevel 1 goto NOPY
python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 goto OLDPY
for /f "delims=" %%v in ('python -c "import sys;print(sys.version.split()[0])"') do echo [1/3] Python %%v
echo.

rem ── 2. 패키지 ──
echo [2/3] 패키지 확인 중...
python -c "import fastapi,uvicorn,pandas,numpy,CoolProp,yaml,scipy,openpyxl,xlrd" >nul 2>&1
if not errorlevel 1 goto PKGOK
echo       설치 중... (최초 1회, 2~3분)
if exist "requirements.txt" goto PIPREQ
python -m pip install fastapi uvicorn[standard] pyyaml pandas numpy CoolProp scipy openpyxl xlrd --quiet
goto PIPDONE
:PIPREQ
python -m pip install -r requirements.txt --quiet
:PIPDONE
if errorlevel 1 goto PIPFAIL
echo       설치 완료
goto PKGDONE
:PKGOK
echo       확인됨
:PKGDONE
echo.

rem ── 3. 방화벽 ──
netsh advfirewall firewall show rule name="HPWD Data Manager" >nul 2>&1
if not errorlevel 1 goto FWOK
net session >nul 2>&1
if errorlevel 1 goto FWNOADMIN
netsh advfirewall firewall add rule name="HPWD Data Manager" dir=in action=allow protocol=TCP localport=8001-8010 >nul 2>&1
echo [3/3] 방화벽 규칙 추가됨 (포트 8001~8010)
goto FWDONE
:FWNOADMIN
echo [3/3] 방화벽 규칙 없음 (관리자 권한 아님)
echo       - 방화벽 팝업이 뜨면 [액세스 허용] 클릭
echo       - 또는 이 파일 우클릭 → "관리자 권한으로 실행" (최초 1회)
goto FWDONE
:FWOK
echo [3/3] 방화벽 규칙 확인됨
:FWDONE
echo.

echo ══════════════════════════════════════════
echo   "다른 PC에서 접속" 주소를 공유하세요
echo   종료: 이 창을 닫거나 Ctrl+C
echo ══════════════════════════════════════════
echo.

python run.py --share

echo.
echo [서버 종료됨]
popd
pause
exit /b 0

:NOCD
echo [오류] 폴더 이동 실패: %~dp0
echo   네트워크 드라이브면 로컬 디스크로 복사 후 실행하세요.
pause
exit /b 1

:NOFILE
echo [오류] run.py 를 찾을 수 없습니다.
echo   현재 위치: %CD%
echo   run.bat 을 run.py 와 같은 폴더에 두세요.
popd
pause
exit /b 1

:NOPY
echo [오류] Python이 설치되어 있지 않거나 PATH에 없습니다.
echo   https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
echo   설치 시 "Add Python to PATH" 체크 필요
popd
pause
exit /b 1

:OLDPY
echo [오류] Python 3.10 이상이 필요합니다.
python --version
popd
pause
exit /b 1

:PIPFAIL
echo [오류] 패키지 설치 실패
echo   수동 실행: python -m pip install -r requirements.txt
popd
pause
exit /b 1
