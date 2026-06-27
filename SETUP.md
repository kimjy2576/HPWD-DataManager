# HPWD Data Manager — 설치 및 실행 가이드

히트펌프 건조기 실험 데이터 통합 분석 프로그램입니다.
새 PC에서 설치하고 실행하는 절차를 안내합니다.

> **저장소: `kimjy2576/HPWD-DataManager` (비공개)**
> **기본 포트: 8001** (사용 중이면 8002, 8003... 자동 변경)

> 🔒 비공개 저장소이므로 **Git clone + 인증(토큰)** 으로만 받을 수 있습니다.
> (ZIP 다운로드는 비공개 저장소에서 불가)

---

## 사전 준비 (최초 1회)

### 1. Python 3.11 설치

> https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe

⚠️ 설치 시 **"Add Python to PATH"** 반드시 체크
⚠️ Windows Store 버전 Python은 권한 문제로 사용 불가 (위 공식 링크 사용)
⚠️ Python 3.13+ 는 CoolProp 호환 문제가 있으니 **3.11 권장**

확인:
```
python --version
```
`Python 3.11.9` 출력되면 정상. (`python`이 안 되면 `py`로 대체)

### 2. Git 설치

> https://git-scm.com/download/win

확인:
```
git --version
```

### 3. GitHub Personal Access Token 발급 (비공개 저장소 접근용)

1. GitHub 로그인 → 우측 상단 프로필 → **Settings**
2. 좌측 맨 아래 **Developer settings**
3. **Personal access tokens → Tokens (classic)**
4. **Generate new token (classic)**
5. 권한에서 **`repo`** (전체) 체크
6. 생성된 토큰 `ghp_...` **복사해서 안전한 곳에 보관**
   (이 화면을 벗어나면 다시 볼 수 없음)

> 토큰은 비밀번호 대신 사용합니다. 외부에 노출하지 마세요.

---

## 설치

### 1. 저장소 복제

```
cd /d %USERPROFILE%\Documents
```
```
git clone https://github.com/kimjy2576/HPWD-DataManager.git
```

→ 사용자명과 비밀번호를 물으면:
- **Username**: `kimjy2576` (GitHub 아이디)
- **Password**: 발급받은 **토큰** (`ghp_...`) 붙여넣기 (GitHub 비밀번호 아님)

```
cd HPWD-DataManager
```

> 폴더가 한 단계 더 들어가 있지 않은지 확인:
> ```
> dir
> ```
> → `run.py`, `START.bat` 이 바로 보여야 함.

### 2. 패키지 설치 (최초 1회)

```
pip install -r requirements.txt
```
`pip`가 인식되지 않으면:
```
python -m pip install -r requirements.txt
```

### 3. 실행

```
START.bat
```
또는
```
python run.py
```

> ⚠️ **PowerShell**에서 `START.bat`이 인식 안 되면 **`.\START.bat`** 으로 입력.
> 또는 탐색기에서 `START.bat` **더블클릭**.

→ 브라우저 자동 열림: **http://localhost:8001**

### 4. 업데이트 (최신 코드 받기)

작업 폴더에서:
```
git pull
```
새 패키지가 추가됐을 수 있으니 가끔:
```
pip install -r requirements.txt
```

---

## 포트 안내

기본 포트는 **8001** 입니다.

```
══════════════════════════════════════════
  HPWD Data Manager — http://localhost:8001
  [Python]  (포트 8001 사용)
══════════════════════════════════════════
```

- **8001이 사용 중**이면 자동으로 8002 → 8003 ... 빈 포트를 찾아 실행합니다.
- 실제 사용 중인 주소는 **CMD 창 상단 로그**에 표시됩니다.
- 다른 서버(예: 8000)와 충돌하지 않습니다.

> 브라우저가 자동으로 안 열리면 CMD 로그의 주소를 직접 입력하세요.

---

## 공유 서버 모드 (다른 PC에서 접속)

서버 PC 한 대에서 실행하고, 같은 네트워크의 다른 PC들이 브라우저로 접속하는 방식입니다.

### 실행 방법

| 모드 | 배치 파일 | 명령 | 접속 범위 |
|------|----------|------|----------|
| **로컬** (기본) | `START.bat` | `python run.py` | 그 PC에서만 |
| **공유 서버** | `START_SERVER.bat` | `python run.py --share` | 같은 네트워크 모든 PC |

### 서버 PC에서 (최초 1회)

**1. 방화벽 허용** (한 번만)

`ALLOW_FIREWALL.bat` 을 **마우스 오른쪽 클릭 → "관리자 권한으로 실행"**
(포트 8001~8010 인바운드 허용 규칙 자동 추가)

> 또는 `START_SERVER.bat` 첫 실행 시 뜨는 Windows 방화벽 팝업에서
> **[액세스 허용]** 클릭해도 됩니다.

**2. 서버 실행**

```
START_SERVER.bat
```
(PowerShell이면 `.\START_SERVER.bat`)

실행하면 CMD에 두 주소가 표시됩니다:
```
══════════════════════════════════════════════════════
  HPWD Data Manager  [Python]
══════════════════════════════════════════════════════
  이 PC에서 접속:   http://localhost:8001
  다른 PC에서 접속: http://192.168.0.15:8001     ← 이 주소를 공유
══════════════════════════════════════════════════════
```

