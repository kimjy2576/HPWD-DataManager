"""
run.py — HPWD Data Manager 진입점
- python run.py            : 로컬 전용 (127.0.0.1, 본인 PC만 접속)
- python run.py --share    : 공유 모드 (0.0.0.0, 같은 네트워크의 다른 PC 접속 가능)
- 환경변수 HPWD_HOST 로도 지정 가능 (예: set HPWD_HOST=0.0.0.0)
- EXE: PyInstaller 번들 (직접 실행)
"""
import sys
import os
import webbrowser
import threading
import time
import socket

def find_free_port(start=8001, tries=20):
    """start부터 빈 포트를 찾아 반환. 모두 사용 중이면 start 반환."""
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:  # 연결 실패 = 빈 포트
                return p
    return start

def get_local_ip():
    """이 PC의 LAN IP 주소 반환 (다른 PC가 접속할 주소)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # 실제 전송 안 함, 라우팅용
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

# --- 실행 옵션 결정 ---
# 우선순위: 커맨드라인 인자 > 환경변수 > 기본값(로컬)
SHARE = ("--share" in sys.argv) or (os.environ.get("HPWD_HOST", "") == "0.0.0.0")
HOST = "0.0.0.0" if SHARE else "127.0.0.1"

PORT = find_free_port(8001)
LOCAL_IP = get_local_ip()
IS_FROZEN = getattr(sys, "frozen", False)

# PyInstaller 번들 경로 보정
if IS_FROZEN:
    BASE = os.path.dirname(sys.executable)
    os.chdir(BASE)
    os.makedirs(os.path.join(BASE, "results"), exist_ok=True)
    os.makedirs(os.path.join(BASE, "config", "saves", "merge"), exist_ok=True)
    os.makedirs(os.path.join(BASE, "config", "saves", "calc"), exist_ok=True)
    os.makedirs(os.path.join(BASE, "config", "saves", "formula"), exist_ok=True)
    os.makedirs(os.path.join(BASE, "config", "saves", "viewer"), exist_ok=True)
    if BASE not in sys.path:
        sys.path.insert(0, BASE)


def open_browser():
    time.sleep(2.0)
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    print(f"\n{'='*56}")
    print(f"  HPWD Data Manager  {'[EXE]' if IS_FROZEN else '[Python]'}")
    print(f"{'='*56}")
    print(f"  이 PC에서 접속:   http://localhost:{PORT}")
    if SHARE:
        print(f"  다른 PC에서 접속: http://{LOCAL_IP}:{PORT}")
        print(f"  ───────────────────────────────────────────")
        print(f"  ※ 공유 모드 (0.0.0.0 바인딩)")
        print(f"  ※ 다른 PC는 같은 네트워크(사내망/공유기)에 있어야 함")
        print(f"  ※ 최초 1회 Windows 방화벽 '액세스 허용' 팝업 → 허용 클릭")
    else:
        print(f"  ───────────────────────────────────────────")
        print(f"  ※ 로컬 모드 (이 PC에서만 접속 가능)")
        print(f"  ※ 다른 PC에서 접속하려면: python run.py --share")
    if PORT != 8001:
        print(f"  ※ 8001이 사용 중이라 {PORT}로 자동 변경됨")
    print(f"{'='*56}\n")

    threading.Thread(target=open_browser, daemon=True).start()

    if IS_FROZEN:
        import uvicorn
        uvicorn.run("server:app", host=HOST, port=PORT, log_level="info")
    else:
        import subprocess
        while True:
            proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "server:app",
                 "--host", HOST, "--port", str(PORT), "--log-level", "info"],
                cwd=os.path.dirname(os.path.abspath(__file__))
            )
            proc.wait()
            print("\n[재시작] 서버 재시작 중...\n")
            time.sleep(2)
