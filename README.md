# HPWD Data Manager

히트펌프 건조기 실험 데이터 통합 분석 프로그램

BR (BlackRose) + AMS + MX100 + NIDAQ 데이터를 병합하고, 열역학 계산을 수행한 뒤,
개별선도 / 습공기 선도 / P-h 선도 / 성능 비교로 시각화합니다.

> **저장소: `kimjy2576/HPWD-DataManager` (비공개)**
> **기본 포트: 8001** (사용 중이면 8002~ 자동 변경)

---

## 실행 방법

폴더의 **`run.bat` 더블클릭** — 이게 전부입니다.

`run.bat`이 자동으로:
1. Python 확인 (3.10+)
2. 패키지 확인 → 없으면 자동 설치
3. 방화벽 규칙 등록 (관리자 권한일 때)
4. **서버 모드로 실행** → 다른 PC에서도 접속 가능

실행하면 CMD에 두 주소가 표시됩니다:
```
  이 PC에서 접속:   http://localhost:8001
  다른 PC에서 접속: http://192.168.0.15:8001   ← 이 주소를 공유
```

> **최초 1회만** `run.bat` 우클릭 → **"관리자 권한으로 실행"** 하면
> 방화벽 규칙이 자동 등록됩니다.
> 안 해도 실행되며, Windows 방화벽 팝업에서 **[액세스 허용]** 눌러도 동일합니다.