### 다른 PC에서 접속

브라우저 주소창에 서버가 알려준 주소 입력:
```
http://192.168.0.15:8001
```
> 실제 IP는 서버 PC의 CMD 로그에 표시된 주소를 사용하세요.
> 접속하는 PC에는 **아무것도 설치할 필요 없음** (브라우저만 있으면 됨).

### 주의사항

- 서버 PC와 접속 PC가 **같은 네트워크**(사내망 / 같은 공유기)에 있어야 합니다.
- 서버 PC의 **CMD 창을 닫으면** 모든 접속이 끊깁니다 (서버 종료).
- 서버 PC가 **절전/종료**되면 접속 불가.
- 회사 네트워크는 보안 정책으로 PC 간 접속이 막혀있을 수 있습니다 (IT팀 문의).
- 서버 PC IP가 바뀌면(DHCP) 접속 주소도 바뀝니다. 고정 IP 권장.

### 여러 사람이 동시 사용 시

- 파일 업로드/병합/계산은 **서버 PC의 디스크**에 저장됩니다.
- Visualization의 폴더 분류·사이드바 설정은 **각자 브라우저**에 저장됩니다 (개인별).
- 결과 파일은 공유되므로 다른 사람이 만든 merged/calc도 보입니다.

---

## CMD vs PowerShell 주의

| 창 종류 | `START.bat` 실행 |
|---------|-----------------|
| **CMD** (명령 프롬프트) | `START.bat` |
| **PowerShell** | `.\START.bat` (앞에 `.\` 필수) |

PowerShell에서 그냥 `START.bat`을 입력하면:
```
'START.bat' 용어가 ... 인식되지 않습니다.
```
→ **해결: `.\START.bat`** 또는 탐색기에서 **더블클릭**

---

## 설정/데이터 저장 위치

| 항목 | 위치 | 비고 |
|------|------|------|
| 프로그램 코드 | 작업 폴더 | git pull로 업데이트 |
| 사용자 설정 (디폴트/세이브 슬롯) | `config/saves/`, `config/viewer_default.json` | 서버 저장 |
| 병합/계산 결과 파일 | `results/` 또는 지정 폴더 | 로컬 |
| 폴더 분류, 사이드바 너비 등 | 브라우저 localStorage | **PC/브라우저별 로컬** |

> ⚠️ Visualization의 **폴더 분류, 사이드바 너비**는 브라우저에 저장되어
> PC/브라우저가 바뀌면 초기화됩니다.
> 차트 **카테고리 / 변수 / Y축 범위 / 글꼴**은 **[⭐ Viewer 디폴트 저장]** 으로
> 서버에 저장하면 다른 PC에서도 불러올 수 있습니다.

---

## 종료 방법

- CMD 창을 닫거나
- `Ctrl + C`

---

## 문제 해결

| 증상 | 해결 |
|------|------|
| clone 시 인증 실패 | Username=`kimjy2576`, Password=**토큰**(`ghp_...`). 비밀번호 아님 |
| `Authentication failed` | 토큰 만료/권한 부족. 새 토큰 발급 (`repo` 권한 체크) |
| PowerShell에서 `START.bat` 인식 안 됨 | **`.\START.bat`** 또는 탐색기 더블클릭 |
| `python`이 인식되지 않음 | Python 재설치 + "Add to PATH". 또는 `py run.py` |
| `git`이 인식되지 않음 | Git for Windows 설치 후 CMD 재시작 |
| `pip`이 인식되지 않음 | `python -m pip ...` 로 대체 |
| `ModuleNotFoundError: uvicorn` | `python -m pip install uvicorn[standard]` |
| CoolProp 설치 실패 | Python 3.11 사용 (3.13 미지원) |
| MX100 변수 안 잡힘 | `pip install xlrd` |
| 포트 충돌 | 자동으로 다음 포트 사용 (조치 불필요) |
| 화면 빈 페이지 | 브라우저 새로고침 (Ctrl+F5) 또는 git pull 후 재실행 |
| git pull 충돌 | `git stash` → `git pull` → `git stash pop` |
| START.bat 실행 안 됨 | CMD에서 `python run.py` 직접 실행 → 실제 오류 확인 |

> START.bat이 오류를 숨길 때가 있습니다. 정확한 원인은:
> ```
> cd 작업폴더
> python run.py
> ```
> 여기서 출력되는 메시지가 진짜 원인입니다.

---

## 빠른 요약

```
# 최초 1회
git clone https://github.com/kimjy2576/HPWD-DataManager.git
  → Username: kimjy2576 / Password: ghp_토큰
cd HPWD-DataManager
pip install -r requirements.txt
START.bat        (PowerShell이면 .\START.bat)

# 이후 업데이트
git pull
START.bat
```

→ 접속: **http://localhost:8001**

---

## 시스템 요구사항

- Windows 10 이상
- Python 3.10 ~ 3.12 (3.11 권장)
- Git for Windows
- 브라우저 (Chrome / Edge 권장)

---

## 담당자

리빙솔루션사업부 세탁기선행플랫폼Task 김진영 선임
