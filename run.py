"""
run.py — HPWD Data Manager 진입점
- python run.py: 개발 모드 (subprocess + 자동 재시작)
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

PORT = find_free_port(8001)
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
    # sys.path에 EXE 위치 추가 (모듈 import용)
    if BASE not in sys.path:
        sys.path.insert(0, BASE)


def open_browser():
    time.sleep(2.0)
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    print(f"\n{'='*50}")
    print(f"  HPWD Data Manager — http://localhost:{PORT}")
    print(f"  {'[EXE]' if IS_FROZEN else '[Python]'}  (포트 {PORT} 사용)")
    if PORT != 8001:
        print(f"  ※ 8001이 사용 중이라 {PORT}로 자동 변경됨")
    print(f"{'='*50}\n")

    threading.Thread(target=open_browser, daemon=True).start()

    if IS_FROZEN:
        # EXE: 직접 uvicorn 실행 (subprocess 불가)
        import uvicorn
        uvicorn.run("server:app", host="127.0.0.1", port=PORT, log_level="info")
    else:
        # 개발 모드: subprocess + 자동 재시작
        import subprocess
        while True:
            proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "server:app",
                 "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "info"],
                cwd=os.path.dirname(os.path.abspath(__file__))
            )
            proc.wait()
            print("\n🔄 서버 재시작 중...\n")
            time.sleep(2)
