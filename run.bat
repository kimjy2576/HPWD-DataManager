@echo off
chcp 65001 >nul 2>&1
title HPWD Data Manager - Server
cd /d "%~dp0"

echo ══════════════════════════════════════════
echo   HPWD Data Manager  (서버 모드)
echo   다른 PC에서 접속 가능
echo ══════════════════════════════════════════
echo.

:: ── 1. Python 확인 ──
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [오류] Python이 설치되어 있지 않거나 PATH에 없습니다.
    echo   Python 3.11 설치 후 "Add Python to PATH" 체크 필요
    echo   https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
    echo.
    pause
    exit /b 1
)
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
    set MAJOR=%%a
    set MINOR=%%b
)
if %MINOR% LSS 10 (
    echo [오류] Python 3.10 이상 필요 ^(현재 %PYVER%^)
    pause
    exit /b 1
)
echo [1/3] Python %PYVER%
echo.

:: ── 2. 패키지 확인 ──
echo [2/3] 패키지 확인 중...
python -c "import fastapi, uvicorn, pandas, numpy, CoolProp, yaml, scipy, openpyxl, xlrd" >nul 2>&1
if %errorlevel% neq 0 (
    echo       설치 중... ^(최초 1회, 2~3분^)
    if exist requirements.txt (
        python -m pip install -r requirements.txt --quiet
    ) else (
        python -m pip install fastapi uvicorn[standard] pyyaml pandas numpy CoolProp scipy openpyxl xlrd --quiet
    )
    if %errorlevel% neq 0 (
        echo [오류] 패키지 설치 실패
        echo   수동 실행: python -m pip install -r requirements.txt
        pause
        exit /b 1
    )
    echo       설치 완료
) else (
    echo       확인됨
)
echo.

:: ── 3. 방화벽 규칙 ──
netsh advfirewall firewall show rule name="HPWD Data Manager" >nul 2>&1
if %errorlevel% neq 0 (
    net session >nul 2>&1
    if %errorlevel% equ 0 (
        netsh advfirewall firewall add rule name="HPWD Data Manager" dir=in action=allow protocol=TCP localport=8001-8010 >nul 2>&1
        echo [3/3] 방화벽 규칙 추가됨 ^(포트 8001~8010^)
    ) else (
        echo [3/3] 방화벽 규칙 없음 ^(관리자 권한 아님^)
        echo       - 실행 후 Windows 방화벽 팝업에서 [액세스 허용] 클릭
        echo       - 또는 이 파일을 우클릭 → "관리자 권한으로 실행" ^(최초 1회^)
    )
) else (
    echo [3/3] 방화벽 규칙 확인됨
)
echo.

echo ══════════════════════════════════════════
echo   아래 "다른 PC에서 접속" 주소를 공유하세요
echo   종료: 이 창을 닫거나 Ctrl+C
echo ══════════════════════════════════════════
echo.

python run.py --share

echo.
echo [서버가 종료되었습니다]
pause
