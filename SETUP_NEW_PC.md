# 새 PC 설치 가이드 (Git 기반)

다른 PC에서 HPWD Data Manager를 설치하고 사용하는 방법입니다.
두 가지 방법이 있으니 상황에 맞게 선택하세요.

- **방법 A (간단)**: 그냥 실행만 할 경우 → ZIP 다운로드
- **방법 B (개발/동기화)**: 코드를 수정하거나 여러 PC에서 동기화할 경우 → Git clone

---

## 사전 준비 (공통, 최초 1회)

### 1. Python 3.11 설치

> https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe

⚠️ 설치 시 **"Add Python to PATH"** 반드시 체크
⚠️ Windows Store 버전 Python은 권한 문제로 사용 불가 (위 공식 링크 사용)

설치 확인 (CMD에서):
```
python --version
```
`Python 3.11.9` 가 출력되면 정상.

---

## 방법 A — ZIP 다운로드 (실행 전용)

코드 수정 없이 그냥 쓰기만 할 때 가장 간단합니다.

### A-1. 다운로드 & 압축 해제

CMD에서 한 줄씩 입력:
```
cd /d %USERPROFILE%\Downloads
```
```
curl -L -o dryer.zip https://github.com/kimjy2576/Dryer-Merger/archive/refs/heads/main.zip
```
```
powershell -command "Expand-Archive -Force dryer.zip ."
```

### A-2. 실행

```
cd Dryer-Merger-main
```
```
START.bat
```

최초 실행 시 필요한 패키지가 자동 설치됩니다 (2~3분).
브라우저가 자동으로 열립니다 (http://localhost:8000).

### A-3. 업데이트 (최신 버전 받기)

기존 폴더를 지우고 A-1을 다시 수행하면 됩니다.
(설정은 서버의 config 폴더에 저장되므로, 설정을 유지하려면 config 폴더를 백업 후 복사)

---

## 방법 B — Git clone (개발 / 다중 PC 동기화)

코드를 수정하거나, 여러 PC에서 같은 환경을 유지할 때 권장합니다.

### B-1. Git 설치 (최초 1회)

> https://git-scm.com/download/win

설치 확인:
```
git --version
```

### B-2. 저장소 복제

원하는 위치에서 (예: 문서 폴더):
```
cd /d %USERPROFILE%\Documents
```
```
git clone https://github.com/kimjy2576/Dryer-Merger.git
```
```
cd Dryer-Merger
```

> 🔒 **비공개 백업 저장소**(HPWD-DataManager)를 쓰려면 위 URL 대신:
> ```
> git clone https://github.com/kimjy2576/HPWD-DataManager.git
> ```
> 이 경우 GitHub 로그인(또는 Personal Access Token)이 필요합니다.

### B-3. 패키지 설치 (최초 1회)

```
pip install -r requirements.txt
```

`pip`가 인식되지 않으면:
```
python -m pip install -r requirements.txt
```

### B-4. 실행

```
START.bat
```
또는
```
python run.py
```

### B-5. 업데이트 (최신 코드 받기)

작업 폴더에서:
```
git pull
```

새 패키지가 추가되었을 수 있으니 가끔:
```
pip install -r requirements.txt
```

---

## 설정/데이터는 어디에 저장되나

| 항목 | 위치 | Git 추적 |
|------|------|----------|
| 프로그램 코드 | 작업 폴더 전체 | ✅ 추적됨 |
| 사용자 설정 (디폴트/세이브 슬롯) | `config/saves/`, `config/viewer_default.json` | 보통 추적됨 |
| 병합/계산 결과 파일 | `outputs/` 또는 지정 폴더 | ❌ 보통 .gitignore |
| 폴더 분류 (Visualization) | 브라우저 localStorage | ❌ PC별 로컬 |

> ⚠️ **localStorage 기반 설정** (Visualization 폴더 분류, 사이드바 너비 등)은
> 브라우저에 저장되므로 PC/브라우저가 바뀌면 초기화됩니다.
> 차트 카테고리/변수/Y축 범위는 **[⭐ Viewer 디폴트 저장]** 으로 서버에 저장하면
> 다른 PC에서도 동일하게 불러올 수 있습니다.

---

## 자주 발생하는 문제

| 증상 | 해결 |
|------|------|
| `python` 인식 안 됨 | Python 공식 버전 재설치 + "Add to PATH" 체크 |
| `git` 인식 안 됨 | Git for Windows 설치 후 CMD 재시작 |
| `pip` 인식 안 됨 | `python -m pip ...` 로 대체 |
| CoolProp 설치 실패 | Python 3.11 사용 (3.13 미지원) |
| MX100 변수 안 잡힘 | `pip install xlrd` |
| 포트 충돌 (8000) | 기존 실행 종료 후 재시도 |
| git pull 충돌 | 로컬 수정사항이 있으면 `git stash` 후 `git pull`, 다시 `git stash pop` |
| 화면 빈 페이지 | 브라우저 새로고침 (Ctrl+F5) 또는 git pull 후 재실행 |

---

## 빠른 요약 (개발자용)

```
# 최초
git clone https://github.com/kimjy2576/Dryer-Merger.git
cd Dryer-Merger
pip install -r requirements.txt
START.bat

# 이후 업데이트
git pull
START.bat
```

---

## 담당자

리빙솔루션사업부 세탁기선행플랫폼Task 김진영 선임
