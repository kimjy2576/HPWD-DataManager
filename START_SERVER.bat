@echo off
chcp 65001 >nul 2>&1
title HPWD Data Manager - 공유 서버 모드

echo ══════════════════════════════════════════
echo   HPWD Data Manager - 공유 서버 모드
echo   (다른 PC에서 접속 가능)
echo ══════════════════════════════════════════
echo.

:: Python 확인
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [오류] Python이 설치되어 있지 않습니다!
    echo   Python 3.11 설치 후 "Add Python to PATH" 체크 필요.
    echo   https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
    echo.
    start https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
    pause
    exit /b 1
)

:: 버전 체크
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
    set MAJOR=%%a
    set MINOR=%%b
)
echo [1/3] Python %PYVER% 확인

if %MINOR% LSS 10 (
    echo [경고] Python 3.10 이상이 필요합니다. (현재: %PYVER%^)
    pause
    exit /b 1
)
echo.

:: 패키지 설치
echo [2/3] 필수 패키지 확인 중...
python -c "import fastapi, uvicorn, pandas, numpy, CoolProp, yaml, scipy, xlrd, openpyxl" >nul 2>&1
if %errorlevel% neq 0 (
    echo       패키지 설치 중... (최초 1회, 2-3분 소요)
    pip install fastapi uvicorn[standard] pyyaml pandas numpy CoolProp scipy openpyxl xlrd --quiet 2>nul
    if %errorlevel% neq 0 (
        echo [오류] 패키지 설치 실패! 수동: pip install -r requirements.txt
        pause
        exit /b 1
    )
    echo       설치 완료!
) else (
    echo       모든 패키지 설치됨 (스킵)
)
echo.

:: 실행 (공유 모드)
echo [3/3] 공유 서버 시작!
echo.
echo ══════════════════════════════════════════
echo   ※ 다른 PC에서 접속하려면:
echo     - 같은 네트워크(사내망/공유기)에 있어야 함
echo     - 아래 로그의 "다른 PC에서 접속" 주소 사용
echo.
echo   ※ 최초 1회 Windows 방화벽 팝업이 뜨면
echo     반드시 [액세스 허용] 클릭!
echo.
echo   종료: 이 창을 닫거나 Ctrl+C
echo ══════════════════════════════════════════
echo.
python run.py --share
pause
