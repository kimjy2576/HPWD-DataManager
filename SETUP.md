# HPWD Data Manager — 설치 및 실행 가이드

히트펌프 건조기 실험 데이터 통합 분석 프로그램입니다.
새 PC에서 설치하고 실행하는 절차를 안내합니다.

> **기본 포트: 8001** (사용 중이면 8002, 8003... 자동 변경)

---

## 사전 준비 (최초 1회)

### Python 3.11 설치

> https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe

⚠️ 설치 시 **"Add Python to PATH"** 반드시 체크
⚠️ Windows Store 버전 Python은 권한 문제로 사용 불가 (위 공식 링크 사용)
⚠️ Python 3.13+ 는 CoolProp 호환 문제가 있으니 **3.11 권장**

설치 확인 (CMD에서):
```
python --version
```
`Python 3.11.9` 출력되면 정상.

> `python`이 인식되지 않으면 `py` 로 대체 가능 (예: `py run.py`)

---

## 방법 A — ZIP 다운로드 (실행 전용, 가장 간단)

코드 수정 없이 그냥 쓰기만 할 때.

### 1. 다운로드 & 압축 해제

CMD에서 한 줄씩:
```
cd /d %USERPROFILE%\Downloads
```
```
curl -L -o dryer.zip https://github.com/kimjy2576/Dryer-Merger/archive/refs/heads/main.zip
```
```
powershell -command "Expand-Archive -Force dryer.zip ."
```

### 2. 실행

```
cd Dryer-Merger-main
```
```
START.bat
```

> ⚠️ **PowerShell**을 쓰는 경우 `START.bat` 대신 **`.\START.bat`** 로 입력하세요.
> (PowerShell은 보안상 현재 폴더 스크립트를 이름만으론 실행하지 않음)
> 또는 탐색기에서 `START.bat`을 **더블클릭**해도 됩니다.

- 최초 실행 시 필요한 패키지 자동 설치 (2~3분)
- 브라우저가 자동으로 열림 → **http://localhost:8001**

---

## 방법 B — Git clone (개발 / 다중 PC 동기화)

코드를 수정하거나 여러 PC에서 동기화할 때.

### 1. Git 설치 (최초 1회)

> https://git-scm.com/download/win

확인:
```
git --version
```

### 2. 저장소 복제

```
cd /d %USERPROFILE%\Documents
```
```
git clone https://github.com/kimjy2576/Dryer-Merger.git
```
```
cd Dryer-Merger
```

> 🔒 비공개 백업 저장소(HPWD-DataManager)를 쓰려면:
> ```
> git clone https://github.com/kimjy2576/HPWD-DataManager.git
> ```
> GitHub 로그인 또는 Personal Access Token 필요.
>
> ⚠️ clone 후 폴더가 한 단계 더 들어가 있지 않은지 확인:
> ```
> dir
> ```
> → `run.py`, `START.bat` 이 바로 보여야 함.
> 안 보이면 `cd 하위폴더명` 으로 한 단계 더 진입.

### 3. 패키지 설치 (최초 1회)

```
pip install -r requirements.txt
```
`pip`가 인식되지 않으면:
```
python -m pip install -r requirements.txt
```

### 4. 실행

```
START.bat
```
또는
```
python run.py
```

> ⚠️ **PowerShell**에서 `START.bat`이 인식 안 되면 **`.\START.bat`** 으로 입력.

→ **http://localhost:8001**

### 5. 업데이트 (최신 코드 받기)

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

- **8001이 사용 중**이면 자동으로 8002 → 8003 ... 순서로 빈 포트를 찾아 실행합니다.
- 실제 사용 중인 주소는 **CMD 창 상단 로그**에 표시됩니다.
- 다른 서버(예: 8000 사용 중인 프로그램)와 충돌하지 않습니다.

> 브라우저는 실제 할당된 포트로 자동으로 열립니다.
> 만약 자동으로 안 열리면 CMD 로그에 표시된 주소를 직접 입력하세요.

---

## 설정/데이터 저장 위치

| 항목 | 위치 | 비고 |
|------|------|------|
| 프로그램 코드 | 작업 폴더 | git pull로 업데이트 |
| 사용자 설정 (디폴트/세이브 슬롯) | `config/saves/`, `config/viewer_default.json` | 서버 저장 |
| 병합/계산 결과 파일 | `results/` 또는 지정 폴더 | 로컬 |
| 폴더 분류, 사이드바 너비 등 | 브라우저 localStorage | **PC/브라우저별 로컬** |

