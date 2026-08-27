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

REQUIRED = [
    ("fastapi",   "fastapi>=0.104"),
    ("uvicorn",   "uvicorn[standard]>=0.24"),
    ("multipart", "python-multipart>=0.0.6"),
    ("pandas",    "pandas>=2.0"),
    ("numpy",     "numpy>=1.24"),
    ("CoolProp",  "CoolProp>=6.4"),
    ("dateutil",  "python-dateutil>=2.8"),
    ("yaml",      "PyYAML>=6.0"),
    ("openpyxl",  "openpyxl>=3.1"),
    ("xlrd",      "xlrd>=2.0"),
    ("scipy",     "scipy>=1.11"),
]


def _pip(*args):
    """pip 실행 — 실패 시 --user, 사내망 SSL 우회 순으로 재시도."""
    import subprocess
    base = [sys.executable, "-m", "pip", "install", *args]
    trusted = ["--trusted-host", "pypi.org",
               "--trusted-host", "files.pythonhosted.org",
               "--trusted-host", "pypi.python.org"]

    attempts = [
        (base, None),
        (base + ["--user"], "--user 옵션으로 재시도..."),
        (base + trusted, "사내망 SSL 우회(trusted-host)로 재시도..."),
        (base + trusted + ["--user"], "trusted-host + --user 로 재시도..."),
    ]
    for cmd, msg in attempts:
        if msg:
            print(f"   → {msg}")
        if subprocess.run(cmd).returncode == 0:
            return True
    return False


def ensure_deps():
    """필수 패키지 확인 후 없는 것만 자동 설치. EXE 모드는 건너뜀."""
    if getattr(sys, "frozen", False):
        return

    import importlib.util
    missing = [(m, spec) for m, spec in REQUIRED
               if importlib.util.find_spec(m) is None]
    if not missing:
        return

    print("\n" + "=" * 56)
    print(f"  누락된 패키지 {len(missing)}개 설치 (최초 1회, 2~3분)")
    print("  " + ", ".join(m for m, _ in missing))
    print("=" * 56 + "\n")

    _pip("--upgrade", "pip")   # wheel 설치 실패 방지

    failed = []
    for mod, spec in missing:
        print(f"\n[설치] {spec}")
        if not _pip(spec):
            failed.append(spec)
            continue
        importlib.invalidate_caches()
        if importlib.util.find_spec(mod) is None:
            failed.append(spec)

    if not failed:
        print("\n[설치] 완료\n")
        return

    print("\n" + "!" * 56)
    print("  다음 패키지 설치에 실패했습니다:")
    for spec in failed:
        print(f"    - {spec}")
    if any(s.startswith("CoolProp") for s in failed):
        v = f"{sys.version_info.major}.{sys.version_info.minor}"
        print("\n  ※ CoolProp은 Python 3.13+ 에서 wheel이 제공되지 않습니다.")
        print(f"     현재 Python {v}  →  3.11 사용을 권장합니다.")
        print("     https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe")
    print("\n  수동 설치: python -m pip install -r requirements.txt")
    print("!" * 56 + "\n")
    input("계속하려면 Enter (오류가 날 수 있음)...")


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
    ensure_deps()

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