> **PowerShell**에서는 `.\run.bat` 으로 입력 (앞에 `.\` 필수)

### 이 PC에서만 쓰고 싶을 때
```
python run.py
```
(`--share` 없이 실행하면 localhost 전용)

---

## 상황별 절차

| 상황 | 할 일 |
|------|------|
| 이 PC에서 **처음** | [최초 설치](#최초-설치) → `run.bat` |
| **최신 버전** 받아서 실행 | 프로그램 내 **[🔄 업데이트]** 버튼, 또는 `git pull` → `run.bat` |
| **그냥** 실행 | `run.bat` |

---

## 최초 설치

### 1. Python 3.11 설치
> https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe

⚠️ 설치 시 **"Add Python to PATH"** 반드시 체크
⚠️ Python 3.13+ 는 CoolProp 호환 문제 → **3.11 권장**

### 2. Git 설치
> https://git-scm.com/download/win

### 3. GitHub 토큰 발급 (비공개 저장소 접근)

GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
→ Generate new token → **`repo`** 권한 체크 → 토큰(`ghp_...`) 복사 보관

### 4. 저장소 복제

CMD에서:
```
cd /d %USERPROFILE%\Documents
git clone https://github.com/kimjy2576/HPWD-DataManager.git
```
→ Username: `kimjy2576` / Password: **토큰**(`ghp_...`)

PowerShell이면 첫 줄만 다름:
```
cd $env:USERPROFILE\Documents
```

### 5. 실행
`HPWD-DataManager` 폴더의 **`run.bat` 더블클릭**
(패키지는 최초 실행 시 자동 설치, 2~3분)

---

## 업데이트

### 방법 1: 프로그램 내 버튼 (권장)
상단 날짜 옆 **[🔄 업데이트]** 클릭 → `git fetch` + 재시작 자동 수행

### 방법 2: 수동
```
cd /d %USERPROFILE%\Documents\HPWD-DataManager
git pull
run.bat
```
> 충돌 시: `git stash` → `git pull` → `git stash pop`

---

## 다른 PC에서 접속

브라우저 주소창에 서버가 알려준 주소 입력:
```
http://192.168.0.15:8001
```
> 접속하는 PC에는 **아무것도 설치할 필요 없음** (브라우저만 있으면 됨)

**주의**
- 서버 PC와 접속 PC가 **같은 네트워크**(사내망/공유기)에 있어야 함
- 서버 PC의 **CMD 창을 닫으면** 서버 종료
- 회사 네트워크는 보안 정책으로 PC 간 접속이 막혀있을 수 있음 (IT팀 문의)
- 서버 PC IP가 바뀌면(DHCP) 접속 주소도 바뀜 → 고정 IP 권장

---

## 설정/데이터 저장 위치

| 항목 | 위치 | 비고 |
|------|------|------|
| 프로그램 코드 | 작업 폴더 | git pull로 업데이트 |
| 사용자 설정 (디폴트/슬롯) | `config/saves/`, `config/viewer_default.json` | 서버 저장 |
| 병합/계산 결과 | `results/` 또는 지정 폴더 | 로컬 |
| 폴더 분류, 사이드바 너비 | 브라우저 localStorage | **PC/브라우저별** |

> ⚠️ Visualization의 **폴더 분류·사이드바 너비**는 브라우저에 저장 → PC/브라우저 바뀌면 초기화.
> 차트 **카테고리/변수/Y축 범위/글꼴**은 **[⭐ Viewer 디폴트 저장]** 으로 서버 저장 → 다른 PC에서도 불러오기 가능.

---

## 데이터 진단 (diagnose.bat)

Merge 결과가 이상하거나 오류가 날 때, **데이터를 밖으로 보내지 않고** 원인을 찾는 도구입니다.

`diagnose.bat` 위에 **AMS 파일이나 케이스 폴더를 끌어다 놓으면** 앱과 같은 코드로 읽어서 요약 리포트를 만듭니다.
리포트는 `results/_diagnose/` 에 저장되며, **원본 값 없이 개수·칸 수·시간 길이·주요 변수 집계값만** 담깁니다.
그 텍스트만 공유하면 됩니다. 파일/폴더 이름까지 가리려면 `python tools\diagnose.py <경로> --mask`.

점검 항목: 헤더/데이터 칸 수 불일치, 중간 헤더 반복, 잘린 줄, 인코딩 혼재, 문자 혼입, 센서 무효값(65535),
배율(×10 저장 의심), 필수 변수 누락, 시간 형식·롤오버·역행·공백, 케이스 내 파일 분할/짝 불일치, 느린 파서 전환

---

## 문제 해결

| 증상 | 해결 |
|------|------|
| PowerShell에서 `run.bat` 인식 안 됨 | **`.\run.bat`** 또는 탐색기 더블클릭 |
| 다른 PC에서 접속 안 됨 | `run.bat` 우클릭 → 관리자 권한으로 1회 실행 (방화벽 등록) |
| clone 시 인증 실패 | Username=`kimjy2576`, Password=**토큰**(`ghp_...`). 비밀번호 아님 |
| `Authentication failed` | 토큰 만료/권한 부족 → 새 토큰 발급 (`repo` 체크) |
| `python` 인식 안 됨 | Python 재설치 + "Add to PATH". 또는 `py run.py --share` |
| `git` 인식 안 됨 | Git for Windows 설치 후 CMD 재시작 |
| `ModuleNotFoundError: uvicorn` | `python -m pip install -r requirements.txt` |
| CoolProp 설치 실패 | Python 3.11 사용 (3.13 미지원) |
| MX100 변수 안 잡힘 | `pip install xlrd` |
| 포트 충돌 | 자동으로 다음 포트 사용 (조치 불필요) |
| 화면 빈 페이지 / 변경 반영 안 됨 | Ctrl+F5 (강력 새로고침) |
| `git pull` 충돌 | `git stash` → `git pull` → `git stash pop` |
| Merge 결과가 이상함 / 원인 불명 | `diagnose.bat` 에 파일을 끌어놓고 리포트 확인 |
| `run.bat` 실행 후 바로 닫힘 | CMD에서 `python run.py --share` 직접 실행 → 실제 오류 확인 |

---

## 시스템 요구사항

- Windows 10 이상
- Python 3.10 ~ 3.12 (3.11 권장)
- Git for Windows
- 브라우저 (Chrome / Edge 권장)

---

## 담당자

리빙솔루션사업부 세탁기선행플랫폼Task 김진영 선임
