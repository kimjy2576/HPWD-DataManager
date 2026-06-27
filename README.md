# HPWD Data Manager

히트펌프 건조기 실험 데이터 통합 분석 프로그램

BR (BlackRose) + AMS + MX100 + NIDAQ 데이터를 병합하고, 열역학 계산을 수행한 뒤,
개별선도 / 습공기 선도 / P-h 선도 / 성능 비교로 시각화합니다.

> **저장소: `kimjy2576/HPWD-DataManager` (비공개)**
> **기본 포트: 8001** (사용 중이면 8002~ 자동 변경)

---

# 빠른 선택 가이드

내 상황에 맞는 항목으로 바로 이동하세요.

| 상황 | 바로가기 |
|------|---------|
| 이 PC에서 **처음** 설치 | [① 최초 실행](#-최초-실행-처음-1회) |
| 이미 설치됨 → **최신 버전 받아서** 실행 | [② 업데이트 후 실행](#-업데이트-후-실행) |
| 이미 설치됨 → **그냥** 실행 | [③ 그냥 실행](#-그냥-실행) |
| 다른 PC에서 접속하는 **공유 서버** | [공유 서버 모드](#공유-서버-모드) |

각 항목마다 **(A) 배치파일 자동 실행** / **(B) PowerShell 수동 실행** 두 방법을 제공합니다.

---

# ① 최초 실행 (처음 1회)

이 PC에서 프로그램을 **처음** 사용할 때.

## 사전 준비 (공통)

### 1) Python 3.11 설치

> https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe

⚠️ 설치 시 **"Add Python to PATH"** 반드시 체크
⚠️ Python 3.13+ 는 CoolProp 호환 문제 → **3.11 권장**

### 2) Git 설치

> https://git-scm.com/download/win

### 3) GitHub 토큰 발급 (비공개 저장소 접근)

GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
→ Generate new token → **`repo`** 권한 체크 → 토큰(`ghp_...`) 복사 보관

---

## (A) 배치파일 자동 실행 ← 쉬움

### 1. 저장소 복제 (CMD)
```
cd /d %USERPROFILE%\Documents
git clone https://github.com/kimjy2576/HPWD-DataManager.git
```
→ Username: `kimjy2576` / Password: **토큰**(`ghp_...`)

### 2. 실행
탐색기에서 `HPWD-DataManager` 폴더로 이동 →
**`START.bat` 더블클릭**

- 최초 실행 시 필요한 패키지가 자동 설치됩니다 (2~3분)
- 브라우저가 자동으로 열립니다 → http://localhost:8001

---

## (B) PowerShell 수동 실행

### 1. 저장소 복제
```
cd $env:USERPROFILE\Documents
git clone https://github.com/kimjy2576/HPWD-DataManager.git
cd HPWD-DataManager
```
→ Username: `kimjy2576` / Password: **토큰**(`ghp_...`)

### 2. 패키지 설치 (최초 1회)
```
pip install -r requirements.txt
```
> `pip` 안 되면: `python -m pip install -r requirements.txt`

### 3. 실행
```
python run.py
```
→ http://localhost:8001 자동 열림

---

# ② 업데이트 후 실행

이미 설치되어 있고, **최신 코드를 받아서** 실행할 때.

## (A) 배치파일 자동 실행

프로그램이 이미 켜져 있다면 → 화면 상단 **[🔄 업데이트]** 버튼 클릭이 가장 간단합니다.

명령창으로 하려면 (CMD):
```
cd /d %USERPROFILE%\Documents\HPWD-DataManager
git pull
```
그 다음 `START.bat` 더블클릭 (또는 아래 명령)
```
START.bat
```

## (B) PowerShell 수동 실행
```
cd $env:USERPROFILE\Documents\HPWD-DataManager
git pull
python run.py
```

> 패키지가 추가됐을 수 있으니 가끔:
> ```
> pip install -r requirements.txt
> ```

> ⚠️ `git pull` 충돌 시:
> ```
> git stash
> git pull
> git stash pop
> ```

---

# ③ 그냥 실행

이미 설치되어 있고, 업데이트 없이 **바로** 실행할 때.

## (A) 배치파일 자동 실행

탐색기에서 `HPWD-DataManager` 폴더 → **`START.bat` 더블클릭**

또는 CMD:
```
cd /d %USERPROFILE%\Documents\HPWD-DataManager
START.bat
```

## (B) PowerShell 수동 실행
```
cd $env:USERPROFILE\Documents\HPWD-DataManager
python run.py
```

> ⚠️ PowerShell에서 `START.bat`이 인식 안 되면 **`.\START.bat`** 으로 입력
> (PowerShell은 보안상 현재 폴더 스크립트를 이름만으론 실행 안 함)

---

# 공유 서버 모드

서버 PC 한 대에서 실행하고, 같은 네트워크의 다른 PC들이 브라우저로 접속.

## 서버 PC에서

### (A) 배치파일 자동 실행
1. **`ALLOW_FIREWALL.bat`** 우클릭 → **"관리자 권한으로 실행"** (최초 1회, 방화벽 허용)
2. **`START_SERVER.bat`** 더블클릭

### (B) PowerShell 수동 실행
```
cd $env:USERPROFILE\Documents\HPWD-DataManager
python run.py --share
```
> 최초 실행 시 Windows 방화벽 팝업 → **[액세스 허용]** 클릭

### 실행하면 두 주소가 표시됨
```
══════════════════════════════════════════
  이 PC에서 접속:   http://localhost:8001
  다른 PC에서 접속: http://192.168.0.15:8001   ← 이 주소 공유
══════════════════════════════════════════
```

## 다른 PC에서 접속

브라우저 주소창에 서버가 알려준 주소 입력:
```
http://192.168.0.15:8001
```
> 접속하는 PC에는 **아무것도 설치할 필요 없음** (브라우저만 있으면 됨)

## 주의

- 서버 PC와 접속 PC가 **같은 네트워크**(사내망/공유기)에 있어야 함
- 서버 PC의 **CMD 창을 닫으면** 서버 종료
- 회사 네트워크는 보안 정책으로 PC 간 접속이 막혀있을 수 있음 (IT팀 문의)

---

# 포트 안내

기본 **8001**, 사용 중이면 자동으로 8002 → 8003 ... 빈 포트 사용.
실제 주소는 **CMD 창 상단 로그**에 표시됩니다.

---

# 배치파일 요약

| 파일 | 용도 | 실행 |
|------|------|------|
| `START.bat` | 로컬 실행 (이 PC만) | 더블클릭 |
| `START_SERVER.bat` | 공유 서버 (다른 PC 접속) | 더블클릭 |
| `ALLOW_FIREWALL.bat` | 방화벽 허용 (최초 1회) | 우클릭 → 관리자 권한 |

---

# 설정/데이터 저장 위치

| 항목 | 위치 | 비고 |
|------|------|------|
| 프로그램 코드 | 작업 폴더 | git pull로 업데이트 |
| 사용자 설정 (디폴트/슬롯) | `config/saves/`, `config/viewer_default.json` | 서버 저장 |
| 병합/계산 결과 | `results/` 또는 지정 폴더 | 로컬 |
| 폴더 분류, 사이드바 너비 | 브라우저 localStorage | **PC/브라우저별** |

> ⚠️ Visualization의 **폴더 분류·사이드바 너비**는 브라우저에 저장 → PC/브라우저 바뀌면 초기화.
> 차트 **카테고리/변수/Y축 범위/글꼴**은 **[⭐ Viewer 디폴트 저장]** 으로 서버 저장 → 다른 PC에서도 불러오기 가능.

---

# 문제 해결

| 증상 | 해결 |
|------|------|
| PowerShell에서 `START.bat` 인식 안 됨 | **`.\START.bat`** 또는 탐색기 더블클릭 |
| clone 시 인증 실패 | Username=`kimjy2576`, Password=**토큰**(`ghp_...`). 비밀번호 아님 |
| `Authentication failed` | 토큰 만료/권한 부족 → 새 토큰 발급 (`repo` 체크) |
| `python` 인식 안 됨 | Python 재설치 + "Add to PATH". 또는 `py run.py` |
| `git` 인식 안 됨 | Git for Windows 설치 후 CMD 재시작 |
| `pip` 인식 안 됨 | `python -m pip ...` 로 대체 |
| `ModuleNotFoundError: uvicorn` | `python -m pip install uvicorn[standard]` |
| CoolProp 설치 실패 | Python 3.11 사용 (3.13 미지원) |
| MX100 변수 안 잡힘 | `pip install xlrd` |
| 포트 충돌 | 자동으로 다음 포트 사용 (조치 불필요) |
| 화면 빈 페이지 | 브라우저 새로고침 (Ctrl+F5) 또는 git pull 후 재실행 |
| `git pull` 충돌 | `git stash` → `git pull` → `git stash pop` |
| START.bat 실행 안 됨 | CMD에서 `python run.py` 직접 실행 → 실제 오류 확인 |

> START.bat이 오류를 숨길 때가 있습니다. 정확한 원인은:
> ```
> cd 작업폴더
> python run.py
> ```

---

# 시스템 요구사항

- Windows 10 이상
- Python 3.10 ~ 3.12 (3.11 권장)
- Git for Windows
- 브라우저 (Chrome / Edge 권장)

---

# 상세 설치 가이드

더 자세한 설치 절차는 **[SETUP.md](SETUP.md)** 를 참조하세요.

---

# 담당자

리빙솔루션사업부 세탁기선행플랫폼Task 김진영 선임
