@echo off
chcp 65001 >nul 2>&1
title HPWD - 방화벽 허용 (관리자 권한 필요)

:: 관리자 권한 확인
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo ══════════════════════════════════════════
    echo   관리자 권한이 필요합니다!
    echo ══════════════════════════════════════════
    echo.
    echo   이 파일을 마우스 오른쪽 클릭 →
    echo   "관리자 권한으로 실행"을 선택하세요.
    echo.
    pause
    exit /b 1
)

echo ══════════════════════════════════════════
echo   HPWD 서버 방화벽 규칙 추가
echo ══════════════════════════════════════════
echo.
echo 포트 8001~8010 TCP 인바운드 허용 규칙을 추가합니다...
echo.

netsh advfirewall firewall delete rule name="HPWD Data Manager" >nul 2>&1
netsh advfirewall firewall add rule name="HPWD Data Manager" dir=in action=allow protocol=TCP localport=8001-8010

if %errorlevel% equ 0 (
    echo.
    echo [완료] 방화벽 규칙이 추가되었습니다.
    echo   이제 다른 PC에서 이 서버에 접속할 수 있습니다.
    echo   (START_SERVER.bat 으로 서버 실행)
) else (
    echo.
    echo [오류] 규칙 추가 실패. 수동으로 Windows Defender 방화벽에서
    echo   포트 8001 인바운드 허용을 설정하세요.
)
echo.
pause