> ⚠️ Visualization의 **폴더 분류, 사이드바 너비**는 브라우저에 저장되어
> PC나 브라우저가 바뀌면 초기화됩니다.
> 차트 **카테고리 / 변수 / Y축 범위 / 글꼴**은 **[⭐ Viewer 디폴트 저장]** 으로
> 서버에 저장하면 다른 PC에서도 불러올 수 있습니다.

---

## 종료 방법

- CMD 창을 닫거나
- CMD에서 `Ctrl + C`

---

## CMD vs PowerShell 주의

Windows에는 두 가지 명령창이 있습니다. 실행 방식이 다릅니다.

| 창 종류 | `START.bat` 실행 방법 |
|---------|----------------------|
| **CMD** (명령 프롬프트) | `START.bat` (그냥 입력) |
| **PowerShell** | `.\START.bat` (앞에 `.\` 필수) |

PowerShell에서 그냥 `START.bat`을 입력하면 다음 오류가 납니다:
```
'START.bat' 용어가 cmdlet, 함수, 스크립트 파일 또는 실행할 수 있는
프로그램 이름으로 인식되지 않습니다.
```
→ **해결: `.\START.bat`** 으로 입력 (PowerShell은 보안상 현재 폴더 스크립트를 이름만으론 실행 안 함)

**가장 쉬운 방법 3가지:**
1. `.\START.bat` 입력 (PowerShell)
2. 탐색기에서 `START.bat` **더블클릭** (명령창 불필요)
3. 폴더 주소창에 `cmd` 입력 → CMD 창이 그 폴더에서 열림 → `START.bat` 그냥 입력

---

## 문제 해결

| 증상 | 해결 |
|------|------|
| PowerShell에서 `START.bat` 인식 안 됨 | **`.\START.bat`** 으로 입력 (앞에 `.\` 추가) 또는 탐색기에서 더블클릭 |
| `python`이 인식되지 않음 | Python 공식 버전 재설치 + "Add to PATH" 체크. 또는 `py run.py` |
| `git`이 인식되지 않음 | Git for Windows 설치 후 CMD 재시작 |
| `pip`이 인식되지 않음 | `python -m pip ...` 로 대체 |
| `START.bat`이 인식 안 됨 (CMD) | 폴더 위치 오류. `dir`로 `run.py` 보이는 폴더까지 진입 |
| `ModuleNotFoundError: uvicorn` | `python -m pip install uvicorn[standard]` |
| CoolProp 설치 실패 | Python 3.11 사용 (3.13 미지원) |
| MX100 변수 안 잡힘 | `pip install xlrd` |
| 포트 충돌 | 8001이 막혀도 자동으로 다음 포트 사용 (조치 불필요) |
| 화면 빈 페이지 | 브라우저 새로고침 (Ctrl+F5) 또는 git pull 후 재실행 |
| git pull 충돌 | `git stash` → `git pull` → `git stash pop` |
| START.bat 실행 안 됨 | CMD에서 `python run.py` 직접 실행 → 실제 오류 메시지 확인 |

> START.bat이 오류를 숨기는 경우가 있습니다.
> 정확한 원인을 보려면 CMD에서 직접:
> ```
> cd 작업폴더
> python run.py
> ```
> 여기서 출력되는 메시지가 진짜 원인입니다.

---

## 빠른 요약

```
# 방법 A (실행만)
curl -L -o dryer.zip https://github.com/kimjy2576/Dryer-Merger/archive/refs/heads/main.zip
powershell -command "Expand-Archive -Force dryer.zip ."
cd Dryer-Merger-main
START.bat

# 방법 B (개발)
git clone https://github.com/kimjy2576/Dryer-Merger.git
cd Dryer-Merger
pip install -r requirements.txt
START.bat
# 업데이트: git pull
```

→ 접속: **http://localhost:8001**

---

## 시스템 요구사항

- Windows 10 이상
- Python 3.10 ~ 3.12 (3.11 권장)
- 브라우저 (Chrome / Edge 권장)

---

## 담당자

리빙솔루션사업부 세탁기선행플랫폼Task 김진영 선임
