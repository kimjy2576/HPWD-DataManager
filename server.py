"""
server.py — FastAPI 백엔드 (v5 — Merge / Calculation 분리)
"""
import os, sys, uuid, time, traceback, re, csv
from collections import Counter
from pathlib import Path
import ams_dat
from io_handler import ams_by_name
from typing import Optional

import yaml
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import load_config

app = FastAPI(title="HPWD Data Manager", version="5.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.middleware("http")
async def no_cache_static(request, call_next):
    """index.html / static 파일 캐시 차단.

    업데이트 후에도 브라우저가 예전 index.html을 캐시해서
    새 코드가 반영되지 않는 문제 방지.
    """
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.startswith("/static"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp

@app.get("/health")
def health():
    return {"status": "ok"}

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
    EXE_DIR = BASE_DIR
else:
    BASE_DIR = Path(__file__).parent
    EXE_DIR = BASE_DIR
STATIC = BASE_DIR / "static"
RESULT_DIR = EXE_DIR / "results"
RESULT_DIR.mkdir(exist_ok=True)


_READ_NOTES = []   # _read_csv 진단 메시지 버퍼 → _run_merge 가 UI 로그로 출력

# ── print() → 앱 로그 연결 ─────────────────────────────────────
# Merge/Calc 실행 중인 스레드의 print() 출력을 CMD 에 그대로 쓰면서
# 해당 세션의 UI 로그에도 전달한다. ([sync] [classify] [stage1] 등 진단이
#  CMD 에만 찍혀 사용자가 볼 수 없던 문제 해결). 스레드별로 구분하므로
#  동시에 여러 세션이 돌아도 로그가 섞이지 않음.
import threading as _threading, contextlib as _contextlib, functools as _functools
_LOG_CTX = _threading.local()


class _TeeStdout:
    def __init__(self, real):
        while isinstance(real, _TeeStdout):   # 이중 설치 방지 (중복 로그 방지)
            real = real._real
        self._real = real

    def write(self, s):
        try:
            self._real.write(s)
        except Exception:
            pass
        sid = getattr(_LOG_CTX, "sid", None)
        if sid:
            buf = getattr(_LOG_CTX, "buf", "") + s
            *lines, rest = buf.split("\n")
            _LOG_CTX.buf = rest
            for ln in lines:
                if ln.strip():
                    _log(sid, ln.rstrip())
        return len(s)

    def flush(self):
        try:
            self._real.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._real, name)


_TEE_LOCK = _threading.Lock()


def _ensure_tee():
    """sys.stdout 이 다른 것으로 바뀌었어도 연결기를 다시 씌움."""
    with _TEE_LOCK:
        if not isinstance(sys.stdout, _TeeStdout):
            sys.stdout = _TeeStdout(sys.stdout)


_ensure_tee()


@_contextlib.contextmanager
def _log_capture(sid):
    _ensure_tee()
    prev = getattr(_LOG_CTX, "sid", None)
    _LOG_CTX.sid, _LOG_CTX.buf = sid, ""
    try:
        yield
    finally:
        rest = getattr(_LOG_CTX, "buf", "")
        if rest.strip():
            _log(sid, rest.rstrip())
        _LOG_CTX.sid, _LOG_CTX.buf = prev, ""


def _capture_log(fn):
    """백그라운드 작업(sid 가 첫 인자)의 print() 를 UI 로그로 연결."""
    @_functools.wraps(fn)
    def wrapper(sid, *a, **kw):
        with _log_capture(sid):
            return fn(sid, *a, **kw)
    return wrapper


def _csv_layout(path, enc, sep, header_row, sample=300):
    """헤더 칸 수와 데이터 칸 수를 비교해 올바른 컬럼 이름 배열을 만든다.

    AMS 로거는 헤더 이름 안의 쉼표(예: '건조 외기 감지 값(상온, 저온, 고온)')를
    데이터에서는 별도 칸으로 기록해, 데이터가 헤더보다 (쉼표 수)만큼 넓어진다.
    이 경우 pandas 기본 동작은 초과분만큼 '앞쪽 칸을 인덱스로' 빼버려
    Time 을 포함한 모든 컬럼이 밀린다 (Time 자리에 Packet Index 가 들어감).
    """
    import csv, itertools
    from collections import Counter
    try:
        with open(path, "r", encoding=enc, errors="replace", newline="") as f:
            rows = list(itertools.islice(csv.reader(f, delimiter=sep),
                                         header_row + 1 + sample))
    except Exception:
        return None
    if len(rows) <= header_row:
        return None
    header = [c.strip() for c in rows[header_row]]
    while header and header[-1] == "":
        header.pop()
    N = len(header)
    data = [r for r in rows[header_row + 1:] if any(x.strip() for x in r)]
    if not data:
        return {"names": header, "N": N, "M": N, "mode": "ok", "note": ""}
    M = Counter(len(r) for r in data).most_common(1)[0][0]
    if M <= N:
        note = "" if M == N else f"데이터 {M}칸 < 헤더 {N}칸 (부족분은 빈 값)"
        return {"names": header, "N": N, "M": M, "mode": "ok", "note": note}
    extra = M - N
    commas = sum(h.count(",") for h in header) if sep == "," else 0
    # 행 끝의 빈 칸 수 (로거가 각 행 끝에 구분자를 붙이는 경우) — 최빈 폭 행의 95% 이상이 비어야 인정
    full = [r for r in data if len(r) == M]
    trail = 0
    for j in range(1, extra + 1):
        if full and sum(1 for r in full if not r[-j].strip()) >= 0.95 * len(full):
            trail = j
        else:
            break
    remain = extra - trail
    tnames = [f"_trail_{i + 1}" for i in range(trail)]
    tnote = f"행 끝 빈 칸 {trail}개(구분자로 끝나는 행) 무시" if trail else ""

    if remain == 0:
        return {"names": header + tnames, "N": N, "M": M, "mode": "trailing",
                "note": f"헤더 {N}칸 / 데이터 {M}칸 → {tnote} (정상)"}
    if remain == commas:
        names = []
        for h in header:
            names.append(h)
            names += [f"{h}#{j + 2}" for j in range(h.count(","))]
        split = [h for h in header if "," in h]
        return {"names": names + tnames, "N": N, "M": M, "mode": "comma_split",
                "note": f"헤더 {N}칸 / 데이터 {M}칸 → 쉼표 포함 헤더 {len(split)}개가 "
                        f"데이터에서 분할됨, 정렬 보정: {split}" + (f" / {tnote}" if trail else "")}
    names = header + [f"_extra_{i + 1}" for i in range(remain)] + tnames
    return {"names": names, "N": N, "M": M, "mode": "pad",
            "note": f"⚠️ 헤더 {N}칸 / 데이터 {M}칸 — 초과 {extra}칸 중 {remain}칸 원인 불명 "
                    f"(헤더 이름 속 쉼표 {commas}개, 행 끝 빈 칸 {trail}개로 설명 안 됨). "
                    f"뒤쪽에 임시 이름 부여 — 뒤쪽 컬럼 값 확인 필요"}


def _dedupe_names(names):
    seen, out = {}, []
    for n in names:
        if n in seen:
            seen[n] += 1
            out.append(f"{n}.{seen[n]}")
        else:
            seen[n] = 0
            out.append(n)
    return out


# 배율 표기('[*10]','[/100]')가 없는 로거 버전에서 전처리가 데이터로 배율을 판정하는 AMS 컬럼
_AMS_INFERABLE = ("HP_CompTargetHz", "HP_FanSpeed", "HP_SuperHeatDegree")


def _ams_default_weight(raw_col: str, std_col: str, scale_factors: dict) -> float:
    """변수 스캔 표의 AMS 기본 Weight = 전처리가 자동으로 적용하는 로거 배율.

    표기 없는 변형 컬럼(_AMS_INFERABLE)은 Merge 때 데이터로 판정하므로 1.0 으로 표시.
    _apply_variable_settings 는 표의 Weight 가 이 값과 같으면 '자동 배율 유지'로 본다.
    """
    w = float((scale_factors or {}).get(std_col, 1.0))
    if std_col in _AMS_INFERABLE and "[" not in str(raw_col):
        w = 1.0
    return w


def _num_or(v, default):
    """표에서 온 숫자(빈칸·None·문자 가능)를 float 로. 해석 불가면 기본값."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if np.isfinite(x) else default


# ══════════════════════════════════════════════
#  텍스트 계측 파일 (.dat / .txt 등) 읽기
# ══════════════════════════════════════════════
# .csv/.tsv 는 확장자로 구분자를 정하는 기존 경로를 그대로 쓰고, 그 밖의 확장자는
# 내용을 보고 구분자·헤더 줄·단위 줄·인코딩을 판별한다. (.dat 은 장비마다 형식이 다름 —
# 탭/공백/세미콜론 구분, 앞쪽 설명 블록, 헤더 아래 단위 줄, UTF-16 저장 등)
# 식별 규칙에 없어도 내용으로 소스를 판별하는 확장자. (.txt 는 메모·다른 표일 때가 많아 자동 판별에서 뺌 —
#  식별 규칙의 확장자에 넣으면 .dat 와 같은 방식으로 읽음)
_GENERIC_TEXT_EXTS = (".dat",)
_CTRL_BYTES = bytes(list(range(0, 9)) + list(range(14, 32)))
_SEP_NAMES = {"\t": "탭", ",": "쉼표", ";": "세미콜론", "|": "세로줄", " ": "공백"}
_RX_DATE_CELL = re.compile(r"^\d{2,4}[-/.]\d{1,2}[-/.]\d{1,4}")
_RX_CLOCK_CELL = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?([.,]\d+)?$")
_RX_DEC_COMMA = re.compile(r"^[-+]?\d+,\d+$")
_RX_DEC_POINT = re.compile(r"^[-+]?\d*\.\d+$")
_RX_UNIT_CELL = re.compile(
    r"^[\[(]?\s*(s|sec|ms|min|h|hr|hour|°c|℃|degc|deg c|deg|c|k|v|mv|volts?|a|ma|amps?|w|kw|kwh|wh|"
    r"va|var|pf|%|%rh|rh|pa|kpa|mpa|bar|bara|barg|psi|hz|rpm|g|kg|l|lpm|l/min|m3/h|cmm|cmh|m/s|"
    r"ts|rn|smp|avg|max|min|tot|std)\s*[\])]?$", re.I)
# 단위 줄에 자주 나오는 형식 표기·자리표시: 'hh:mm:ss', 'yyyy/mm/dd', '-', 'n/a'
_RX_FMT_CELL = re.compile(r"^(-+|n/?a|[hmsdy]{1,4}([:/.\- ][hmsdy]{1,4})+(\.[0s]+)?)$", re.I)


def _file_kind(path, nbytes=65536):
    """파일 앞부분 바이트로 종류 판별 → (종류, 인코딩 힌트).

    종류: 'text' | 'excel' | 'binary' | 'missing'. 확장자는 보지 않는다
    (.dat 은 텍스트 표일 수도, 장비 고유 형식의 바이너리일 수도 있음)."""
    try:
        with open(path, "rb") as f:
            head = f.read(nbytes)
    except OSError:
        return "missing", None
    if head.startswith((b"PK\x03\x04", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")):
        return "excel", None
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "text", "utf-16"
    if head.startswith(b"\xef\xbb\xbf"):
        return "text", "utf-8-sig"
    if b"\x00" in head:
        ev, od = head[0::2], head[1::2]      # BOM 없는 UTF-16: 한 바이트 건너 NUL
        if od.count(0) > 0.3 * len(od) and ev.count(0) < 0.1 * max(len(ev), 1):
            return "text", "utf-16-le"
        if ev.count(0) > 0.3 * len(ev) and od.count(0) < 0.1 * max(len(od), 1):
            return "text", "utf-16-be"
        return "binary", None
    if len(head) - len(head.translate(None, _CTRL_BYTES)) > 0.01 * max(len(head), 1):
        return "binary", None
    return "text", None


def _read_head_lines(path, enc_hint=None, max_lines=300):
    """앞쪽 줄들과 인코딩. 힌트(BOM/UTF-16)가 없으면 UTF-8 → cp949 → latin1 순으로 시도."""
    encs = [enc_hint] if enc_hint else ["utf-8-sig", "utf-8", "cp949", "euc-kr", "latin1"]
    for enc in encs:
        try:
            lines = []
            with open(path, "r", encoding=enc, newline="") as f:
                for _ in range(max_lines):
                    ln = f.readline()
                    if not ln:
                        break
                    lines.append(ln.rstrip("\r\n"))
            return lines, enc
        except UnicodeError:
            continue
        except OSError:
            break
    return [], None


def _split_cells(line, sep):
    if sep == " ":
        return line.split()
    if '"' not in line:
        return line.split(sep)
    try:
        return next(csv.reader([line], delimiter=sep))
    except Exception:
        return line.split(sep)


def _cell_kind(c, dec="."):
    """'' (빈칸) | 'num' | 'time' (날짜·시각) | 'text'"""
    c = c.strip().strip('"').strip()
    if not c:
        return ""
    try:
        float(c.replace(",", ".") if dec == "," else c)
        return "num"
    except ValueError:
        pass
    if _RX_DATE_CELL.match(c) or _RX_CLOCK_CELL.match(c) or c[:2] in ("오전", "오후"):
        return "time"
    return "text"


def _sniff_table(path, enc_hint=None, max_lines=300):
    """텍스트 표 구조 판별 (앞 300줄 기준). 표가 아니면 None.

    1) 구분자: 데이터 쪽(뒤 절반) 줄들의 칸 수가 일정한 것 — 탭 > 세미콜론 > 쉼표 > 세로줄 > 공백
    2) 데이터 시작: 끝에서부터 '칸 수가 같고 대부분 숫자·시각인 줄'이 이어지는 구간의 처음
    3) 헤더: 데이터 바로 위 줄들(최대 5줄) 중 이름다운 줄 — 나머지(단위·집계 표시 줄)는 건너뜀
    줄 번호는 모두 파일의 물리적 줄(0부터)."""
    lines, enc = _read_head_lines(path, enc_hint, max_lines)
    nz = [(i, ln) for i, ln in enumerate(lines) if ln.strip()]
    if len(nz) < 2:
        return None
    tail = nz[len(nz) // 2:] if len(nz) >= 10 else nz

    best = None
    for sep in ("\t", ";", ",", "|", " "):
        cnt = Counter(len(_split_cells(ln, sep)) for _, ln in tail)
        multi = [(k, v) for k, v in cnt.items() if k >= 2]
        if not multi:
            continue
        m_, f_ = max(multi, key=lambda kv: (kv[1], kv[0]))
        cand = (f_ / len(tail), m_, sep)
        if cand[0] >= 0.8:
            best = cand
            break
        if best is None or cand[:2] > best[:2]:
            best = cand
    if best is None or best[0] < 0.5:
        return None
    _, m, sep = best

    dec = "."
    if sep != ",":
        cells = [c.strip() for _, ln in tail for c in _split_cells(ln, sep)]
        n_comma = sum(1 for c in cells if _RX_DEC_COMMA.match(c))
        n_point = sum(1 for c in cells if _RX_DEC_POINT.match(c))
        if n_comma >= 5 and n_comma > 2 * n_point:
            dec = ","

    rows = []
    for i, ln in nz:
        cells = _split_cells(ln, sep)
        kinds = [_cell_kind(c, dec) for c in cells]
        ne = [k for k in kinds if k]
        rows.append({"i": i, "n": len(cells), "cells": cells, "kinds": kinds,
                     "ratio": (sum(1 for k in ne if k != "text") / len(ne)) if ne else 0.0})
    tol = max(1, m // 10)
    fits = lambda r: r["n"] >= 2 and abs(r["n"] - m) <= tol
    is_data = lambda r: fits(r) and r["ratio"] >= 0.6

    def header_like(r):
        txt = [c.strip().strip('"') for c, k in zip(r["cells"], r["kinds"]) if k == "text"]
        return fits(r) and r["ratio"] < 0.5 and len(txt) >= 2

    # 데이터 구간: 끝에서부터 위로 (마지막 2줄은 잘린 줄일 수 있어 건너뛸 수 있음)
    k = len(rows) - 1
    while k > 0 and not is_data(rows[k]) and k >= len(rows) - 3:
        k -= 1
    if not is_data(rows[k]):
        return None
    start, j = k, k - 1
    while j >= 0:
        if is_data(rows[j]):
            start = j
        elif not (j >= 1 and is_data(rows[j - 1]) and rows[j - 1]["n"] == rows[start]["n"]
                  and not header_like(rows[j])):
            break      # 데이터 사이에 낀 한 줄(이벤트 표시·잘린 줄)은 건너뛰고, 그 밖에는 멈춤
        j -= 1

    # 헤더: 데이터 바로 위 최대 5줄 (칸 수가 맞는 줄이 이어지는 동안)
    cand = []
    c = start - 1
    while c >= 0 and len(cand) < 5 and fits(rows[c]) and not is_data(rows[c]):
        cand.append(c)
        c -= 1

    def uniq_text(r):
        return {x.strip().strip('"') for x, kd in zip(r["cells"], r["kinds"]) if kd == "text"}

    def name_score(r):
        # 이름 줄 > 단위 줄('s','°C','[V]','hh:mm:ss','-' …)·집계 줄('Avg','Smp').
        # 시간 열 이름('Time','TIMESTAMP','시간')이 있으면 이름 줄일 가능성이 큼
        u = uniq_text(r)
        unit = sum(1 for x in u if _RX_UNIT_CELL.match(x) or _RX_FMT_CELL.match(x))
        has_t = any(re.match(r"(date[ _/-]?)?time(stamp)?\b|date\b|시간|시각|일시|날짜", x.lower()) for x in u)
        return len(u) - 2 * unit + 3 * has_t
    h = None
    if len(cand) == 1:      # 후보가 한 줄이면 이름이 'V','A','W' 같아도 헤더로 인정
        h = cand[0] if len(uniq_text(rows[cand[0]])) >= 2 else None
    elif cand:
        # cand 는 데이터에 가까운 순 → 뒤집어서 동점이면 위쪽 줄(이름 줄이 단위 줄보다 위에 옴)
        h = max(reversed(cand), key=lambda c: name_score(rows[c]))
        if name_score(rows[h]) <= 0 and len(uniq_text(rows[h])) < 2:
            h = None
    # 파일 앞쪽에 같은 헤더가 또 있으면(로거 재시작으로 헤더 반복) 첫 헤더부터 읽음
    if h is not None:
        for x in range(h):
            if rows[x]["cells"] == rows[h]["cells"]:
                start = x + (start - h)
                h = x
                break

    data_rows = [r for r in rows[start:] if is_data(r)]
    width = max([r["n"] for r in data_rows] + [m])
    if h is not None:
        hdr = rows[h]
        names = [x.strip().strip('"').strip() for x in hdr["cells"]]
        if sep == " " and len(names) != m:                  # 공백 정렬 표: 이름 안의 공백 처리
            alt = [x.strip('"') for x in re.split(r"\t+|\s{2,}", lines[hdr["i"]].strip())]
            if len(alt) == m:
                names = alt
    else:
        names = []
    # 공백 구분에서 'YYYY/MM/DD hh:mm:ss' 가 두 칸으로 갈라진 경우 → 읽은 뒤 합침
    dt_merge = False
    if sep == " " and h is not None and len(names) == m - 1:
        smp = data_rows[:20]
        dt_merge = bool(smp) and all(
            _RX_DATE_CELL.match(r["cells"][0]) and _RX_CLOCK_CELL.match(r["cells"][1]) for r in smp)
    blank = []
    for jx, nm in enumerate(names):
        if not nm:
            names[jx] = f"Col{jx + 1}"
            blank.append(names[jx])
    if not names:
        names = [f"Col{jx + 1}" for jx in range(m)]
    return {
        "enc": enc, "sep": sep, "dec": dec, "ncols": m, "width": width,
        "header": rows[h]["i"] if h is not None else None,
        "data_line": rows[start]["i"],
        "units": (start - h - 1) if h is not None else 0,
        "preamble": h if h is not None else start,
        "names": names, "blank_names": blank, "dt_merge": dt_merge,
        "rows": len(data_rows), "first": nz[0][1][:80],
    }


def _read_generic_table(path, file_rules=None, src_key="nidaq", default_skip=0, nrows=None):
    """.csv/.tsv 가 아닌 텍스트 표(.dat/.txt 등) 읽기 — 구조는 _sniff_table 이 판별."""
    name = os.path.basename(str(path))

    def note(msg):
        msg = f"  [read_dat] {name}: {msg}"
        print(msg)
        _READ_NOTES.append(msg)

    kind, enc_hint = _file_kind(path)
    if kind == "binary":
        note("⚠️ 텍스트가 아닌 바이너리 파일이라 읽을 수 없음 — AMS 프로그램 .dat 이면 케이스 확정 후 "
             "'파일 선택' → [형식 학습] 을 한 번 하면 AMS 로 읽힘, 다른 장비면 CSV/텍스트로 내보내기 필요")
        return pd.DataFrame()
    if kind == "excel":
        note(f"⚠️ 엑셀 형식 파일 — {src_key.upper()} 는 텍스트 표만 읽음 (엑셀은 MX100 확장자 규칙으로)")
        return pd.DataFrame()
    if kind == "missing":
        note("⚠️ 파일을 열 수 없음")
        return pd.DataFrame()
    try:
        st = _sniff_table(path, enc_hint)
    except Exception as e:
        note(f"⚠️ 형식 판별 실패: {type(e).__name__}: {str(e)[:150]}")
        return pd.DataFrame()
    if st is None:
        note("⚠️ 표 형태(구분자로 나뉜 열 + 숫자 데이터 줄)를 찾지 못해 읽지 않음")
        return pd.DataFrame()

    names = list(st["names"])
    if st["dt_merge"]:
        names = ["__date__", "__clock__"] + names[1:]
    if len(names) < st["width"]:
        names += [f"_extra_{k + 1}" for k in range(st["width"] - len(names))]
    names = _dedupe_names(names)
    kw = dict(sep=r"\s+" if st["sep"] == " " else st["sep"], header=None, names=names,
              skiprows=st["data_line"], index_col=False, encoding=st["enc"],
              encoding_errors="replace", decimal=st["dec"])
    if nrows:
        kw["nrows"] = nrows
    bad = []
    try:
        df = pd.read_csv(path, low_memory=False, **kw)
    except Exception:
        def _cut(row):
            bad.append(len(row))
            return row[:len(names)]
        try:
            df = pd.read_csv(path, engine="python", on_bad_lines=_cut, **kw)
        except Exception as e:      # 이상한 파일 하나 때문에 케이스 병합 전체가 멈추지 않게
            note(f"⚠️ 읽기 실패: {type(e).__name__}: {str(e)[:150]}")
            return pd.DataFrame()

    if st["dt_merge"] and "__date__" in df.columns:
        df.insert(0, st["names"][0],
                  df.pop("__date__").astype(str) + " " + df.pop("__clock__").astype(str))
    df.columns = [str(c).strip() for c in df.columns]
    # 파일 중간에 반복된 헤더 줄(로거 재시작) 제거 → 그 열들이 문자형으로 남지 않게
    n_rep = 0
    if st["header"] is not None and len(df):
        hdr_vals = [str(x) for x in st["names"]][:len(df.columns)]
        k = min(3, len(hdr_vals))
        if k >= 2:
            rep = np.ones(len(df), dtype=bool)
            for jx in range(k):
                rep &= (df.iloc[:, jx].astype(str).str.strip() == hdr_vals[jx]).to_numpy()
            n_rep = int(rep.sum())
            if n_rep:
                df = df.loc[~rep].reset_index(drop=True)
                for c in df.columns[1:]:
                    if df[c].dtype == object or str(df[c].dtype).startswith("str"):
                        conv = pd.to_numeric(df[c], errors="coerce")
                        if conv.notna().sum() == df[c].notna().sum():
                            df[c] = conv
    # 값이 전부 빈 '이름 없는 칸'·'데이터보다 넓은 헤더 칸'(행 끝 구분자, LabVIEW Comment 등) 제거
    extra_hdr = set(names[st["width"]:])
    drop = [c for c in df.columns
            if (c.startswith("_extra_") or c in st["blank_names"] or c in extra_hdr) and df[c].isna().all()]
    df = df.drop(columns=drop)

    enc_name = {"utf-8-sig": "UTF-8", "utf-8": "UTF-8", "cp949": "CP949(한글)", "euc-kr": "EUC-KR(한글)",
                "utf-16": "UTF-16", "utf-16-le": "UTF-16", "utf-16-be": "UTF-16"}.get(st["enc"], st["enc"])
    parts = [f"구분자 {_SEP_NAMES.get(st['sep'], repr(st['sep']))}", f"인코딩 {enc_name}"]
    if st["dec"] == ",":
        parts.append("소수점 쉼표")
    parts.append(f"헤더 {st['header'] + 1}번째 줄" if st["header"] is not None
                 else "헤더 줄 없음(열 이름 Col1, Col2 …)")
    if st["preamble"]:
        parts.append(f"앞쪽 설명 {st['preamble']}줄 건너뜀")
    if st["units"]:
        parts.append(f"헤더 아래 단위·보조 줄 {st['units']}개 건너뜀")
    if st["dt_merge"]:
        parts.append("날짜·시각 두 칸을 한 칸으로 합침")
    if n_rep:
        parts.append(f"중간에 반복된 헤더 줄 {n_rep}개 제외")
    note(f"텍스트 표로 읽음 — {', '.join(parts)} → {len(df):,}행 × {len(df.columns)}열")
    if bad:
        note(f"⚠️ 칸 수가 다른 줄 {len(bad)}개 → 초과분 잘라서 읽음")
    return df


def read_source_csv(path, file_rules, src_key, default_skip=1, nrows=None):
    """csv/tsv 읽기 — 인코딩 폴백 + 헤더 행 자동 탐지 + 레이아웃 보정.

    앱(Merge)·변수 스캔·진단 도구(tools/diagnose.py)가 같은 경로를 쓰도록 모듈 수준에 둠.
    (스캔이 따로 읽으면 BOM·헤더 행·쉼표 헤더 처리 차이로 변수명이 Merge 와 달라짐)
    nrows: 앞쪽 일부 행만 읽기 (변수 스캔용)
    .csv/.tsv 가 아닌 확장자(.dat 등)는 내용으로 구조를 판별하는 _read_generic_table 로.
    """
    path = str(path)
    if os.path.splitext(path)[1].lower() not in (".csv", ".tsv") and src_key == "ams" \
            and _file_kind(path)[0] == "binary":
        def _note(msg):
            print(msg)
            _READ_NOTES.append(msg)
        try:
            return ams_dat.read_dat(path, nrows=nrows, log=_note)
        except Exception as e:
            _note(f"  [ams_dat] {os.path.basename(path)}: ⚠️ 읽기 실패: {type(e).__name__}: {str(e)[:150]}")
            return pd.DataFrame()
    if os.path.splitext(path)[1].lower() not in (".csv", ".tsv"):
        # AMS/BR 로그가 .dat 로 저장된 경우: 쉼표·탭 구분이면 기존 csv 경로로 읽어
        # .csv 와 똑같은 열 이름(쉼표 포함 헤더 보정 등)을 만든다 → 이름 매핑·변수 표가 그대로 맞음
        st = None
        if src_key in ("ams", "br"):
            kind, enc_hint = _file_kind(path)
            if kind == "text" and not (enc_hint or "").startswith("utf-16"):
                try:
                    st = _sniff_table(path, enc_hint)
                except Exception:
                    st = None
        if not st or st["sep"] not in (",", "\t"):
            return _read_generic_table(path, file_rules, src_key, default_skip, nrows)
        sep = st["sep"]
    else:
        sep = '\t' if path.lower().endswith('.tsv') else ','
    skip = (file_rules or {}).get(src_key, {}).get("skip_rows", default_skip)

    # 인코딩 결정
    enc_used = None
    for enc in ["utf-8-sig", "utf-8", "cp949", "euc-kr", "latin1"]:
        try:
            with open(path, "r", encoding=enc) as f:
                for _ in range(8):
                    if not f.readline():
                        break
            enc_used = enc
            break
        except Exception:
            continue
    if enc_used is None:
        enc_used = "latin1"

    # 앞부분 행을 읽어 헤더 후보 점수화
    try:
        with open(path, "r", encoding=enc_used, errors="replace") as f:
            head_lines = [f.readline().rstrip("\r\n") for _ in range(8)]
    except Exception:
        head_lines = []

    def _score(line):
        if not line:
            return -1
        cells = [c.strip().strip('"') for c in line.split(sep)]
        cells = [c for c in cells if c != ""]
        if len(cells) < 2:
            return -1
        def is_num(x):
            try:
                float(x.replace(",", ""))
                return True
            except Exception:
                return False
        # 헤더다움: 값이 대부분 비숫자 + 컬럼 수가 많음
        non_num = sum(1 for c in cells if not is_num(c))
        uniq = len(set(cells))
        return non_num * 2 + len(cells) + (uniq == len(cells)) * 3

    best_skip = skip
    if head_lines:
        scores = {i: _score(l) for i, l in enumerate(head_lines)}
        cand = max(scores, key=scores.get)
        # 설정값이 명백히 나쁠 때만 교체
        if scores.get(cand, -1) > scores.get(skip, -1) + 3:
            best_skip = cand
            _m = (f"  [read_csv] {os.path.basename(path)}: "
                  f"헤더 행 자동 조정 skip_rows {skip} → {best_skip}")
            print(_m); _READ_NOTES.append(_m)

    skiparg = list(range(best_skip)) if best_skip else None
    def _read_with_layout(enc, sk):
        lay = _csv_layout(path, enc, sep, sk)
        if lay is None:
            return None
        names = _dedupe_names(lay["names"])
        if lay["note"]:
            msg = f"  [read_csv] {os.path.basename(path)}: {lay['note']}"
            print(msg); _READ_NOTES.append(msg)
        kw = dict(encoding=enc, sep=sep, header=None, names=names,
                  skiprows=sk + 1, index_col=False, encoding_errors="replace")
        if nrows:
            kw["nrows"] = nrows
        try:
            return pd.read_csv(path, low_memory=False, **kw)
        except Exception as e_fast:
            # 칸 수가 다른 행이 있으면 python 엔진으로 초과분을 잘라서 읽음 (느림) — 반드시 보고
            bad = []
            def _cut(row):
                bad.append(len(row))
                return row[:len(names)]
            df_ = pd.read_csv(path, engine="python", on_bad_lines=_cut, **kw)
            if nrows:
                return df_   # 스캔용 부분 읽기 — 전체 파일 칸 수 집계는 생략
            # python 파서는 index_col=False 일 때 초과 칸을 콜백 없이 잘라버려 직접 셈 (느린 경로에서만 실행)
            import csv as _csv
            from collections import Counter as _C
            cnt, lines_ = _C(), []
            with open(path, encoding=enc, errors="replace", newline="") as fh:
                rd_ = _csv.reader(fh, delimiter=sep)
                for i_, row_ in enumerate(rd_):
                    if i_ <= sk or not row_ or len(row_) <= len(names):
                        continue
                    cnt[len(row_)] += 1
                    if len(lines_) < 3:
                        lines_.append(rd_.line_num)
            bad = [w for w, n in cnt.items() for _ in range(n)]
            dist = ", ".join(f"{w}칸 {n}행" for w, n in sorted(cnt.items())) or "개수 미상"
            first = f"{lines_}번째 줄" if lines_ else str(e_fast).strip().splitlines()[-1][:90]
            msg = (f"  [read_csv] {os.path.basename(path)}: ⚠️ 칸 수가 다른 행 {len(bad)}개({dist}) "
                   f"→ 초과분 잘라서 읽음 / 느린 파서로 전환되어 읽기 시간 증가 (첫 위치: {first})")
            print(msg); _READ_NOTES.append(msg)
            return df_

    for enc in [enc_used, "utf-8", "cp949", "latin1"]:
        try:
            df = _read_with_layout(enc, best_skip)
            if df is None:
                continue
            df.columns = [str(c).strip() for c in df.columns]
            df = df.drop(columns=[c for c in df.columns if c.startswith("_trail_")], errors="ignore")
            return df
        except Exception as e:
            msg = f"  [read_csv] {os.path.basename(path)} ({enc}) 읽기 실패: {type(e).__name__}: {e}"
            print(msg); _READ_NOTES.append(msg)
            continue
    return pd.DataFrame()


def _save_csv_verified(df, path, sid=None, label=""):
    """CSV 저장 후 실제 파일 존재/크기를 검증. 실패 시 로그 남기고 False."""
    from pathlib import Path as _P
    path = _P(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False)
    except Exception as e:
        msg = f"  ❌ 저장 실패 [{label}] {path}: {type(e).__name__}: {e}"
        print(msg)
        if sid: _log(sid, msg)
        return False
    if not path.exists() or path.stat().st_size == 0:
        msg = f"  ❌ 저장 후 파일 없음/0바이트 [{label}] {path}"
        print(msg)
        if sid: _log(sid, msg)
        return False
    return True


def _copy_verified(src, dst, sid=None):
    """원본 폴더 복사. 실패해도 진행하되 반드시 사유를 로그로 남긴다."""
    import shutil
    from pathlib import Path as _P
    src, dst = _P(src), _P(dst)
    try:
        if not dst.parent.exists():
            msg = f"  ⚠️ 원본 폴더 없음 — 복사 생략: {dst.parent}"
            print(msg)
            if sid: _log(sid, msg)
            return False
        shutil.copy2(str(src), str(dst))
        if dst.exists() and dst.stat().st_size > 0:
            if sid: _log(sid, f"  📁 원본 폴더 복사: {dst}")
            return True
        msg = f"  ⚠️ 복사 후 파일 확인 실패: {dst}"
    except PermissionError as e:
        msg = f"  ⚠️ 복사 권한 오류 (파일이 Excel 등에서 열려있는지 확인): {dst} — {e}"
    except OSError as e:
        msg = f"  ⚠️ 복사 실패 (경로 길이/네트워크 확인): {dst} — {type(e).__name__}: {e}"
    except Exception as e:
        msg = f"  ⚠️ 복사 실패: {dst} — {type(e).__name__}: {e}"
    print(msg)
    if sid: _log(sid, msg)
    return False


def _record_source(out_name, src_dir):
    """결과 파일 → 원본 폴더 매핑을 _sources.json 에 기록."""
    import json
    meta = RESULT_DIR / "_sources.json"
    data = {}
    if meta.exists():
        try:
            data = json.loads(meta.read_text("utf-8"))
        except Exception:
            data = {}
    data[out_name] = str(src_dir)
    try:
        meta.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    except Exception as e:
        print(f"  [sources] 기록 실패: {e}")


def _lookup_source(out_name):
    """_sources.json 에서 원본 폴더를 조회 (없으면 None)."""
    import json
    meta = RESULT_DIR / "_sources.json"
    if not meta.exists():
        return None
    try:
        data = json.loads(meta.read_text("utf-8"))
    except Exception:
        return None
    for key in (out_name,
                out_name.replace("_calc.csv", "_merged.csv"),
                out_name.replace("_formula.csv", "_merged.csv")):
        if key in data:
            return Path(data[key])
    return None


# ══════════════════════════════════════════════
#  results 폴더 트리 — 하위 폴더(= Visualization 의 폴더) 지원
#  파일은 지금처럼 '파일 이름'으로 주고받고, 서버가 results 아래 실제 위치를 찾는다.
#  그래서 파일을 하위 폴더로 옮겨도 Merge·Calc·Viewer·Formula 가 그대로 동작한다.
#  같은 이름의 결과 파일은 results 전체에서 하나만 둔다 (이동·이름 변경 때 검사).
# ══════════════════════════════════════════════
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_BAD_NAME_CHARS = set('<>:"/\\|?*')


def _check_name(name, what="이름"):
    """Windows 에서도 쓸 수 있는 파일·폴더 이름인지 검사 → 정리된 이름. 문제 있으면 HTTP 400."""
    n = str(name or "").strip().rstrip(". ")
    if not n:
        raise HTTPException(400, f"{what}이 비어 있습니다")
    bad = sorted({c for c in n if c in _BAD_NAME_CHARS or ord(c) < 32})
    if bad:
        raise HTTPException(400, f"{what}에 쓸 수 없는 문자가 있습니다: {' '.join(bad)}  "
                                 f"(\\ / : * ? \" < > | 는 사용할 수 없음)")
    if n in (".", "..") or n.split(".")[0].strip().upper() in _WIN_RESERVED:
        raise HTTPException(400, f"'{n}' 은(는) Windows 예약어라 {what}으로 쓸 수 없습니다 "
                                 f"(CON, PRN, AUX, NUL, COM1~9, LPT1~9)")
    if len(n) > 150:
        raise HTTPException(400, f"{what}이 너무 깁니다 ({len(n)}자, 최대 150자)")
    return n


def _same_name(a, b):
    return a.lower() == b.lower() if os.name == "nt" else a == b


def _samefile(a, b):
    try:
        return os.path.samefile(a, b)
    except Exception:
        return False


def _hidden_dir(part):
    """'.' 숨김 폴더, '_' 프로그램용 폴더(_diagnose 등)는 결과 폴더로 보지 않음."""
    return part.startswith((".", "_"))


def _iter_results():
    """results 아래 모든 결과 CSV (하위 폴더 포함, 숨김·프로그램용 폴더 제외)."""
    for q in RESULT_DIR.rglob("*.csv"):
        try:
            rel = q.relative_to(RESULT_DIR)
            if q.is_file() and not any(_hidden_dir(part) for part in rel.parts[:-1]):
                yield q
        except Exception:
            continue


def _res_find(name):
    """파일 이름 → results 트리 안의 실제 위치 (루트 우선, 없으면 하위 폴더). 없으면 None."""
    name = Path(str(name or "").replace("\\", "/")).name
    if not name:
        return None
    p = RESULT_DIR / name
    if p.is_file():
        return p
    hits = [q for q in _iter_results() if _same_name(q.name, name)]
    if not hits:
        return None
    hits.sort(key=lambda q: q.stat().st_mtime, reverse=True)
    return hits[0]


def _res_file(name):
    """읽기용 위치. 없으면 404."""
    p = _res_find(name)
    if p is None:
        raise HTTPException(404, f"파일 없음: {name}")
    return p


def _res_target(name, near=None):
    """쓰기용 위치: 같은 이름 파일이 이미 있으면 그 자리(사용자가 옮긴 폴더 유지),
    없으면 near(관련 파일)와 같은 폴더, 그것도 없으면 results 루트."""
    name = Path(str(name)).name
    hit = _res_find(name)
    if hit is not None:
        return hit
    if near is not None:
        return Path(near).parent / name
    return RESULT_DIR / name


def _res_folder_of(p):
    rel = Path(p).parent.relative_to(RESULT_DIR).as_posix()
    return "" if rel == "." else rel


def _res_folder_path(folder):
    """'A' 또는 'A/B' → results 안의 실제 경로. 각 단계 이름을 검사해 results 밖으로 못 나가게 함
    ('_'·'.' 으로 시작하는 프로그램용 폴더도 거부)."""
    folder = str(folder or "").strip().replace("\\", "/").strip("/")
    if not folder:
        return RESULT_DIR
    return RESULT_DIR.joinpath(*[_check_folder_name(x) for x in folder.split("/") if x.strip()])


def _res_rel(p):
    """results 안의 실제 폴더 경로 → 'A/B' ('' = results 바로 아래)."""
    rel = Path(p).relative_to(RESULT_DIR).as_posix()
    return "" if rel == "." else rel


def _sources_load():
    import json
    meta = RESULT_DIR / "_sources.json"
    if meta.exists():
        try:
            return json.loads(meta.read_text("utf-8"))
        except Exception:
            pass
    return {}


def _sources_save(data):
    import json
    try:
        (RESULT_DIR / "_sources.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        return True
    except Exception as e:
        print(f"  [sources] 기록 실패: {e}")
        return False


_IN_USE_MSG = "다른 프로그램(엑셀·탐색기 등)이 사용 중이라 바꾸지 못했습니다. 닫고 다시 시도하세요"


sessions: dict[str, dict] = {}
DEFAULT_CFG = load_config()


# ══════════════════════════════════════════════
#  Pydantic 모델
# ══════════════════════════════════════════════
class BrowseRequest(BaseModel):
    path: str

class SelectRequest(BaseModel):
    category_path: str
    case_names: list[str]

class MergeRequest(BaseModel):
    session_id: str
    config: Optional[dict] = None
    variable_settings: Optional[dict] = None  # {"col_name": {"include": bool, "weight": float, "bias": float}, ...}

class CalcRequest(BaseModel):
    session_id: Optional[str] = None
    config: Optional[dict] = None
    experimental: Optional[dict] = None    # {"load_kg", "imc_kg", "fmc_kg"}
    source_files: Optional[list[str]] = None  # 특정 merged 파일 지정
    variable_mapping: Optional[dict] = None   # {"calc_var": "merged_col", ...}


# ══════════════════════════════════════════════
#  설정 API
# ══════════════════════════════════════════════
@app.get("/api/config")
def get_config():
    return DEFAULT_CFG

@app.put("/api/config")
def update_config(body: dict):
    global DEFAULT_CFG
    DEFAULT_CFG = body
    yaml_path = BASE_DIR / "config" / "default_config.yaml"
    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.dump(DEFAULT_CFG, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    return {"status": "ok"}


@app.get("/api/saves/{category}")
def list_saves(category: str):
    """세이브 슬롯 목록. category: merge/calc/formula"""
    import json
    d = BASE_DIR / "config" / "saves" / category
    d.mkdir(parents=True, exist_ok=True)
    slots = []
    for f in sorted(d.glob("*.json"), key=os.path.getmtime, reverse=True):
        try:
            meta = json.loads(f.read_text("utf-8"))
            slots.append({"name": f.stem, "timestamp": meta.get("timestamp", ""), "size": f.stat().st_size})
        except:
            slots.append({"name": f.stem, "timestamp": "", "size": f.stat().st_size})
    return {"slots": slots}


@app.post("/api/saves/{category}/{name}")
def save_slot(category: str, name: str, req: dict):
    """세이브 슬롯에 저장."""
    import json
    d = BASE_DIR / "config" / "saves" / category
    d.mkdir(parents=True, exist_ok=True)
    req["timestamp"] = pd.Timestamp.now().isoformat()
    req["slot_name"] = name
    (d / f"{name}.json").write_text(json.dumps(req, ensure_ascii=False, indent=2), "utf-8")
    return {"saved": name, "category": category}


@app.get("/api/saves/{category}/{name}")
def load_slot(category: str, name: str):
    """세이브 슬롯에서 불러오기."""
    import json
    p = BASE_DIR / "config" / "saves" / category / f"{name}.json"
    if not p.exists():
        raise HTTPException(404, f"슬롯 없음: {name}")
    return json.loads(p.read_text("utf-8"))


@app.delete("/api/saves/{category}/{name}")
def delete_slot(category: str, name: str):
    """세이브 슬롯 삭제."""
    p = BASE_DIR / "config" / "saves" / category / f"{name}.json"
    if not p.exists():
        raise HTTPException(404, f"슬롯 없음: {name}")
    p.unlink()
    return {"deleted": name}


@app.get("/api/default-merge-settings")
def get_default_merge_settings():
    """기본 Merge 변수 설정 반환."""
    p = BASE_DIR / "config" / "merge_settings_BR.json"
    if not p.exists():
        return {"variable_settings": {}}
    import json
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


@app.post("/api/save-default-merge-settings")
def save_default_merge_settings(req: dict):
    """현재 Merge 변수 설정을 디폴트로 저장."""
    import json
    p = BASE_DIR / "config" / "merge_settings_BR.json"
    data = {"version": 1, "type": "merge_settings",
            "timestamp": pd.Timestamp.now().isoformat(),
            "variable_settings": req.get("variable_settings", {})}
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    return {"saved": len(data["variable_settings"]), "path": str(p)}


@app.get("/api/default-calc-mapping")
def get_default_calc_mapping():
    """기본 Calculation 변수 매핑 반환."""
    import json
    p = BASE_DIR / "config" / "calc_mapping_default.json"
    if not p.exists():
        return {"mapping": {}}
    return json.loads(p.read_text("utf-8"))


@app.post("/api/save-default-calc-mapping")
def save_default_calc_mapping(req: dict):
    """현재 Calculation 변수 매핑을 디폴트로 저장."""
    import json
    p = BASE_DIR / "config" / "calc_mapping_default.json"
    data = {"version": 1, "type": "calc_mapping",
            "timestamp": pd.Timestamp.now().isoformat(),
            "mapping": req.get("mapping", {})}
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    return {"saved": len(data["mapping"]), "path": str(p)}


@app.get("/api/default-formula-settings")
def get_default_formula_settings():
    """기본 Formula 수식 설정 반환."""
    import json
    p = BASE_DIR / "config" / "formula_default.json"
    if not p.exists():
        return {"overrides": {}, "custom": []}
    return json.loads(p.read_text("utf-8"))


@app.post("/api/save-default-formula-settings")
def save_default_formula_settings(req: dict):
    """현재 Formula 수식을 디폴트로 저장."""
    import json
    p = BASE_DIR / "config" / "formula_default.json"
    data = {"version": 1, "type": "formula_settings",
            "timestamp": pd.Timestamp.now().isoformat(),
            "overrides": req.get("overrides", {}),
            "custom": req.get("custom", [])}
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    return {"saved_overrides": len(data["overrides"]),
            "saved_custom": len(data["custom"]), "path": str(p)}


@app.get("/api/default-viewer-settings")
def get_default_viewer_settings():
    """기본 Viewer 그래프 설정 반환."""
    import json
    p = BASE_DIR / "config" / "viewer_default.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text("utf-8"))


@app.post("/api/save-default-viewer-settings")
def save_default_viewer_settings(req: dict):
    """현재 Viewer 설정을 디폴트로 저장."""
    import json
    p = BASE_DIR / "config" / "viewer_default.json"
    req["timestamp"] = pd.Timestamp.now().isoformat()
    p.write_text(json.dumps(req, ensure_ascii=False, indent=2), "utf-8")
    return {"saved": True, "path": str(p)}


@app.get("/api/validate-refrigerant/{name}")
def validate_ref(name: str, backend: str = "HEOS"):
    """냉매명이 CoolProp/REFPROP에서 유효한지 검증."""
    try:
        from properties import validate_refrigerant
        return validate_refrigerant(name, backend)
    except Exception as e:
        return {"valid": False, "input": name, "coolprop_name": name,
                "error": f"검증 중 오류: {str(e)}"}


# ══════════════════════════════════════════════
#  경로 탐색 API
# ══════════════════════════════════════════════
@app.post("/api/browse")
def browse_directory(req: BrowseRequest):
    p = Path(req.path)
    if not p.exists(): raise HTTPException(404, f"경로 없음: {req.path}")
    if not p.is_dir(): raise HTTPException(400, f"디렉토리 아님: {req.path}")
    items = []
    try:
        for item in sorted(p.iterdir()):
            if item.name.startswith("."): continue
            items.append({"name": item.name, "is_dir": item.is_dir(),
                          "size": item.stat().st_size if item.is_file() else None})
    except PermissionError:
        raise HTTPException(403, f"접근 권한 없음: {req.path}")
    return {"path": str(p.resolve()), "items": items,
            "cases": [i["name"] for i in items if i["is_dir"]]}


_SRC_KEYS = ("br", "ams", "mx100", "nidaq")
_DATA_EXTS = (".csv", ".tsv", ".dat", ".txt", ".xls", ".xlsx", ".xlsm")
_GEN_TAGS = ("merged", "calc", "result", "formula", ".cache")
_TAG_TAIL = re.compile(r"(_br|_ams|_temp)$", re.I)


def _stem_key(name: str) -> str:
    """'test_br.csv' → 'test' — 앱이 붙이는 _br/_ams/_temp 를 떼고 비교 (같은 측정 짝 찾기)."""
    return _TAG_TAIL.sub("", Path(name).stem.lower())


def _case_entries(case_dir: Path, file_rules: dict | None, sources: list | None,
                  overrides: dict | None = None, pairs: dict | None = None):
    """케이스 폴더의 파일 판별 + 사용자 선택 적용.

    overrides: {파일 이름: 소스키 | None(사용 안 함)} — 화면에서 고른 것. 자동 판별보다 우선.
    pairs: {AMS .dat: 같은 측정 CSV} — 앱에서 형식을 학습한 짝.
    같은 측정의 AMS CSV 가 함께 있는 AMS 바이너리 .dat 는 기본으로 뺀다 (같은 데이터가 두 번 병합되지 않게).
    반환: (소스별 파일 {br:[…], …}, 미분류 [{file, why}], 화면용 파일 목록 [{file, size, kind, auto, dflt, src, why, learn, csvs}])"""
    um: list = []
    cf = _classify_files(case_dir, file_rules, sources, unmatched=um)
    auto = {f: k for k, lst in cf.items() for f in lst}
    why = {u["file"]: u["why"] for u in um}
    ams_csv = {_stem_key(f): f for f in cf.get("ams", []) if f.lower().endswith(".csv")}
    for f in list(cf.get("ams", [])):
        if not f.lower().endswith(".dat"):
            continue
        twin = (pairs or {}).get(f) if (pairs or {}).get(f) in cf.get("ams", []) else ams_csv.get(_stem_key(f))
        if twin:
            cf["ams"].remove(f)
            why[f] = f"같은 측정의 CSV({twin})가 함께 있어 기본으로 뺌 — 체크하면 .dat 를 사용"
    dflt = {f: k for k, lst in cf.items() for f in lst}
    ov = {}
    for f, src in (overrides or {}).items():
        if not (case_dir / f).is_file():
            continue
        for k in cf:
            if f in cf[k]:
                cf[k].remove(f)
        if src in cf:
            cf[src] = sorted(cf[src] + [f])
        ov[f] = src
    if ov:
        um = [u for u in um if u["file"] not in ov]
        print(f"  [case] {case_dir.name}: 화면에서 고른 파일 {len(ov)}개 적용 "
              + ", ".join(f"{f}→{v.upper() if v else '사용 안 함'}" for f, v in list(ov.items())[:6]))
    eff = {f: k for k, lst in cf.items() for f in lst}
    csvs = sorted(x.name for x in case_dir.iterdir() if x.is_file() and x.suffix.lower() == ".csv"
                  and not any(t in x.name.lower() for t in _GEN_TAGS))
    entries = []
    for x in sorted(case_dir.iterdir()):
        if not x.is_file() or x.name.startswith("."):
            continue
        nl = x.name.lower()
        if any(t in nl for t in _GEN_TAGS):
            continue
        if not (x.name in auto or x.name in why or nl.endswith(_DATA_EXTS) or x.name in ov):
            continue
        kind = _file_kind(str(x))[0] if nl.endswith(".dat") else ""
        e = {"file": x.name, "size": x.stat().st_size, "kind": kind, "auto": auto.get(x.name),
             "dflt": dflt.get(x.name), "src": eff.get(x.name), "why": why.get(x.name, "")}
        if not e["auto"] and not e["why"]:
            e["why"] = "자동 판별 대상 아님 — 쓰려면 체크하고 소스를 고르세요"
        no_spec = False
        if kind == "binary" and nl.endswith(".dat") and (e["auto"] == "ams" or e["src"] == "ams"):
            # AMS 바이너리 .dat — 같은 폴더 .proj 로 읽히거나 학습한 형식이 있어야 읽힘
            how = ams_dat.readable(str(x), size=e["size"])
            no_spec = how is None
            if no_spec:
                e["warn"] = ("바이너리 .dat 인데 아직 읽을 수 없음 — AMS 측정 폴더의 프로젝트 파일(.proj)을 같은 폴더에 "
                             "두면 바로 읽힘 (또는 아래에서 같은 측정의 AMS CSV 로 [형식 학습]). 안 하면 병합에서 빠짐")
            else:
                e["via"] = how
        if nl.endswith(".dat") and kind in ("binary", "excel") and (e["auto"] != "ams" or no_spec):
            e["learn"] = True
            k0 = _stem_key(x.name)
            e["csvs"] = sorted(csvs, key=lambda c: (_stem_key(c) != k0, auto.get(c) != "ams", c))
        entries.append(e)
    return cf, um, entries


# ── 여러 파일 → 병합 회차 계획 ───────────────────────────
# BR·AMS 파일마다 첫·마지막 시각을 읽어서
#   · 시간이 이어지는 파일(로그가 여러 파일로 나뉜 것)은 하나로 이어 붙이고
#   · 다른 소스 파일은 시간이 겹치는 회차에 배정한다.
# 시각을 모르는 파일(경과 시간 등)은 예전처럼 i번째끼리 짝짓는다.
_RX_HAS_DATE = re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}")
_RX_HMS = re.compile(r"^\s*(\d{1,2}):(\d{2}):(\d{2}(?:\.\d+)?)\s*$")
_SPAN_CACHE: dict = {}
_SEG_GAP_S = 1800          # 이 이내로 이어지면 같은 시험의 조각으로 봄 (30분)
_BR_TIME_COLS = ("LogTime", "logtime", "Log Time", "Time", "DateTime", "Timestamp")


def _parse_time_str(v):
    """시각 글자 → ('abs', 1970 기준 초) | ('tod', 하루 중 초) | None (경과 시간 등)."""
    t = str(v).strip().strip('"')
    if not t or t.lower() == "nan":
        return None
    if _RX_HAS_DATE.search(t):
        ts = pd.to_datetime(t, errors="coerce")
        if pd.isna(ts):
            return None
        return "abs", (ts.tz_localize(None) if ts.tzinfo else ts).value / 1e9
    m = _RX_HMS.match(t)
    if m:
        return "tod", int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    return None


def _tail_lines(path: str, enc: str | None, nbytes: int = 65536) -> list:
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        chunk = f.read()
    lines = chunk.decode(enc or "utf-8", errors="replace").splitlines()
    return [ln for ln in lines[1:] if ln.strip()] if size > nbytes else [ln for ln in lines if ln.strip()]


def _file_span(path: Path, src: str, file_rules: dict | None):
    """BR/AMS 파일의 첫·마지막 시각 → {"kind": "abs"|"tod", "start", "end"} (초) | None (모름)."""
    path = Path(path)
    try:
        stt = path.stat()
    except OSError:
        return None
    key = (str(path), src, stt.st_mtime, stt.st_size)
    if key in _SPAN_CACHE:
        return _SPAN_CACHE[key]
    span = None
    try:
        import contextlib, io
        p = str(path)
        kind, enc = _file_kind(p)
        if src == "ams" and kind == "binary":
            span = ams_dat.time_bounds(p)
        elif kind == "text":
            with contextlib.redirect_stdout(io.StringIO()):
                head = read_source_csv(p, file_rules or {}, src, 1, nrows=5)
            _READ_NOTES.clear()
            if head is not None and not head.empty:
                cols = [str(c) for c in head.columns]
                if src == "br":
                    tc = next((c for c in _BR_TIME_COLS if c in cols), cols[0])
                else:
                    tc = "Time" if "Time" in cols else cols[0]
                first = next((r for r in (_parse_time_str(v) for v in head[tc]) if r), None)
                if first:
                    idx = cols.index(tc)
                    if p.lower().endswith(".tsv"):
                        sep = "\t"
                    elif p.lower().endswith(".csv"):
                        sep = ","
                    else:
                        st_ = _sniff_table(p, enc)
                        sep = st_["sep"] if st_ else ","
                    last = None
                    for ln in reversed(_tail_lines(p, enc)[-30:]):
                        cells = _split_cells(ln, sep)
                        if idx < len(cells):
                            r = _parse_time_str(cells[idx])
                            if r and r[0] == first[0]:
                                last = r
                                break
                    if last:
                        a0, a1 = first[1], last[1]
                        if first[0] == "tod" and a1 < a0:
                            a1 += 86400.0
                        span = {"kind": first[0], "start": float(a0), "end": float(a1)}
    except Exception:
        span = None
    _SPAN_CACHE[key] = span
    return span


def _span_cmp(a: dict, b: dict):
    """두 시각 범위를 같은 기준으로 (둘 다 날짜가 있으면 날짜까지, 아니면 하루 중 시각으로)."""
    if a["kind"] == "abs" and b["kind"] == "abs":
        return (a["start"], a["end"]), (b["start"], b["end"])

    def tod(x):
        if x["kind"] == "tod":
            return x["start"], x["end"]
        s0 = x["start"] % 86400.0
        return s0, s0 + (x["end"] - x["start"])
    return tod(a), tod(b)


# ── 같은 시험을 동시에 기록한 AMS 파일 (다른 로거·보드) ──
# 시간이 겹치는데 열 구성이 다르면 조각이 아니라 나란히 놓을 파일 → 각각 전처리해서 1초 시간축에서 병합
_AMS_KEY_COLS = frozenset({"time", "packet index", "packetindex", "index", "no", "no.", "date", "datetime"})
_COLS_CACHE: dict = {}


def _file_head_cols(path: Path, src: str, file_rules: dict | None) -> frozenset:
    """파일의 열 이름 (시각·번호 열 제외, 소문자) — 동시 기록 판정용. 못 읽으면 빈 집합."""
    path = Path(path)
    try:
        stt = path.stat()
    except OSError:
        return frozenset()
    key = (str(path), src, stt.st_mtime, stt.st_size)
    if key in _COLS_CACHE:
        return _COLS_CACHE[key]
    cols = frozenset()
    n0 = len(_READ_NOTES)
    try:
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            head = read_source_csv(str(path), file_rules or {}, src, 1, nrows=5)
        if head is not None:
            cols = frozenset(str(c).strip().lower() for c in head.columns) - _AMS_KEY_COLS
    except Exception:
        pass
    del _READ_NOTES[n0:]
    _COLS_CACHE[key] = cols
    return cols


def _distinct_cols(a, b) -> bool:
    """열 구성이 다른 로거·보드 수준으로 다른가 — 공통 열이 적은 쪽 열 수의 절반 미만."""
    if not a or not b:
        return False
    return len(a & b) < 0.5 * min(len(a), len(b))


def _overlap_s(a: dict, b: dict) -> float:
    (a0, a1), (b0, b1) = _span_cmp(a, b)
    return min(a1, b1) - max(a0, b0)


def _ams_stream_groups(case_dir: Path, files: list, file_rules: dict | None) -> list:
    """한 회차의 AMS 파일 → 묶음 [[파일, …], …].
    · 시간이 이어지는 조각(로그가 여러 파일로 나뉜 것) → 한 묶음, 시간 순으로 이어 붙임
    · 시간이 겹치는데 열 구성이 다른 파일(같은 시험을 동시에 기록한 다른 로거·보드) → 다른 묶음,
      각각 전처리해서 1초 시간축에서 나란히 병합 (예전엔 이어 붙여서 겹치는 시각의 두 번째 파일 값이 빠졌음)"""
    files = list(files or [])
    if len(files) <= 1:
        return [files] if files else []
    info = [(f, _file_span(case_dir / f, "ams", file_rules), _file_head_cols(case_dir / f, "ams", file_rules))
            for f in files]
    info.sort(key=lambda x: (x[1] is None, (x[1] or {}).get("start", 0.0), x[0]))
    streams = []
    for f, sp, cols in info:
        best = None
        for s in streams:
            # 이 묶음에 시간이 겹치면서(시각을 모르면 겹친다고 봄) 열 구성이 다른 파일이 있으면 → 나란히
            if any(_distinct_cols(c2, cols) and (sp is None or s2 is None or _overlap_s(s2, sp) > 60)
                   for _, s2, c2 in s):
                continue
            allc = frozenset().union(*(c2 for _, _, c2 in s))
            sim = len(allc & cols) / max(1, len(allc | cols))
            if best is None or sim > best[0]:
                best = (sim, s)
        if best is not None:
            best[1].append((f, sp, cols))
        else:
            streams.append([(f, sp, cols)])
    return [[f for f, _, _ in s] for s in streams]


def _ref_source(cf: dict, dt: str, sources: list):
    """기준 시간축 소스 (설정한 기준 소스 파일이 없으면 BR → AMS → MX100 → NIDAQ 순으로 대체)."""
    ref_map = {"BR": "br", "AMS": "ams", "MX100": "mx100", "NIDAQ": "nidaq"}
    if dt in sources and cf.get(ref_map.get(dt, "br")):
        return dt, ref_map[dt]
    for alt in ("BR", "AMS", "MX100", "NIDAQ"):
        if alt in sources and cf.get(ref_map[alt]):
            return alt, ref_map[alt]
    return None, None


def _plan_runs(case_dir: Path, cf: dict, ref_key: str | None, file_rules: dict | None, mode: str = "auto"):
    """병합 회차 → ([{"br": [...], "ams": [...]}, ...], 설명 줄).

    mode: auto(시간으로 판단) | concat(소스마다 전부 이어 붙여 1회) | separate(예전처럼 i번째끼리 짝)"""
    notes = []
    if ref_key is None:
        return [], notes
    if ref_key not in ("br", "ams"):
        return [{"br": [], "ams": []}], notes
    other = "ams" if ref_key == "br" else "br"
    R, O = list(cf.get(ref_key, [])), list(cf.get(other, []))
    spans = {(k, f): _file_span(case_dir / f, k, file_rules) for k, lst in ((ref_key, R), (other, O)) for f in lst}
    by_start = lambda k: (lambda f: (spans.get((k, f)) is None, (spans.get((k, f)) or {}).get("start", 0), f))

    def with_streams(runs):
        # AMS 파일이 여럿인 회차: 이어 붙일 조각 / 나란히 놓을 동시 기록 파일 나누기
        for r in runs:
            r["ams_streams"] = _ams_stream_groups(case_dir, r.get("ams", []), file_rules)
            if len(r["ams_streams"]) > 1:
                notes.append("AMS " + " ‖ ".join(" + ".join(s) for s in r["ams_streams"])
                             + " — 시간이 겹치고 열 구성이 달라 동시에 기록한 파일로 보고 나란히 병합")
        return runs, notes

    if mode == "separate":
        return with_streams([{ref_key: [f], other: [O[i]] if i < len(O) else []} for i, f in enumerate(R)])
    if mode == "concat":
        return with_streams([{ref_key: sorted(R, key=by_start(ref_key)), other: sorted(O, key=by_start(other))}])

    # ① 기준 파일: 시간이 이어지면 한 회차로
    #    (AMS 가 기준일 때 시간이 겹치는데 열 구성이 다른 파일 = 동시에 기록한 다른 로거 → 같은 회차)
    if R and all(spans.get((ref_key, f)) for f in R):
        Rs = sorted(R, key=by_start(ref_key))
        groups = [[Rs[0]]]
        for f in Rs[1:]:
            sf = spans[(ref_key, f)]
            gaps = {}
            for g in groups[-1]:
                prev, cur = _span_cmp(spans[(ref_key, g)], sf)
                gaps[g] = cur[0] - prev[1]
            gap = min(gaps.values())
            same = -60 <= gap <= _SEG_GAP_S
            if not same and gap < -60 and ref_key == "ams":
                cols_f = _file_head_cols(case_dir / f, "ams", file_rules)
                ov = [g for g in groups[-1] if _overlap_s(spans[(ref_key, g)], sf) > 60]
                same = bool(ov) and all(_distinct_cols(_file_head_cols(case_dir / g, "ams", file_rules), cols_f)
                                        for g in ov)
            if same:
                groups[-1].append(f)
            else:
                groups.append([f])
    else:
        groups = [[f] for f in R]
        if len(R) > 1:
            notes.append(f"{ref_key.upper()} 파일 시각을 읽지 못한 것이 있어 파일마다 따로 병합")
    runs = [{ref_key: g, other: []} for g in groups]

    def run_span(g):
        ss = [spans.get((ref_key, f)) for f in g]
        if not all(ss):
            return None
        return {"kind": ss[0]["kind"], "start": min(x["start"] for x in ss), "end": max(x["end"] for x in ss)}
    rsp = [run_span(r[ref_key]) for r in runs]

    # ② 다른 소스 파일: 시간이 겹치는(가까운) 회차로
    unknown, dropped = [], []
    for f in O:
        sf = spans.get((other, f))
        if sf is None or not all(rsp):
            unknown.append(f)
            continue
        best, best_score = None, None
        for i, rs in enumerate(rsp):
            (r0, r1), (f0, f1) = _span_cmp(rs, sf)
            ov = min(r1, f1) - max(r0, f0)
            score = ov if ov > 0 else -min(abs(f0 - r1), abs(r0 - f1))
            if best_score is None or score > best_score:
                best, best_score = i, score
        if best is not None and best_score > -_SEG_GAP_S:
            runs[best][other].append(f)
        else:
            dropped.append(f)
    if unknown:
        if len(runs) == 1:
            runs[0][other] += unknown
            if len(unknown) > 1:
                notes.append(f"{other.upper()} 파일 시각을 읽지 못해 이름 순서로 이어 붙임: {', '.join(unknown)}")
        else:
            for i, f in enumerate(unknown):
                if i < len(runs):
                    runs[i][other].append(f)
                else:
                    dropped.append(f)
            notes.append(f"{other.upper()} 파일 시각을 읽지 못해 i번째 {ref_key.upper()} 파일과 짝지음")
    if dropped:
        notes.append(f"⚠️ 시간이 겹치는 {ref_key.upper()} 파일이 없어 뺀 {other.upper()} 파일: {', '.join(dropped)}")
    for r in runs:
        r[other] = sorted(r[other], key=by_start(other))
    return with_streams(runs)


def _plan_summary(runs: list, notes: list, mode: str) -> dict:
    joined = []
    for r in runs:
        for k in ("br", "ams"):
            fl = r.get(k, [])
            if len(fl) < 2:
                continue
            st = (r.get("ams_streams") or []) if k == "ams" else []
            if len(st) > 1:
                joined.append(f"AMS {len(fl)}개 → 동시 기록 {len(st)}묶음 나란히")
            else:
                joined.append(f"{k.upper()} {len(fl)}개 이어 붙임")
    return {"n": len(runs), "mode": mode, "notes": notes,
            "runs": [{"br": r.get("br", []), "ams": r.get("ams", []), "ams_streams": r.get("ams_streams", [])}
                     for r in runs],
            "joined": joined}


def _case_plan(case_dir: Path, cf: dict, mode: str = "auto", cfg: dict | None = None) -> dict:
    cfg = cfg or DEFAULT_CFG
    proc = cfg.get("processing", {})
    _, rk = _ref_source(cf, proc.get("data_time", "BR"), proc.get("data_sources", ["BR", "AMS", "MX100"]))
    runs, notes = _plan_runs(case_dir, cf, rk, cfg.get("file_rules"), mode)
    return _plan_summary(runs, notes, mode)


class CaseFilesReq(BaseModel):
    session_id: str
    case: str
    overrides: dict = {}
    multi: str | None = None      # 여러 파일 처리: auto | concat | separate


@app.post("/api/case-files")
def set_case_files(req: CaseFilesReq):
    """화면에서 고른 케이스 파일(사용/제외·소스) 저장 → 변수 스캔·Merge 가 이대로 사용."""
    s = sessions.get(req.session_id)
    if not s:
        raise HTTPException(404, "세션 없음 — 케이스를 다시 확정하세요")
    if req.case not in s.get("case_files", {}):
        raise HTTPException(404, f"케이스 없음: {req.case}")
    case_dir = Path(s["category_path"]) / req.case
    ren = s.get("renamed", {}).get(req.case, {})     # Merge 가 _br 등을 붙여 이름을 바꿨으면 화면의 옛 이름을 새 이름으로
    ov = {ren.get(Path(str(k)).name, Path(str(k)).name): (v if v in _SRC_KEYS else None)
          for k, v in (req.overrides or {}).items()}
    s.setdefault("overrides", {})[req.case] = ov
    if req.multi in ("auto", "concat", "separate"):
        s.setdefault("multi", {})[req.case] = req.multi
    cf, um, entries = _case_entries(case_dir, DEFAULT_CFG.get("file_rules"),
                                    DEFAULT_CFG.get("processing", {}).get("data_sources"),
                                    ov, s.get("pairs", {}).get(req.case))
    s["case_files"][req.case] = cf
    plan = _case_plan(case_dir, cf, s.get("multi", {}).get(req.case, "auto"))
    return {"case": req.case, "files": cf, "unmatched": um, "entries": entries, "plan": plan}


class AmsLearnReq(BaseModel):
    session_id: str
    case: str
    dat: str
    csv: str


@app.post("/api/ams-dat/learn")
def ams_dat_learn(req: AmsLearnReq):
    """앱 안에서 AMS .dat 형식 학습 — 케이스 폴더의 .dat 와 같은 측정의 CSV 를 비교 (tools/diagnose.py 와 같은 학습)."""
    s = sessions.get(req.session_id)
    if not s:
        raise HTTPException(404, "세션 없음 — 케이스를 다시 확정하세요")
    case_dir = Path(s["category_path"]) / req.case
    ren = s.get("renamed", {}).get(req.case, {})
    dat = case_dir / ren.get(Path(req.dat).name, Path(req.dat).name)
    if not dat.is_file():
        raise HTTPException(404, f".dat 파일 없음: {req.dat}")
    cp = Path(str(req.csv).strip().strip('"'))
    if not cp.is_absolute():
        cp = case_dir / ren.get(cp.name, cp.name)
    if not cp.is_file():
        raise HTTPException(404, f"CSV 파일 없음: {req.csv}")
    fr = DEFAULT_CFG.get("file_rules", {})
    _READ_NOTES.clear()
    df = read_source_csv(str(cp), fr, "ams", 1)
    _READ_NOTES.clear()
    if df is None or df.empty:
        raise HTTPException(400, f"CSV 를 읽지 못함: {cp.name}")
    t0 = time.perf_counter()
    res = ams_dat.learn(ams_dat.read_bytes(dat), df)
    fl = res.get("first_look") or {}
    out = {"ok": bool(res["ok"]), "why": res.get("why", ""), "notes": res.get("notes", []),
           "sec": round(time.perf_counter() - t0, 1),
           "look": {"size": fl.get("size"), "sig": fl.get("sig"), "entropy": round(float(fl.get("entropy", 0)), 2),
                    "record": (fl.get("period") or {}).get("p"), "records": fl.get("records")}}
    v = res.get("verify")
    if v:
        sp = res["spec"]
        out.update(value_pct=round(v["overall"] * 100, 3), time_pct=round(v["time_ok"] * 100, 2), rows=v["rows"],
                   columns=len(sp["columns"]), codes=len(sp["codes"]), unmapped=res.get("unmapped", []),
                   unmapped_const=res.get("unmapped_const", []), coverage=round(res.get("coverage", 0) * 100))
    if res["ok"]:
        out["spec"] = ams_dat.save_spec(res["spec"]).name
        if cp.parent == case_dir:
            s.setdefault("pairs", {}).setdefault(req.case, {})[dat.name] = cp.name
    print(f"  [ams_dat] 형식 학습 {dat.name} ↔ {cp.name}: {'성공 → ' + out['spec'] if out['ok'] else '실패 — ' + out['why']}")
    return out


@app.post("/api/select")
def select_cases(req: SelectRequest):
    category = Path(req.category_path)
    if not category.exists(): raise HTTPException(404, f"경로 없음: {req.category_path}")
    sid = str(uuid.uuid4())[:8]
    case_files, unmatched, entries, plans = {}, {}, {}, {}
    for case in req.case_names:
        case_dir = category / case
        if not case_dir.is_dir(): continue
        cf, um, ent = _case_entries(case_dir, DEFAULT_CFG.get("file_rules"),
                                    DEFAULT_CFG.get("processing",{}).get("data_sources"))
        case_files[case], entries[case] = cf, ent
        plans[case] = _case_plan(case_dir, cf)
        if um:
            unmatched[case] = um
    if not case_files: raise HTTPException(400, "유효한 케이스 없음")
    sessions[sid] = {
        "status": "ready", "progress": 0, "log": [], "error": None,
        "category_path": str(category), "case_files": case_files,
        "overrides": {}, "pairs": {}, "multi": {},
        "merge_results": [], "calc_results": [],
    }
    return {"session_id": sid, "cases": case_files, "unmatched": unmatched, "files": entries, "plans": plans}


def _peek_header(path: Path, skip_rows: int = 1) -> str:
    """CSV 앞부분 여러 행을 합쳐 반환 (헤더 행 위치가 달라도 시그니처 탐지 가능)."""
    for enc in ("utf-8-sig", "utf-8", "cp949", "latin1"):
        try:
            with open(path, "r", encoding=enc, errors="strict") as f:
                lines = [f.readline() for _ in range(6)]
            return "".join(l for l in lines if l).lower()
        except Exception:
            continue
    return ""


# 소스별 헤더 시그니처 (하나라도 있으면 해당 소스로 판정)
_SIG = {
    "ams": ["compcurrenthz", "eevcurrentposition", "_dry_duct_in_temp_",
            "_dry_eva_in_temp_", "requestrpm", "currentrpm",
            "superheatdegree", "功率", "耗电", "행정 정보"],
    "br": ["d_currentabs", "ctrl_", "hp_", "t_air_", "p_comp"],
}


def _sniff_source(path: Path, file_rules: dict, candidates=("ams", "br")) -> str | None:
    """헤더 내용으로 소스 판별. 확실하지 않으면 None."""
    if path.suffix.lower() != ".csv":
        return None
    scores = {}
    for src in candidates:
        skip = (file_rules.get(src, {}) or {}).get("skip_rows", 1)
        head = _peek_header(path, skip)
        if not head:
            continue
        scores[src] = sum(1 for sig in _SIG.get(src, []) if sig in head)
    if not scores:
        return None
    best = max(scores, key=scores.get)
    # 최소 2개 이상 매칭 + 2위와 차이가 있어야 확정
    others = [v for k, v in scores.items() if k != best]
    if scores[best] >= 2 and (not others or scores[best] > max(others)):
        return best
    return None


def _auto_text_source(path: Path, file_rules: dict):
    """식별 규칙에 안 맞은 .dat 를 내용으로 판별 → (소스 | None, 이유).

    AMS 헤더 특징이 있으면 AMS, 그 밖의 표 형태 계측 데이터는 NIDAQ(일반 계측)으로 본다.
    BR 은 열 이름 특징('t_air_', 'p_comp' 등)이 일반 계측 채널 이름과 겹쳐 자동으로 고르지 않음."""
    kind, enc = _file_kind(path)
    if kind == "binary":
        how = ams_dat.readable(str(path))     # 같은 폴더 .proj 또는 학습한 형식
        if how:
            if "ams" in file_rules:
                return "ams", f"AMS 바이너리 — {how}"
            return None, "AMS 바이너리지만 AMS 소스가 꺼져 있음 — Setting 의 사용 소스에서 AMS 를 켜면 병합됨"
        return None, ("바이너리 파일 — AMS 프로그램 .dat 이면 측정 폴더의 프로젝트 파일(.proj)을 같은 폴더에 두면 "
                      "바로 읽힘 (또는 '파일 선택'에서 같은 측정의 AMS CSV 로 [형식 학습])")
    if kind == "excel":
        return None, "엑셀 형식 — MX100 확장자 규칙에 넣으면 MX100 으로 읽음"
    if kind != "text":
        return None, "파일을 열 수 없음"
    lines, _ = _read_head_lines(path, enc, 40)
    head = "\n".join(lines).lower()
    if "ams" in file_rules and sum(1 for s in _SIG["ams"] if s in head) >= 2:
        return "ams", "AMS 헤더"
    st = _sniff_table(path, enc)
    if not st or st["rows"] < 2:
        return None, "표 형태 데이터가 아님"
    if "nidaq" in file_rules:
        return "nidaq", f"일반 계측 데이터 {st['ncols']}열"
    return None, "NIDAQ(일반 계측) 소스가 꺼져 있음 — Setting 의 사용 소스에서 NIDAQ 를 켜면 병합됨"


def _classify_files(folder: Path, file_rules: dict = None, active_sources: list = None,
                    unmatched: list | None = None) -> dict:
    """파일 식별 규칙에 따라 소스별 분류. active_sources로 필터.

    규칙에 안 맞은 .dat 는 내용으로 판별(_auto_text_source).
    unmatched 에 리스트를 주면 어느 소스에도 안 들어간 데이터 파일을 {file, why} 로 담는다."""
    if not file_rules:
        file_rules = {
            "mx100": {"extensions": [".xls", ".xlsx"], "include_patterns": []},
            "nidaq": {"extensions": [".tsv"], "include_patterns": []},
            "ams":   {"extensions": [".csv"], "include_patterns": ["_ams"]},
            "br":    {"extensions": [".csv"], "include_patterns": []},
        }
    # active_sources 필터: ["BR","MX100"] → {"br","mx100"}
    if active_sources:
        enabled = {s.lower() for s in active_sources}
        file_rules = {k: v for k, v in file_rules.items() if k in enabled}
    print(f"  [classify] active={active_sources}, rules={list(file_rules.keys())}")
    files = {k: [] for k in file_rules}
    order = [k for k in file_rules if k != "br"] + (["br"] if "br" in file_rules else [])
    for f in sorted(folder.iterdir()):
        if not f.is_file(): continue
        n = f.name.lower()
        if any(tag in n for tag in ["merged", "calc", "result", "formula", ".cache"]):
            continue
        # 파일명에 AMS 가 있으면 AMS (식별 규칙·내용 판별보다 우선) — AMS_log.csv, 260311_AMS.dat 등
        if "ams" in file_rules and ams_by_name(f.name):
            files["ams"].append(f.name)
            print(f"  [classify] 파일명 판별: {f.name} → AMS")
            continue
        matched = False
        for src in order:
            rule = file_rules[src]
            exts = rule.get("extensions", [])
            inc = rule.get("include_patterns", [])
            exc = rule.get("exclude_patterns", [])
            if exts and not any(n.endswith(e) for e in exts):
                continue
            if inc and not any(p.lower() in n for p in inc):
                continue
            if exc and any(p.lower() in n for p in exc):
                continue
            # br(폴백)로 잡힌 csv는 헤더 내용으로 재판별
            #   → 파일명에 _ams 가 없어도 AMS 파일을 올바르게 인식
            if src == "br" and "ams" in file_rules:
                sniff = _sniff_source(f, file_rules)
                if sniff and sniff != "br":
                    files[sniff].append(f.name)
                    print(f"  [classify] 내용 판별: {f.name} → {sniff.upper()}")
                    matched = True
                    break
            files[src].append(f.name)
            matched = True
            break
        if matched:
            continue
        if n.endswith(_GENERIC_TEXT_EXTS):
            try:
                src, why = _auto_text_source(f, file_rules)
            except Exception as e:   # 판별 오류로 케이스 선택 자체가 막히지 않게
                src, why = None, f"형식 판별 중 오류 ({type(e).__name__})"
            if src:
                files[src].append(f.name)
                print(f"  [classify] 내용 판별: {f.name} → {src.upper()} ({why})")
                continue
            print(f"  [classify] 미분류: {f.name} — {why}")
            if unmatched is not None:
                unmatched.append({"file": f.name, "why": why})
        elif n.endswith(('.csv','.tsv','.xls','.xlsx')):
            print(f"  [classify] 미분류: {f.name} (ext={f.suffix})")
            if unmatched is not None:
                unmatched.append({"file": f.name, "why": "식별 규칙(확장자·포함/제외 패턴)에 맞는 사용 소스 없음"})
    print(f"  [classify] 결과: {{{', '.join(f'{k}={len(v)}' for k,v in files.items())}}}")
    return files


# ══════════════════════════════════════════════
#  컬럼 스캔 API (Merge 전 변수 설정용)
# ══════════════════════════════════════════════
@app.post("/api/scan-columns")
def scan_columns(req: BrowseRequest):
    """
    선택한 **모든 케이스**의 BR/AMS/MX100/NIDAQ 파일(화면에서 고른 대로)을 읽어
    사용 가능한 전체 컬럼 목록을 반환한다 — 파일·케이스마다 변수가 달라도 전부 표시.
    (예전엔 첫 번째 케이스만 읽고, 못 읽은 파일은 서버 콘솔에만 남겨서
     AMS 파일이 둘이어도 한 파일 변수만 보일 수 있었음)
    columns[c].files: 그 변수가 있는 파일, files: 파일별 결과 (열 수 / 못 읽은 이유)
    """
    sid = req.path  # session_id를 path 필드로 받음
    if sid not in sessions:
        raise HTTPException(404, "세션 없음")
    s = sessions[sid]
    category = Path(s["category_path"])
    case_files = s["case_files"]

    all_columns = {}
    scanned = []           # [{case, file, src, cols, error?}]
    fr = DEFAULT_CFG.get("file_rules", {})
    active = DEFAULT_CFG.get("processing",{}).get("data_sources")
    t_start, budget_s, n_cases = time.perf_counter(), 40.0, 0

    def _read_mx_head(p):
        skip_r = fr.get("mx100", {}).get("skip_rows", 24)
        df = pd.read_excel(p, skiprows=skip_r, header=0, nrows=5)
        df.columns = [str(c).strip() for c in df.columns]
        return df

    def _scan_file(case_name, case_dir, label, fp_name, reader):
        n0 = len(_READ_NOTES)
        err, df = None, None
        try:
            df = reader(case_dir / fp_name)
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:200]}"
        if df is None or len(df.columns) == 0:
            notes = [x.strip() for x in _READ_NOTES[n0:] if x.strip()]
            why = err or (notes[-1] if notes else "열을 하나도 읽지 못함")
            scanned.append({"case": case_name, "file": fp_name, "src": label, "cols": 0, "error": why})
            print(f"  [scan] {case_name}/{fp_name} 읽기 실패: {why}")
            return
        for i, c in enumerate(df.columns):
            info = all_columns.get(c)
            if info is None:
                info = all_columns[c] = {"source": label, "dtype": str(df.iloc[:, i].dtype), "files": []}
            if fp_name not in info["files"]:
                info["files"].append(fp_name)
        scanned.append({"case": case_name, "file": fp_name, "src": label, "cols": int(len(df.columns))})

    for case_name in case_files:
        if n_cases and time.perf_counter() - t_start > budget_s:
            break   # 케이스가 아주 많을 때 — 나머지는 알림만
        n_cases += 1
        case_dir = category / case_name
        # 최신 파일 목록으로 재분류 + 화면에서 고른 파일(사용/제외·소스) 적용
        try:
            cf = _case_entries(case_dir, fr, active, s.get("overrides", {}).get(case_name),
                               s.get("pairs", {}).get(case_name))[0]
        except Exception as e:
            scanned.append({"case": case_name, "file": "", "src": "", "cols": 0,
                            "error": f"케이스 파일 판별 실패: {type(e).__name__}: {e}"})
            continue
        # Merge 와 같은 읽기 함수 사용 → 표에 보이는 이름 = merged 파일 컬럼 이름
        for src_key, label, skip in (("br", "BR", 1), ("ams", "AMS", 1), ("nidaq", "NIDAQ", 0)):
            for fp_name in cf.get(src_key, []):
                _scan_file(case_name, case_dir, label, fp_name,
                           lambda p, k=src_key, sk=skip: read_source_csv(str(p), fr, k, sk, nrows=5))
        for mx_name in cf.get("mx100", []):
            _scan_file(case_name, case_dir, "MX100", mx_name, _read_mx_head)
    if n_cases < len(case_files):
        print(f"  [scan] 시간이 오래 걸려 케이스 {n_cases}/{len(case_files)}개만 스캔")

    # ── config 기반 기본 선택값 (저장된 merge 설정이 없을 때의 폴백) ──
    # column_mappings + selected_columns + scale_factors 를 조합해
    # "원본 컬럼 → 표준명 + 가중치" 기본 체크 목록을 만든다.
    defaults = {}
    try:
        cm = DEFAULT_CFG.get("column_mappings", {}) or {}
        sc = DEFAULT_CFG.get("selected_columns", {}) or {}
        scale_map = {
            "ams": DEFAULT_CFG.get("ams_scale_factors", {}) or {},
            "blackrose": {},
        }
        # config 키(ams/blackrose) → selected_columns 키(ams/br)
        sel_key = {"ams": "ams", "blackrose": "br"}
        for src, mapping in cm.items():
            sel = set(sc.get(sel_key.get(src, src), []) or [])
            sf = scale_map.get(src, {})
            for raw_col, std_col in (mapping or {}).items():
                if sel and std_col not in sel:
                    continue
                if raw_col not in all_columns:
                    continue
                # 배율 표기('[*10]','[/100]')가 없는 변형 컬럼은 전처리가 자동 판정하므로 1.0 표시
                _w = _ams_default_weight(raw_col, std_col, sf) if src == "ams" else float(sf.get(std_col, 1.0))
                defaults[raw_col] = {
                    "include": True,
                    "rename": std_col if std_col != raw_col else "",
                    "weight": _w,
                    "bias": 0.0,
                }
    except Exception as e:
        print(f"  [scan] 기본 선택값 생성 실패: {e}")
    # 이름을 바꾸지 않는 선택 변수(Time, 원본명이 곧 표준명인 변수)도 기본 체크.
    # 위 루프는 이름 변경 매핑만 돌기 때문에 매핑에 없는 이런 변수가 빠져 있었음.
    try:
        for cols in (sc or {}).values():
            for std in (cols or []):
                if std in all_columns and std not in defaults:
                    defaults[std] = {"include": True, "rename": "", "weight": 1.0, "bias": 0.0}
    except Exception as e:
        print(f"  [scan] 선택 변수 기본 체크 실패: {e}")
    # 시간 컬럼은 병합 시 항상 유지되므로(_apply_variable_settings 보호 목록) 화면에서도 체크
    for t in ("Time", "Time_sec", "Time_min"):
        if t in all_columns:
            defaults[t] = {"include": True, "rename": "", "weight": 1.0, "bias": 0.0}
    # AMS 컬럼 표시 정보: 표준명(전처리 후 이름), 로거 배율 자동 적용 여부
    try:
        ams_map = (DEFAULT_CFG.get("column_mappings", {}) or {}).get("ams", {}) or {}
        sf_ams = DEFAULT_CFG.get("ams_scale_factors", {}) or {}
        for c, info in all_columns.items():
            if info.get("source") != "AMS":
                continue
            std = ams_map.get(c, c)
            if std != c:
                info["std"] = std
            if std in sf_ams:
                info["auto_scale"] = _ams_default_weight(c, std, sf_ams)
                info["auto_mode"] = "infer" if (std in _AMS_INFERABLE and "[" not in c) else "fixed"
    except Exception as e:
        print(f"  [scan] AMS 표시 정보 생성 실패: {e}")
    _READ_NOTES.clear()   # 스캔 중 읽기 메모는 Merge 로그와 무관
    print(f"  [scan] 케이스 {n_cases}개·파일 {len(scanned)}개 → 컬럼 {len(all_columns)}개, "
          f"config 기본 체크 {len(defaults)}개, 못 읽은 파일 {sum(1 for x in scanned if x.get('error'))}개")

    return {
        "total": len(all_columns),
        "columns": all_columns,
        "defaults": defaults,
        "files": scanned,
        "cases": n_cases,
        "cases_total": len(case_files),
    }


# ══════════════════════════════════════════════
#  Merge API (Stage 1 분리)
# ══════════════════════════════════════════════
@app.post("/api/merge")
async def start_merge(req: MergeRequest, background_tasks: BackgroundTasks):
    sid = req.session_id
    if sid not in sessions: raise HTTPException(404, "세션 없음")
    s = sessions[sid]
    if s["status"] == "processing": raise HTTPException(409, "처리 중")
    cfg = req.config or DEFAULT_CFG
    s.update(status="processing", progress=0, log=[], merge_results=[], error=None)
    background_tasks.add_task(_run_merge, sid, cfg, req.variable_settings)
    return {"status": "started", "mode": "merge"}


@_capture_log
def _run_merge(sid: str, cfg: dict, var_settings: dict | None = None):
    s = sessions[sid]
    category = Path(s["category_path"])
    case_files = s["case_files"]
    dt = cfg["processing"]["data_time"]

    try:
        _log(sid, "═══ Merge 시작 ═══")
        _log(sid, f"  환경: Python {sys.version.split()[0]} / pandas {pd.__version__}")
        from preprocessor import (
            preprocess_blackrose, preprocess_ams, preprocess_mx100,
            sync_and_merge, add_time_columns, _fuzzy_col_match,
        )
        from calculator import run_stage1
        from postprocessor import run_postprocessing
        from config import get_column_mapping, get_selected_columns
        from io_handler import rename_files_in_folder

        # 사용 소스 확인
        sources = cfg["processing"].get("data_sources", ["BR", "AMS", "MX100"])
        use_br = "BR" in sources
        use_ams = "AMS" in sources
        use_mx = "MX100" in sources
        use_ni = "NIDAQ" in sources
        _log(sid, f"  사용 소스: {', '.join(sources)} / 기준 시간축: {dt}")

        # ── 전처리에서 남길 컬럼 = Setting 기본 목록(selected_columns) ∪ 변수 표에서 체크한 변수 ──
        # 예전에는 BR/AMS 전처리가 Setting 기본 목록으로 먼저 걸러서, 표에서 새로 체크한 변수는
        # _apply_variable_settings 에 도달하기 전에 이미 빠져 있었다 (체크해도 merged 에 없음).
        sel_br = list(get_selected_columns(cfg, "br"))
        sel_ams = list(get_selected_columns(cfg, "ams"))
        extras = []   # Setting 기본 목록 밖에서 표로 추가한 변수 (원본 이름)
        if var_settings:
            _nm = lambda x: str(x).strip().replace(" ", "_").lower()
            cm_ams = get_column_mapping(cfg, "ams") or {}
            cm_br = get_column_mapping(cfg, "blackrose") or {}
            have = {"BR": {_nm(c) for c in sel_br}, "AMS": {_nm(c) for c in sel_ams}}
            for key, v in var_settings.items():
                if not (v or {}).get("include", True) or key in ("Time", "LogTime", "Time_sec", "Time_min"):
                    continue
                src = (v or {}).get("source")      # 화면이 보낸 소스 (없으면 양쪽 모두 시도)
                added = False
                for s_name, cmap, sel in (("AMS", cm_ams, sel_ams), ("BR", cm_br, sel_br)):
                    if src not in (None, s_name):
                        continue
                    std = cmap.get(key, key)        # 전처리에서 바뀐 뒤의 이름
                    if _nm(std) not in have[s_name]:
                        sel.append(std)
                        have[s_name].add(_nm(std))
                        added = True
                if added:
                    extras.append(key)
            included = [k for k, v in var_settings.items() if (v or {}).get("include", True)]
            _log(sid, f"  변수 설정: {len(included)}개 포함, {len(var_settings) - len(included)}개 제외")
            if extras:
                _log(sid, f"  ➕ Setting 기본 목록 밖 추가 변수 {len(extras)}개: "
                          f"{', '.join(extras[:12])}{' …' if len(extras) > 12 else ''}")

        # 1단계: 케이스별 파일 판별 + 병합 계획 (진행률 분모를 정확히 하기 위해 먼저 수행)
        plans = []
        for case_name in case_files:
            case_dir = category / case_name
            ren = rename_files_in_folder(str(case_dir)) or {}
            ov = {ren.get(k, k): v for k, v in (s.get("overrides", {}).get(case_name) or {}).items()}
            pr = {ren.get(k, k): ren.get(v, v) for k, v in (s.get("pairs", {}).get(case_name) or {}).items()}
            if ren:
                s.setdefault("overrides", {})[case_name] = ov
                s.setdefault("pairs", {})[case_name] = pr
                s.setdefault("renamed", {}).setdefault(case_name, {}).update(ren)
            cf = _case_entries(case_dir, cfg.get("file_rules"), sources, ov, pr)[0]
            s["case_files"][case_name] = cf
            ref_eff, ref_key = _ref_source(cf, dt, sources)
            mode = s.get("multi", {}).get(case_name, "auto")
            runs, pnotes = _plan_runs(case_dir, cf, ref_key, cfg.get("file_rules"), mode)
            n = len(runs)
            found = ", ".join(f"{k.upper()} {len(v)}개" for k, v in cf.items() if v) or "없음"
            if n == 0:
                _log(sid, f"  ⚠️ [{case_name}] 병합할 파일 없음 (판별된 파일: {found}) → 건너뜀")
            elif ref_eff != dt:
                _log(sid, f"  ⚠️ [{case_name}] 기준 시간축 '{dt}' 파일 없음 → '{ref_eff}' 기준으로 병합 ({found})")
            if n and (n > 1 or any(len(r.get(k, [])) > 1 for r in runs for k in ("br", "ams"))):
                mtxt = {"auto": "자동(시간으로 판단)", "concat": "하나로 이어 붙이기", "separate": "파일마다 따로"}.get(mode, mode)
                _log(sid, f"  🗂 [{case_name}] 병합 계획 {n}회 — 여러 파일: {mtxt}")
                def _desc(r, k):
                    st = (r.get("ams_streams") or []) if k == "ams" else []
                    if len(st) > 1:
                        return "AMS " + "  ‖  ".join(" + ".join(s) for s in st) + " (동시 기록 → 나란히 병합)"
                    return f"{k.upper()} " + " + ".join(r[k]) + (" (시간 순 이어 붙임)" if len(r[k]) > 1 else "")
                for i, r in enumerate(runs):
                    _log(sid, f"     {i+1}회: " + " / ".join(_desc(r, k) for k in ("br", "ams") if r.get(k)))
            for m in pnotes:
                _log(sid, f"     {m}")
            plans.append((case_name, case_dir, cf, ref_eff, runs))
        total = sum(len(p[4]) for p in plans)
        done, results = 0, []

        for case_name, case_dir, cf, ref_eff, runs in plans:
            n = len(runs)
            for i, run in enumerate(runs):
                t0 = time.perf_counter()
                _log(sid, f"[{case_name}] {i+1}/{n} 병합 중...")

                # 선택된 소스만 읽기 — BR·AMS 는 이 회차에 배정된 파일(여러 개면 시간 순 이어 붙임),
                # MX100/NIDAQ 는 전체 파일 리스트
                bp = [str(case_dir / f) for f in run.get("br", [])] if use_br else None
                # AMS: 묶음마다 (이어 붙일 조각들), 묶음끼리는 동시 기록 → 시간축에서 나란히
                ap = [[str(case_dir / f) for f in grp]
                      for grp in (run.get("ams_streams") or [run.get("ams", [])]) if grp] if use_ams else None
                mp_list = [str(case_dir / f) for f in cf.get("mx100",[])] if use_mx and cf.get("mx100") else None
                np_list = [str(case_dir / f) for f in cf.get("nidaq",[])] if use_ni and cf.get("nidaq") else None

                t1 = time.perf_counter()
                ams_parts, df_br, df_mx, ni_parts = _read(ap, bp, mp_list, dt, np_list=np_list, file_rules=cfg.get("file_rules"))
                df_ams = ams_parts[0] if ams_parts else pd.DataFrame()   # (있는지 확인용 — 묶음은 ams_parts)
                df_ni = ni_parts[0] if ni_parts else pd.DataFrame()   # NIDAQ 첫 묶음 (기준 시간축용)
                _READ_NOTES.clear()   # 내용은 print() 로 이미 로그에 전달됨
                n_ams = sum(len(p) for p in ams_parts)
                _log(sid, f"  📖 읽기: {time.perf_counter()-t1:.1f}s (AMS={n_ams}행"
                          + (f"/동시 기록 {len(ams_parts)}묶음" if len(ams_parts) > 1 else "")
                          + f" BR={len(df_br)}행 MX100={len(df_mx)}행/{len(cf.get('mx100',[]))}파일 NIDAQ={sum(len(p) for p in ni_parts)}행/{len(cf.get('nidaq',[]))}파일)")

                t1 = time.perf_counter()
                # 선택된 소스만 전처리
                if use_br and not df_br.empty:
                    df_br_main, df_br_add = preprocess_blackrose(
                        df_br, sel_br, get_column_mapping(cfg, "blackrose"))
                else:
                    df_br_main, df_br_add = pd.DataFrame(), pd.DataFrame()

                # AMS: 묶음마다 전처리 (동시에 기록한 다른 로거는 열 구성이 달라 각자 이름·배율 처리)
                ams_procs = []
                for k_, part in enumerate(ams_parts if use_ams else []):
                    if part.empty:
                        continue
                    if len(ams_parts) > 1:
                        print(f"  [ams] 묶음 {k_+1}/{len(ams_parts)} 전처리 ({len(part)}행 × {len(part.columns)}열)")
                    ams_procs.append(preprocess_ams(
                        part, get_column_mapping(cfg, "ams"),
                        cfg.get("ams_scale_factors", {}), cfg.get("subprocess_mapping", {}),
                        sel_ams))
                # 기준 시간축(AMS)으로 쓸 묶음 = 가장 긴 것
                df_ams_proc = max(ams_procs, key=len) if ams_procs else pd.DataFrame()
                # 전처리가 실제로 적용한 AMS 로거 배율 (표의 Weight 와 이중 적용 방지용)
                ams_applied = {}
                for p_ in ams_procs:
                    for k_, v_ in p_.attrs.get("ams_scale_applied", {}).items():
                        ams_applied.setdefault(k_, v_)
                present = {n for n, d in (("BR", df_br), ("AMS", df_ams), ("MX100", df_mx), ("NIDAQ", df_ni))
                           if not d.empty}
                # [ams] 진단은 preprocess_ams 의 print() 로 이미 로그에 전달됨

                if use_mx and not df_mx.empty:
                    df_mx_proc = preprocess_mx100(df_mx, cfg["mx100"]["useless_columns"])
                else:
                    df_mx_proc = pd.DataFrame()

                # NI DAQ(일반 계측 — .tsv/.dat 등) 전처리 (Time 컬럼 + 숫자 변환) — 파일 묶음별
                ni_procs = []
                for part in (ni_parts if use_ni else []):
                    p_ = _prep_generic_time(part.copy(), "NIDAQ")
                    for col in p_.columns.difference(["Time"]):
                        p_[col] = pd.to_numeric(p_[col], errors="coerce")
                    ni_procs.append(p_)
                df_ni_proc = ni_procs[0] if ni_procs else pd.DataFrame()
                _log(sid, f"  ⚙️ 전처리: {time.perf_counter()-t1:.1f}s")

                t1 = time.perf_counter()
                merged = sync_and_merge(
                    [df_br_main, df_br_add, df_mx_proc] + (ams_procs or [df_ams_proc]) + (ni_procs or [df_ni_proc]),
                    ref_eff, df_br_main, df_ams_proc, df_mx_proc, df_nidaq=df_ni_proc)
                _log(sid, f"  🔗 시간동기화: {time.perf_counter()-t1:.1f}s ({len(merged)}행)")

                # 디버그: 압력/온도 컬럼 존재 여부
                pcols = [c for c in merged.columns if 'P_Comp' in c or 'P_Cond' in c or 'P_Eva' in c]
                tcols = [c for c in merged.columns if c.startswith('T_Cond') or c.startswith('T_Eva')]
                _log(sid, f"  🔍 병합 후 압력컬럼: {pcols or '없음'}")
                _log(sid, f"  🔍 병합 후 냉매온도: {tcols or '없음'}")
                if 'P_Comp_Out' in merged.columns:
                    v = merged['P_Comp_Out'].dropna()
                    _log(sid, f"  🔍 P_Comp_Out 원본값: min={v.min():.3f}, max={v.max():.3f}, mean={v.mean():.3f}")
                if 'T_Cond_M1' in merged.columns:
                    v = merged['T_Cond_M1'].dropna()
                    _log(sid, f"  🔍 T_Cond_M1 값: min={v.min():.1f}, max={v.max():.1f}")

                t1 = time.perf_counter()
                merged = add_time_columns(merged)

                # ── 시작 트림 (merge 후 적용 — 모든 소스에 일관 적용) ──
                st_cfg = cfg.get("filtering", {}).get("start_trim", {})
                if st_cfg.get("enabled"):
                    st_col = st_cfg.get("column", "HP_CompTargetHz")
                    st_val = st_cfg.get("min_value", 10)
                    # 퍼지 매칭
                    st_matched = _fuzzy_col_match(merged.columns, [st_col])
                    st_actual = st_matched[0] if st_matched else None
                    if st_actual:
                        st_numeric = pd.to_numeric(merged[st_actual], errors="coerce")
                        st_mask = st_numeric >= st_val
                        if st_mask.any():
                            st_first = st_mask.idxmax()
                            before_len = len(merged)
                            merged = merged.loc[st_first:].reset_index(drop=True)
                            _log(sid, f"  ✂️ 시작 트림: {st_actual} >= {st_val} → {before_len-len(merged)}행 제거, {len(merged)}행 남음")
                            # Time_min/Time_sec 재계산 (0부터 시작)
                            merged = add_time_columns(merged)
                        else:
                            _log(sid, f"  ✂️ 시작 트림: {st_actual} >= {st_val} 없음 → 트림 안 함")
                    else:
                        _log(sid, f"  ✂️ 시작 트림: 컬럼 '{st_col}' 없음 → 트림 안 함")

                merged = run_stage1(merged, cfg)
                if 'P_Comp_Out' in merged.columns:
                    v = merged['P_Comp_Out'].dropna()
                    _log(sid, f"  🔍 Stage1 후 P_Comp_Out: min={v.min():.3f}, max={v.max():.3f}")
                _log(sid, f"  📐 Stage1: {time.perf_counter()-t1:.1f}s")

                t1 = time.perf_counter()
                merged = run_postprocessing(merged, cfg)
                _log(sid, f"  🔧 후처리: {time.perf_counter()-t1:.1f}s")

                # ── 변수 설정 적용 (W&B + 포함/제외 + 이름 변경) ──
                if var_settings:
                    rep = {}
                    merged = _apply_variable_settings(
                        merged, var_settings, cfg=cfg, ams_applied=ams_applied,
                        log=lambda m: _log(sid, m), report=rep)
                    # 표에서 추가한 변수가 실제로 들어갔는지 확인 (조용히 빠지는 일이 없도록)
                    if extras:
                        fin = rep.get("final", {})
                        ok = [fin[k] for k in extras if fin.get(k) in merged.columns]
                        miss = [k for k in extras if fin.get(k) not in merged.columns
                                and (var_settings[k] or {}).get("source") in present]
                        empty = [c for c in ok if merged[c].isna().all()]
                        _log(sid, f"  ➕ 추가 변수 {len(ok)}/{len(extras)}개 포함"
                                  + (f" ({', '.join(ok[:10])}{' …' if len(ok) > 10 else ''})" if ok else ""))
                        if miss:
                            _log(sid, f"  ⚠️ 체크했지만 이 케이스 파일에 없는 변수 {len(miss)}개: {', '.join(miss[:10])}")
                        if empty:
                            _log(sid, f"  ⚠️ 값이 전부 비어 있는 추가 변수: {', '.join(empty[:10])} "
                                      f"(문자 값은 숫자로 바뀌지 않아 빈 값이 됨)")

                # _merged.csv 저장 (한번 쓰고 복사)
                t1 = time.perf_counter()
                parts = case_name.split("_")[:9]
                prefix = "_".join(parts)
                out_name = f"{prefix}_{i+1}_merged.csv"
                result_path = _res_target(out_name)   # 같은 이름이 하위 폴더에 있으면 그 자리에 덮어씀

                if not _save_csv_verified(merged, result_path, sid, "결과 폴더"):
                    raise IOError(f"결과 저장 실패: {result_path}")

                # 원본(케이스) 폴더에도 복사 — 실패해도 진행하되 반드시 로그
                _copy_verified(result_path, case_dir / out_name, sid)

                # 출처 폴더 기록 (Calc 단계에서 원본 폴더를 찾는 데 사용)
                _record_source(out_name, case_dir)

                results.append(out_name)
                _log(sid, f"  💾 저장: {time.perf_counter()-t1:.1f}s")

                el = time.perf_counter() - t0
                _log(sid, f"  → {out_name} (총 {el:.1f}초, {len(merged)}행×{len(merged.columns)}열)")
                done += 1
                s["progress"] = int(done / max(total, 1) * 100)

        if not results:
            msg = ("병합된 파일이 없습니다 — 선택한 케이스 폴더에서 사용 소스("
                   + ", ".join(sources) + ") 파일을 찾지 못했습니다. "
                   "위 [classify] 로그와 설정 → 파일 식별 규칙(확장자/포함 패턴)을 확인하세요.")
            s.update(merge_results=[], status="error", error=msg, progress=100)
            _log(sid, f"\n❌ {msg}")
            return

        s.update(merge_results=results, status="done", progress=100)
        _log(sid, f"\n═══ Merge 완료: {len(results)}개 파일 ═══")

    except Exception as e:
        s.update(status="error", error=str(e))
        _log(sid, f"에러: {e}\n{traceback.format_exc()}")


def _apply_variable_settings(df: pd.DataFrame, var_settings: dict, cfg: dict | None = None,
                             ams_applied: dict | None = None, log=None,
                             report: dict | None = None) -> pd.DataFrame:
    """
    변수 표 설정 적용: Weight & Bias → 제외 컬럼 삭제 → 이름 변경.

    · 표의 키는 '변수 스캔'에 보이는 원본 이름. column_mappings 에 있는 AMS/BR 원본은
      전처리에서 이미 표준명으로 바뀌었으므로(예: DryFtemp → HP_F_Temp) 매핑을 따라가 찾는다.
      (예전엔 원본 이름으로만 찾아서 기본 AMS 변수의 Weight·체크 해제·이름 변경이 조용히 무시됨)
    · Weight 는 '원본 값에 곱하는 총 배율' (출력 = 원본 × Weight + Bias).
      전처리가 로거 배율(ams_scale_factors)을 이미 적용한 AMS 컬럼은
        - 표의 Weight 가 스캔 기본값(_ams_default_weight)과 같으면 → 자동 배율 그대로 (추가 곱셈 없음)
        - 다르면 → 이미 곱해진 배율을 나눠 결과가 원본 × Weight 가 되게 함 (이중 적용 방지)
    · 같은 컬럼을 가리키는 설정이 여럿이면(BR·AMS 에 같은 변수) 하나라도 포함이면 유지,
      W&B·이름 변경은 처음 한 번만.
    report: 주어지면 {"final": {설정 키: 최종 컬럼명}} 을 채움
    """
    cfg = cfg or {}
    say = log or print
    cm = cfg.get("column_mappings", {}) or {}
    ams_map = cm.get("ams", {}) or {}
    br_map = cm.get("blackrose", {}) or {}
    sf = cfg.get("ams_scale_factors", {}) or {}
    ams_applied = ams_applied or {}

    norm = lambda x: str(x).strip().replace(" ", "_").lower()
    col_map = {norm(c): c for c in df.columns}

    def _find(name):
        if name in df.columns:
            return name
        return col_map.get(norm(name))

    def std_of(key, src):
        if src == "AMS":
            return ams_map.get(key, key)
        if src == "BR":
            return br_map.get(key, key)
        if src in ("MX100", "NIDAQ"):
            return key
        return ams_map.get(key) or br_map.get(key) or key

    protected = {"Time", "Time_sec", "Time_min"}
    keep, drop, wb_done, renames, final = set(), [], set(), {}, {}
    wb_log, rn_log = [], []

    for key, s in var_settings.items():
        s = s or {}
        src = s.get("source")
        std = std_of(key, src)
        actual = _find(key) or _find(std)
        if not actual:
            continue
        if not s.get("include", True):
            drop.append(actual)
            continue
        keep.add(actual)
        final[key] = actual

        # ── Weight & Bias ──
        w = _num_or(s.get("weight"), 1.0)
        b = _num_or(s.get("bias"), 0.0)
        is_ams_key = src == "AMS" or (src is None and key in ams_map)
        f = 1.0
        if is_ams_key and std in sf:
            dw = _ams_default_weight(key, std, sf)
            f = _num_or(ams_applied.get(std), 1.0) or 1.0
            mult = 1.0 if abs(w - dw) <= 1e-9 * max(1.0, abs(dw)) else w / f
        else:
            mult = w
        if (mult != 1.0 or b != 0.0) and actual not in wb_done:
            if pd.api.types.is_numeric_dtype(df[actual]):
                df[actual] = df[actual] * mult + b
                wb_done.add(actual)
                desc = f"원본×{w:g}" + (f" (자동 ×{f:g} 대신)" if f != 1.0 and mult != 1.0 else "") \
                    if mult != 1.0 else ""
                desc += (" " if desc else "") + (f"{b:+g}" if b else "")
                wb_log.append(f"{actual} {desc.strip()}")
            else:
                say(f"  [변수] ⚠️ {actual}: 숫자 컬럼이 아니라 Weight/Bias 미적용")

        # ── 이름 변경 ──
        rn = str(s.get("rename") or "").strip()
        if rn and rn != actual and actual not in renames:
            if rn in df.columns or rn in renames.values():
                # 계산 단계가 같은 값으로 이미 만든 표준명 별칭(예: Heatpump_DuctInTemp → T_Air_Eva_In)이면
                # 목적이 이미 이뤄진 것 → 조용히 넘어감. 값이 다를 때만 경고.
                same = False
                if rn in df.columns:
                    a_ = pd.to_numeric(df[rn], errors="coerce")
                    b_ = pd.to_numeric(df[actual], errors="coerce")
                    same = bool(((a_ - b_).abs() <= 1e-9 * (1 + b_.abs())).where(b_.notna(), a_.isna()).all())
                if not same:
                    say(f"  [변수] ⚠️ {key}: 변경 이름 '{rn}' 이 값이 다른 기존 컬럼과 겹쳐 이름 변경 안 함")
            else:
                renames[actual] = rn
                rn_log.append(f"{actual}→{rn}")

    drop = [c for c in dict.fromkeys(drop) if c not in keep and c not in protected]
    if drop:
        df = df.drop(columns=drop, errors="ignore")
    if renames:
        df = df.rename(columns=renames)
    if report is not None:
        report["final"] = {k: renames.get(a, a) for k, a in final.items()}

    if wb_log:
        say(f"  [변수] Weight/Bias 적용 {len(wb_log)}개: {', '.join(wb_log[:12])}{' …' if len(wb_log) > 12 else ''}")
    if rn_log:
        say(f"  [변수] 이름 변경 {len(rn_log)}개: {', '.join(rn_log[:12])}{' …' if len(rn_log) > 12 else ''}")
    if drop:
        say(f"  [변수] 체크 해제로 제외 {len(drop)}개")
    return df


# ══════════════════════════════════════════════
#  로컬 Merged 파일 임포트 API
# ══════════════════════════════════════════════
class ImportRequest(BaseModel):
    file_paths: list[str]  # 로컬 CSV 파일 절대 경로 리스트
    add_merged_suffix: bool = True  # _merged 접미사 자동 추가 여부

@app.post("/api/import-merged")
def import_merged(req: ImportRequest):
    """로컬 경로의 CSV 파일을 results 폴더로 복사 + 출처 폴더 기록."""
    import shutil, json
    imported = []
    errors = []
    # 출처 메타데이터 로드/생성
    meta_path = RESULT_DIR / "_sources.json"
    sources = {}
    if meta_path.exists():
        try: sources = json.loads(meta_path.read_text("utf-8"))
        except: pass

    for fp in req.file_paths:
        p = Path(fp)
        if not p.exists():
            errors.append(f"파일 없음: {fp}")
            continue
        if not p.suffix.lower() == '.csv':
            errors.append(f"CSV 아님: {fp}")
            continue
        dest_name = p.name
        if req.add_merged_suffix and "_merged" not in dest_name and "_calc" not in dest_name:
            dest_name = p.stem + "_merged.csv"
        dest = _res_target(dest_name)
        try:
            shutil.copy2(str(p), str(dest))
            imported.append(dest_name)
            # 출처 폴더 기록
            sources[dest_name] = str(p.parent.resolve())
        except Exception as e:
            errors.append(f"{p.name}: {str(e)}")

    # 메타데이터 저장
    try: meta_path.write_text(json.dumps(sources, ensure_ascii=False, indent=2), "utf-8")
    except: pass

    return {"imported": imported, "errors": errors, "count": len(imported)}


@app.post("/api/browse-files")
def browse_files(req: BrowseRequest):
    """폴더 내 CSV 파일 목록 반환 (로컬 파일 선택용)."""
    p = Path(req.path)
    if not p.exists(): raise HTTPException(404, f"경로 없음: {req.path}")
    if not p.is_dir(): raise HTTPException(400, f"디렉토리 아님: {req.path}")
    files = []
    try:
        for f in sorted(p.iterdir()):
            if f.is_file() and f.suffix.lower() == '.csv':
                files.append({"name": f.name, "path": str(f.resolve()),
                              "size": f.stat().st_size,
                              "has_merged": "_merged" in f.name.lower(),
                              "has_calc": "_calc" in f.name.lower()})
    except PermissionError:
        raise HTTPException(403, f"접근 권한 없음: {req.path}")
    return {"path": str(p.resolve()), "files": files}


class ScanCasesRequest(BaseModel):
    category_path: str
    case_names: list[str]

@app.post("/api/scan-case-csvs")
def scan_case_csvs(req: ScanCasesRequest):
    """선택된 케이스 폴더들에서 CSV 파일을 일괄 수집."""
    category = Path(req.category_path)
    if not category.exists():
        raise HTTPException(404, f"경로 없음: {req.category_path}")
    result = {}  # {case_name: [{name, path, size, has_merged}]}
    total = 0
    for case in req.case_names:
        case_dir = category / case
        if not case_dir.is_dir():
            continue
        csvs = []
        for f in sorted(case_dir.iterdir()):
            if f.is_file() and f.suffix.lower() == '.csv':
                csvs.append({"name": f.name, "path": str(f.resolve()),
                             "size": f.stat().st_size,
                             "has_merged": "_merged" in f.name.lower(),
                             "has_calc": "_calc" in f.name.lower(),
                             "case": case})
                total += 1
        result[case] = csvs
    return {"category": str(category.resolve()), "cases": result, "total_files": total}


# ══════════════════════════════════════════════
#  Calculation API (Stage 2 분리)
# ══════════════════════════════════════════════
@app.post("/api/calculate")
async def start_calculation(req: CalcRequest, background_tasks: BackgroundTasks):
    sid = req.session_id
    # 세션 없으면 임시 세션 자동 생성
    if not sid or sid not in sessions:
        sid = str(uuid.uuid4())[:8]
        sessions[sid] = {
            "status": "ready", "progress": 0, "log": [], "error": None,
            "category_path": "", "case_files": {},
            "merge_results": [], "calc_results": [],
        }
    s = sessions[sid]
    if s["status"] == "processing": raise HTTPException(409, "처리 중")

    cfg = req.config or DEFAULT_CFG
    source = req.source_files or s.get("merge_results", [])
    if not source: raise HTTPException(400, "Merged 파일을 선택하세요.")

    s.update(status="processing", progress=0, log=[], calc_results=[], error=None, skipped_info={})
    background_tasks.add_task(_run_calc, sid, cfg, source, req.experimental, req.variable_mapping)
    return {"status": "started", "mode": "calculate", "source_count": len(source), "session_id": sid}


@_capture_log
def _run_calc(sid: str, cfg: dict, source_files: list[str],
              exp: dict | None, var_map: dict | None):
    s = sessions[sid]

    try:
        _log(sid, "═══ Calculation 시작 ═══")
        env = cfg["environment"]
        from properties import resolve_refrigerant
        cp_name = resolve_refrigerant(env["refrigerant"])
        display = f"{env['refrigerant']}" if env["refrigerant"] == cp_name else f"{env['refrigerant']} → {cp_name}"
        _log(sid, f"냉매: {display} ({env.get('backend','HEOS')})")

        from properties import get_props
        get_props(env["refrigerant"], env["patm"], env.get("backend", "HEOS"))
        from performance import run_stage2

        total = len(source_files)
        results = []

        for i, fn in enumerate(source_files):
            t0 = time.perf_counter()
            _log(sid, f"[{i+1}/{total}] {fn} 계산 중...")

            # merged CSV 읽기
            src_path = _res_find(fn)
            if src_path is None:
                _log(sid, f"  [경고] 파일 없음: {fn}, 스킵")
                continue
            df = pd.read_csv(src_path)
            _log(sid, f"  입력: {len(df)}행 × {len(df.columns)}열")

            # 변수 매핑 적용 (있으면)
            if var_map:
                rename_dict = {}
                for calc_var, merged_col in var_map.items():
                    if merged_col in df.columns and calc_var != merged_col:
                        rename_dict[merged_col] = calc_var
                if rename_dict:
                    df = df.rename(columns=rename_dict)
                    _log(sid, f"  변수 매핑: {len(rename_dict)}개 적용")

            # 압력 재계산 (Merge에서 잘못 계산됐을 수 있으므로)
            from calculator import _calc_pressures
            from properties import get_props as _gp
            _props = _gp(env["refrigerant"], env["patm"], env.get("backend", "HEOS"))
            df = _calc_pressures(df, _props, env["patm"])

            # Stage 2 실행 (파일별 실험값 지원)
            file_exp = exp.get(fn, exp) if isinstance(exp, dict) and fn in exp else exp
            try:
                from calculator import apply_std_aliases
                df = apply_std_aliases(df)   # 옛 merged 파일도 앱 표준명으로 계산
            except Exception as _e:
                print(f"  [alias] 적용 실패: {_e}")
            df_calc = run_stage2(df, cfg, file_exp, filename=fn)
            _log(sid, f"  계산 완료: {len(df_calc.columns)}열")
            rh_mode = "측정값 사용" if "RH_Eva_In" in df.columns else "에너지밸런스 역산 (수렴 반복)"
            _log(sid, f"  RH 모드: {rh_mode}")

            # 수렴 정보 로그
            skipped = df_calc.attrs.get("skipped_blocks", {})
            if skipped:
                _log(sid, f"  [경고] {len(skipped)}개 블록 스킵:")
                for blk, reason in skipped.items():
                    _log(sid, f"    - {blk}: {reason}")
                # 스킵 정보를 세션에 저장 (프론트엔드 팝업용)
                if "skipped_info" not in s:
                    s["skipped_info"] = {}
                s["skipped_info"][fn] = skipped

            # 수렴 반복 정보 로그
            conv = df_calc.attrs.get("converge_info")
            if conv:
                status = "수렴" if conv["converged"] else "미수렴"
                _log(sid, f"  수렴 반복: {conv['iterations']}회, 잔차={conv['residual']:.6f} ({status})")

            # IMC 보정 로그
            imc = df_calc.attrs.get("imc_correction")
            if imc:
                _log(sid, f"  🔧 IMC 보정: 스케일={imc.get('scale_factor',1):.4f}, "
                     f"실제={imc.get('total_real_kg',0):.3f}kg, "
                     f"계산={imc.get('total_calc_kg',0):.3f}kg")
            elif file_exp and file_exp.get("imc_kg"):
                reason = imc.get("reason", "") if imc else "응축수 계산 블록 스킵"
                _log(sid, f"  🔧 IMC 보정: 비활성 ({reason})")

            # _calc.csv 저장
            out_name = fn.replace("_merged.csv", "_calc.csv")
            calc_path = _res_target(out_name, near=src_path)   # merged 와 같은 폴더에
            if not _save_csv_verified(df_calc, calc_path, sid, "결과 폴더"):
                raise IOError(f"결과 저장 실패: {calc_path}")

            # 원본 폴더 결정: _sources.json 우선, 없으면 기존 휴리스틱
            case_dir = _lookup_source(fn)
            if case_dir is None:
                cat_path = s.get("category_path")
                if cat_path:
                    for case_name in s.get("case_files", {}):
                        if case_name.replace(" ", "_") in fn or fn.startswith(case_name[:10]):
                            case_dir = Path(cat_path) / case_name
                            break
            if case_dir is not None:
                _copy_verified(calc_path, Path(case_dir) / out_name, sid)
            else:
                _log(sid, f"  ⚠️ 원본 폴더를 찾지 못해 결과 폴더에만 저장: {out_name}")

            results.append(out_name)
            el = time.perf_counter() - t0
            _log(sid, f"  → {out_name} ({el:.1f}초)")

            # ── Formula 자동 적용 ──
            try:
                import json as _json
                formula_path = BASE_DIR / "config" / "formula_default.json"
                if formula_path.exists():
                    fdata = _json.loads(formula_path.read_text("utf-8"))
                    custom = fdata.get("custom", [])
                    all_formulas = [{"name": c["name"], "expr": c["expr"]}
                                    for c in custom
                                    if c.get("enabled", True) and c.get("name") and c.get("expr")]
                    _log(sid, f"  🧪 Formula: {len(all_formulas)}개 수식 로드")
                    n_applied, n_failed = 0, 0
                    for fm in all_formulas:
                        try:
                            # 위험 키워드 차단 (import문, exec/open 함수 호출, 던더)
                            if any(d in fm["expr"] for d in ["import ", "exec(", "__(", "open(", "os.", "sys."]):
                                _log(sid, f"    ⛔ {fm['name']}: 차단된 키워드")
                                n_failed += 1; continue
                            df_calc[fm["name"]] = df_calc.eval(fm["expr"])
                            _log(sid, f"    ✅ {fm['name']} = {fm['expr'][:60]}")
                            n_applied += 1
                        except Exception as fe:
                            _log(sid, f"    ❌ {fm['name']}: {fe}")
                            n_failed += 1
                    # 항상 재저장 (formula 컬럼 포함)
                    if _save_csv_verified(df_calc, calc_path, sid, "결과 폴더(Formula)"):
                        # 원본 폴더 사본도 formula 컬럼 포함본으로 갱신
                        if case_dir is not None:
                            _copy_verified(calc_path, Path(case_dir) / out_name, sid)
                    _log(sid, f"  🧪 결과: {n_applied}개 성공, {n_failed}개 실패, 총 {len(df_calc.columns)}열 저장")
            except Exception as fe:
                _log(sid, f"  [Formula 경고] {fe}")

            s["progress"] = int((i + 1) / total * 100)

        s.update(calc_results=results, status="done", progress=100)
        _log(sid, f"\n═══ Calculation 완료: {len(results)}개 파일 ═══")

    except Exception as e:
        s.update(status="error", error=str(e))
        _log(sid, f"에러: {e}\n{traceback.format_exc()}")


# ══════════════════════════════════════════════
#  컬럼 조회 API (변수 매핑용)
# ══════════════════════════════════════════════
@app.get("/api/columns/{fn}")
def get_columns(fn: str):
    """merged CSV의 컬럼 목록 반환 (Calc 변수 매핑 UI용)."""
    p = _res_file(fn)
    df = pd.read_csv(p, nrows=1)
    return {
        "filename": fn,
        "columns": df.columns.tolist(),
        "numeric_columns": df.select_dtypes(include=[np.number]).columns.tolist(),
    }


# ══════════════════════════════════════════════
#  Calc 필요 변수 정의 + 매핑 API
# ══════════════════════════════════════════════
CALC_VARIABLES = [
    # 압력 (필수)
    {"key": "P_Comp_Out",      "label": "압축기 토출 압력",      "unit": "barg", "category": "압력", "required": False,  "default_match": ["P_Comp_Out", "P_Cond_In"]},
    {"key": "P_Comp_In",       "label": "압축기 흡입 압력",      "unit": "barg", "category": "압력", "required": False,  "default_match": ["P_Comp_In", "P_Eva_Out"]},
    {"key": "P_Cond_Out",      "label": "응축기 출구 압력",      "unit": "barg", "category": "압력", "required": False, "default_match": ["P_Cond_Out"]},
    {"key": "P_Eva_In",        "label": "증발기 입구 압력",      "unit": "barg", "category": "압력", "required": False, "default_match": ["P_Eva_In"]},
    # 냉매 온도
    {"key": "T_Cond_In",       "label": "응축기 냉매 입구 온도",  "unit": "°C",  "category": "냉매 온도", "required": True,  "default_match": ["T_Cond_In", "T_Comp_Out", "Heatpump_CompTemp"]},
    {"key": "T_Cond_Out",      "label": "응축기 냉매 출구 온도",  "unit": "°C",  "category": "냉매 온도", "required": True,  "default_match": ["T_Cond_Out"]},
    {"key": "T_Cond_Mid",      "label": "응축기 냉매 중간 온도",  "unit": "°C",  "category": "냉매 온도", "required": False, "default_match": ["T_Cond_Mid", "T_Cond_M1"]},
    {"key": "T_Comp_Body",     "label": "압축기 토출 온도(바디)",   "unit": "°C",  "category": "냉매 온도", "required": False, "default_match": ["T_Comp_Body", "Heatpump_CompTemp", "T_Comp_Out"]},
    {"key": "T_Eva_Out",       "label": "증발기 냉매 출구 온도",  "unit": "°C",  "category": "냉매 온도", "required": True,  "default_match": ["T_Eva_Out", "Heatpump_EvaOutTemp"]},
    {"key": "T_Comp_In",       "label": "압축기 흡입 온도",      "unit": "°C",  "category": "냉매 온도", "required": False, "default_match": ["T_Comp_In", "Heatpump_EvaOutTemp", "T_Eva_Out"]},
    {"key": "T_Comp_Out",      "label": "압축기 토출 온도",      "unit": "°C",  "category": "냉매 온도", "required": False, "default_match": ["T_Comp_Out", "T_Cond_In"]},
    {"key": "T_Subcooler_Out", "label": "서브쿨러 출구 온도",    "unit": "°C",  "category": "냉매 온도", "required": False, "default_match": ["T_Subcooler_Out"]},
    # 공기 온도
    {"key": "T_Air_Eva_In",    "label": "증발기 공기 입구 온도",  "unit": "°C",  "category": "공기 온도", "required": True,  "default_match": ["T_Air_Eva_In", "Heatpump_DuctInTemp"]},
    {"key": "T_Air_Eva_Out",   "label": "증발기 공기 출구 온도",  "unit": "°C",  "category": "공기 온도", "required": True,  "default_match": ["T_Air_Eva_Out"]},
    {"key": "T_Air_Cond_Out",  "label": "응축기 공기 출구 온도",  "unit": "°C",  "category": "공기 온도", "required": True,  "default_match": ["T_Air_Cond_Out", "Heatpump_DuctOutTemp"]},
    {"key": "RH_Eva_In",       "label": "증발기 입구 RH (측정)", "unit": "-",   "category": "공기 온도", "required": False, "default_match": ["RH_Eva_In", "RH_Eva_In_measure"]},
    # 전력
    {"key": "Po_WD",           "label": "총 전력",             "unit": "W",   "category": "전력", "required": True,  "default_match": ["Po_WD"]},
    {"key": "Po_Comp",         "label": "압축기 전력",          "unit": "W",   "category": "전력", "required": True,  "default_match": ["Po_Comp"]},
    {"key": "Po_Fan",          "label": "팬 전력",             "unit": "W",   "category": "전력", "required": True,  "default_match": ["Po_Fan"]},
    # 제어
    {"key": "Ctrl_Comp_Hz",    "label": "압축기 주파수",        "unit": "Hz",  "category": "제어", "required": False, "default_match": ["Ctrl_Comp_Hz", "HP_CompCurrentHz"]},
    {"key": "Ctrl_DryMotion",  "label": "건조 모션 정보",       "unit": "-",   "category": "제어", "required": False, "default_match": ["Ctrl_DryMotion", "Heatpump_DryMotionInfo"]},
    # 시간
    {"key": "Time_min",        "label": "시간 (분)",           "unit": "min", "category": "시간", "required": True,  "default_match": ["Time_min"]},
    {"key": "Time_sec",        "label": "시간 (초)",           "unit": "sec", "category": "시간", "required": True,  "default_match": ["Time_sec"]},
]

@app.get("/api/calc-variables")
def get_calc_variables():
    """Calculation에 필요한 변수 목록 반환."""
    return {"variables": CALC_VARIABLES}


@app.post("/api/auto-map")
def auto_map(req: BrowseRequest):
    """
    merged CSV 파일명을 받아서 CALC_VARIABLES와 자동 매칭 결과를 반환.
    exact match → default_match 순서로 탐색.
    """
    fn = req.path
    p = _res_file(fn)
    df = pd.read_csv(p, nrows=1)
    merged_cols = df.columns.tolist()

    mapping = {}  # {calc_var: matched_merged_col or null}
    for v in CALC_VARIABLES:
        matched = None
        # 1. exact match
        if v["key"] in merged_cols:
            matched = v["key"]
        else:
            # 2. default_match 리스트에서 순서대로 탐색
            for dm in v.get("default_match", []):
                if dm in merged_cols:
                    matched = dm
                    break
        mapping[v["key"]] = matched

    unmatched = [v["key"] for v in CALC_VARIABLES if v["required"] and not mapping.get(v["key"])]

    return {
        "filename": fn,
        "merged_columns": merged_cols,
        "mapping": mapping,
        "unmatched_required": unmatched,
        "total_matched": sum(1 for v in mapping.values() if v),
        "total_required": sum(1 for v in CALC_VARIABLES if v["required"]),
    }


# ══════════════════════════════════════════════
#  공통 유틸
# ══════════════════════════════════════════════
_TIME_COL_NAMES = ("time", "datetime", "date_time", "date/time", "date time", "timestamp", "time stamp",
                   "logtime", "log time", "시간", "시각", "일시", "측정시간", "측정 시간", "측정시각")


def _pick_time_col(cols):
    """시간 열 이름 찾기 (대소문자·공백 무시, 'Time (s)'·'TIMESTAMP' 등 변형 포함). 없으면 None."""
    low = {}
    for c in cols:
        low.setdefault(str(c).strip().lower(), c)
    for nm in _TIME_COL_NAMES:
        if nm in low:
            return low[nm]
    for c in cols:
        if re.match(r"(date[ _/-]?)?time(stamp)?\b|시간|시각", str(c).strip().lower()):
            return c
    return None


def _prep_generic_time(df, label="NIDAQ"):
    """일반 계측 데이터(NIDAQ·.dat 등)의 Time 열 정리 — 다른 소스와 시계 시각으로 맞추기 위함.

    - 시간 열 이름 찾기 (대소문자·변형), 없으면 첫 열
    - '오전/오후 h:mm:ss' → 24시간제
    - 숫자 시간: 엑셀 날짜 일련값 / LabVIEW 시각(1904년 기준 초) / Unix 시각(초·ms) → 현지 시각,
      그 밖의 숫자는 경과 시간(열 이름에 ms·min·h 가 있으면 그 단위, 없으면 초)."""
    if df.empty:
        return df
    if "Time" not in df.columns:
        tc = _pick_time_col(df.columns)
        if tc is None:
            tc = df.columns[0]
            print(f"  [{label}] 시간 열 이름을 찾지 못해 첫 열 '{tc}' 을 시간으로 사용")
        elif str(tc) != "Time":
            print(f"  [{label}] 시간 열: '{tc}'")
        df = df.rename(columns={tc: "Time"})
    else:
        tc = "Time"
    t = df["Time"]
    if pd.api.types.is_datetime64_any_dtype(t):
        return df
    # 날짜와 시각이 다른 열(Date | Time)이면 합쳐서 사용 (자정 넘김·여러 날 측정 대비)
    first = t.dropna().astype(str).str.strip()
    first = first.iloc[0] if len(first) else ""
    if _RX_CLOCK_CELL.match(first):
        dcol = next((c for c in df.columns if str(c).strip().lower() in ("date", "날짜", "일자", "측정일")), None)
        if dcol is not None and df[dcol].notna().any():
            dfirst = str(df[dcol].dropna().iloc[0]).strip()
            if _RX_DATE_CELL.match(dfirst) and ":" not in dfirst:
                df["Time"] = df[dcol].astype(str).str.strip() + " " + t.astype(str).str.strip()
                df = df.drop(columns=[dcol])
                print(f"  [{label}] '{dcol}' 열과 시각 열을 합쳐 시간으로 사용")
                return df
    num = pd.to_numeric(t, errors="coerce")
    if len(num) and num.notna().mean() >= 0.9:
        import datetime as _dt
        med = float(num.median())
        step = float(num.diff().abs().median()) if len(num) > 1 else 1.0
        off = pd.Timedelta(_dt.datetime.now().astimezone().utcoffset() or _dt.timedelta(0))
        if 20000 < med < 80000 and step < 0.01:      # 하루 = 1 → 1초 간격이면 1.16e-5
            df["Time"] = pd.Timestamp("1899-12-30") + pd.to_timedelta(num, unit="D")
            kind = "엑셀 날짜 일련값"
        elif 2.5e9 < med < 5e9:
            df["Time"] = pd.Timestamp("1904-01-01") + pd.to_timedelta(num, unit="s") + off
            kind = "LabVIEW 시각(1904년 기준 초, UTC → 이 PC 시간대)"
        elif 1e9 < med < 2.5e9:
            df["Time"] = pd.Timestamp("1970-01-01") + pd.to_timedelta(num, unit="s") + off
            kind = "Unix 시각(초, UTC → 이 PC 시간대)"
        elif 1e12 < med < 2.5e12:
            df["Time"] = pd.Timestamp("1970-01-01") + pd.to_timedelta(num, unit="ms") + off
            kind = "Unix 시각(ms, UTC → 이 PC 시간대)"
        else:
            nm = str(tc).lower()
            unit, uname = "s", "초"
            if re.search(r"\bms\b|msec|milli", nm):
                unit, uname = "ms", "ms"
            elif re.search(r"\bmin\b|minute|\[min\]|\(min\)|분", nm):
                unit, uname = "min", "분"
            elif re.search(r"\bh\b|\bhr\b|hour|\[h\]|\(h\)", nm):
                unit, uname = "h", "시간"
            df["Time"] = pd.Timestamp("2000-01-01") + pd.to_timedelta(num, unit=unit)
            kind = (f"경과 시간({uname}) — 00:00:00 부터로 봄. 다른 소스가 시계 시각이면 서로 시간이 "
                    f"맞지 않으니 기준 시간축을 {label} 로 두거나 단독으로 병합")
        df["Time"] = df["Time"].dt.round("ms")   # 일련값·실수 오차(µs) 때문에 1초 격자와 어긋나지 않게
        print(f"  [{label}] Time 이 숫자 → {kind}")
        return df
    s = t.astype(str)
    if s.str.contains("오전|오후", regex=True).any():
        s2 = s.str.replace(r"(오전|오후)\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)",
                           lambda m: f"{m.group(2)} {'AM' if m.group(1) == '오전' else 'PM'}", regex=True)
        try:
            conv = pd.to_datetime(s2, errors="coerce", format="mixed")
        except (TypeError, ValueError):
            conv = pd.to_datetime(s2, errors="coerce")
        if conv.notna().mean() >= 0.8:
            df["Time"] = conv
            print(f"  [{label}] Time '오전/오후' 표기 → 24시간제로 변환")
    return df


def _read(ap, bp, mp_list, dt, np_list=None, file_rules=None):
    """
    소스별 파일 읽기. mp_list/np_list는 리스트(여러 파일 → 컬럼 병합).
    ap, bp는 경로 하나 또는 리스트 — 여러 개면 같은 시험이 나뉜 조각으로 보고 순서대로 이어 붙임
    (병합 계획이 시간 순으로 정렬해서 넘김).
    """
    df_a, df_b, df_m, df_n = pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    fr = file_rules or {}
    def _read_csv(path, src_key, default_skip=1):
        return read_source_csv(path, fr, src_key, default_skip)

    def _read_excel(path):
        skip = fr.get("mx100",{}).get("skip_rows",24)
        if _file_kind(path)[0] in ("text", "binary"):
            print(f"  [MX100] {os.path.basename(path)}: ⚠️ 엑셀 파일이 아님 — MX100 은 엑셀(.xls/.xlsx)만 읽음. "
                  f"텍스트 계측 파일이면 Setting > 파일 식별 규칙에서 이 확장자를 MX100 대신 NIDAQ 에 넣으세요")
            return pd.DataFrame()
        try:
            df = pd.read_excel(path, skiprows=skip, header=0)
            df.columns = [c.strip() for c in df.columns]
            return df
        except Exception as e1:
            try:
                df = pd.read_excel(path, skiprows=skip, header=[0,1])
                cols = [c[1] if "Unnamed" in str(c[0]) else c[0] for c in df.columns]
                df.columns = [c.strip() for c in cols]
                if "Date" in df.columns and "Time" in df.columns:
                    df["Time"] = df["Date"].astype(str)+" "+df["Time"].astype(str)
                    df.drop(columns=["Date"], inplace=True, errors="ignore")
                return df
            except Exception as e2:
                print(f"[MX100] 읽기 실패: {e1} / {e2}")
                return pd.DataFrame()

    def _merge_multi(dfs):
        """여러 df를 Time 기준 병합 (중복 컬럼은 첫 번째 우선)"""
        if not dfs: return pd.DataFrame()
        if len(dfs) == 1: return dfs[0]
        merged = dfs[0]
        for df in dfs[1:]:
            if df.empty: continue
            new_cols = [c for c in df.columns if c not in merged.columns or c == "Time"]
            if len(new_cols) <= 1: continue  # Time만 있으면 스킵
            if "Time" in merged.columns and "Time" in df.columns:
                merged = pd.merge(merged, df[new_cols], on="Time", how="outer", suffixes=('','_dup'))
                merged = merged.loc[:, ~merged.columns.str.endswith('_dup')]
            else:
                # Time 없으면 단순 concat
                for c in new_cols:
                    if c != "Time" and c not in merged.columns:
                        merged[c] = df[c].values[:len(merged)] if len(df) >= len(merged) else np.nan
        return merged

    def _read_parts(paths, src_key, label):
        paths = [p_ for p_ in ([paths] if isinstance(paths, str) else (paths or [])) if p_ and os.path.exists(p_)]
        parts = []
        for p_ in paths:
            try:
                d = _read_csv(p_, src_key, 1)
            except Exception as e:   # 한 파일을 못 읽어도 나머지로 계속
                print(f"  [{label}] ⚠️ {os.path.basename(p_)} 읽기 실패 → 이 파일은 빼고 병합: {type(e).__name__}: {str(e)[:150]}")
                continue
            if d is not None and not d.empty:
                parts.append((p_, d))
            else:
                print(f"  [{label}] ⚠️ {os.path.basename(p_)}: 읽은 데이터가 없어 빼고 병합")
        if not parts:
            return pd.DataFrame()
        if len(parts) == 1:
            return parts[0][1]
        if src_key == "ams" and all("Time" in d.columns for _, d in parts):
            # 조각마다 시각 표기가 다르면(.dat 는 날짜 포함, CSV 는 시:분:초 등) 하루 중 시각으로 맞춤
            kinds = {ams_dat.csv_time_seconds(d["Time"].head(50))[1] for _, d in parts}
            if len(kinds) > 1:
                for _, d in parts:
                    sec, _k, _b = ams_dat.csv_time_seconds(d["Time"])
                    tod = np.mod(sec, 86400.0)
                    d["Time"] = [f"{int(x // 3600):02d}:{int(x % 3600 // 60):02d}:{x % 60:06.3f}" if np.isfinite(x) else None
                                 for x in tod]
                print(f"  [{label}] 조각마다 시각 표기가 달라 하루 중 시각(HH:MM:SS.fff)으로 맞춤: {sorted(kinds)}")
        print(f"  [{label}] 파일 {len(parts)}개 이어 붙임: " + " + ".join(
            f"{os.path.basename(p_)}({len(d):,}행)" for p_, d in parts))
        return pd.concat([d for _, d in parts], ignore_index=True, sort=False)

    df_b = _read_parts(bp, "br", "BR")
    # AMS: ap = 묶음 목록 [[조각, …], …] (경로 하나·경로 목록도 받음 → 한 묶음)
    if isinstance(ap, str):
        ap = [[ap]]
    elif ap and not isinstance(ap[0], (list, tuple)):
        ap = [list(ap)]
    ams_parts = []
    for grp in (ap or []):
        d = _read_parts(list(grp), "ams", "AMS")
        if not d.empty:
            ams_parts.append(d)
    if len(ams_parts) > 1:
        print(f"  [AMS] 동시에 기록한 파일 묶음 {len(ams_parts)}개 → 각각 전처리해서 1초 시간축에서 나란히 병합: "
              + "  ‖  ".join(" + ".join(os.path.basename(x) for x in grp) for grp in ap if grp))

    # MX100: 여러 파일 → 컬럼 병합
    if mp_list:
        mx_dfs = []
        for mp in (mp_list if isinstance(mp_list, list) else [mp_list]):
            if mp and os.path.exists(mp):
                df = _read_excel(mp)
                if not df.empty:
                    mx_dfs.append(df)
                    print(f"  [MX100] {os.path.basename(mp)}: {len(df)}행 × {len(df.columns)}열")
        df_m = _merge_multi(mx_dfs)

    # NIDAQ(일반 계측 — .tsv/.dat 등): 여러 파일
    #   열 구성이 같은 파일(시간대별로 나뉜 로그) → 행으로 이어 붙임
    #   열 구성이 다른 파일(다른 장비) → 각각 따로 두고 시간 동기화에서 1초 격자에 맞춤
    #   (예전에는 Time 값이 정확히 같은 행끼리만 열 병합 → 장비마다 시각·형식이 다르면 어긋나고,
    #    열이 같은 두 번째 파일은 통째로 빠졌음)
    ni_parts = []
    if np_list:
        groups = {}
        for np_ in (np_list if isinstance(np_list, list) else [np_list]):
            if np_ and os.path.exists(np_):
                df = _read_csv(np_, "nidaq", 0)
                if not df.empty:
                    groups.setdefault(tuple(df.columns), []).append(df)
                    print(f"  [NIDAQ] {os.path.basename(np_)}: {len(df)}행 × {len(df.columns)}열")
        for g in groups.values():
            if len(g) > 1:
                print(f"  [NIDAQ] 열 구성이 같은 파일 {len(g)}개 → 이어 붙임")
            ni_parts.append(pd.concat(g, ignore_index=True) if len(g) > 1 else g[0])

    return ams_parts, df_b, df_m, ni_parts

def _log(sid, msg):
    if sid in sessions: sessions[sid]["log"].append(msg)


# ══════════════════════════════════════════════
#  상태 / 결과 API
# ══════════════════════════════════════════════
@app.get("/api/status/{sid}")
def get_status(sid: str):
    if sid not in sessions: raise HTTPException(404)
    s = sessions[sid]
    return {k: s.get(k) for k in ("status","progress","log","merge_results","calc_results","error","skipped_info")}

@app.get("/api/results")
def list_results():
    """results 아래 결과 CSV 목록 (하위 폴더 포함). folder: 하위 폴더 ('' = results 바로 아래)."""
    sources = _sources_load()
    files = sorted(_iter_results(), key=os.path.getmtime, reverse=True)
    seen = {}
    for f in files:
        k = f.name.lower() if os.name == "nt" else f.name
        seen[k] = seen.get(k, 0) + 1
    out = []
    for f in files:
        k = f.name.lower() if os.name == "nt" else f.name
        out.append({"name": f.name, "size": f.stat().st_size,
                    "type": "merged" if "_merged" in f.name else "formula" if "_formula" in f.name
                            else "calc" if "_calc" in f.name else "other",
                    "source_dir": sources.get(f.name, ""),
                    "folder": _res_folder_of(f),
                    "dup": seen[k] > 1})   # 같은 이름이 다른 폴더에도 있음 (탐색기로 복사한 경우 등)
    return out


# ── 결과 폴더 관리 (Visualization 폴더 = results 의 실제 하위 폴더) ──
class ResFolderReq(BaseModel):
    name: str
    new_name: Optional[str] = None
    parent: str = ""             # 만들기: 이 폴더 안에 / 옮기기: 이 폴더 안으로 ('' = results 바로 아래)


class ResMoveReq(BaseModel):
    names: list[str]
    folder: str = ""


class ResRenameReq(BaseModel):
    renames: list[dict]          # [{"old": "A_1_merged.csv", "new": "B_merged.csv"}, ...]
    also_source: bool = True     # 원본 케이스 폴더에 있는 사본도 같이 이름 변경


@app.get("/api/result-folders")
def list_result_folders():
    folders = []
    for d in RESULT_DIR.rglob("*"):
        try:
            rel = d.relative_to(RESULT_DIR)
            if d.is_dir() and not any(_hidden_dir(part) for part in rel.parts):
                folders.append(rel.as_posix())
        except Exception:
            continue
    return {"folders": sorted(folders, key=str.lower), "root": str(RESULT_DIR)}


def _check_folder_name(name):
    n = _check_name(name, "폴더 이름")
    if _hidden_dir(n):
        raise HTTPException(400, "'_' 나 '.' 으로 시작하는 폴더 이름은 프로그램용이라 쓸 수 없습니다")
    return n


@app.post("/api/result-folders")
def create_result_folder(req: ResFolderReq):
    """새 폴더 — parent 폴더 안에 만든다 (parent='' → results 바로 아래).
    이름에 '/' 를 넣으면 여러 단계를 한 번에 만든다 (예: '2026-11/건조A')."""
    base = _res_folder_path(req.parent)
    if not base.is_dir():
        raise HTTPException(404, f"상위 폴더 없음: {req.parent}")
    parts = [x for x in str(req.name or "").replace("\\", "/").split("/") if x.strip()]
    if not parts:
        raise HTTPException(400, "폴더 이름이 비어 있습니다")
    p = base.joinpath(*[_check_folder_name(x) for x in parts])
    if p.exists():
        raise HTTPException(409, f"이미 있는 폴더입니다: {_res_rel(p)}")
    try:
        p.mkdir(parents=True)
    except OSError as e:   # 경로가 너무 김(Windows 260자) · 권한 등
        raise HTTPException(400, f"폴더를 만들 수 없습니다 ({len(str(p))}자 경로): {e.strerror or e}")
    return {"folder": _res_rel(p), "path": str(p)}


@app.post("/api/result-folders/rename")
def rename_result_folder(req: ResFolderReq):
    src = _res_folder_path(req.name)
    if src == RESULT_DIR or not src.is_dir():
        raise HTTPException(404, f"폴더 없음: {req.name}")
    dst = src.parent / _check_folder_name(req.new_name)
    if dst.exists() and not _samefile(src, dst):
        raise HTTPException(409, f"같은 이름의 폴더가 이미 있습니다: {dst.name}")
    try:
        os.rename(src, dst)
    except PermissionError:
        raise HTTPException(409, f"폴더 '{src.name}': {_IN_USE_MSG}")
    return {"folder": dst.relative_to(RESULT_DIR).as_posix(), "path": str(dst)}


@app.post("/api/result-folders/delete")
def delete_result_folder(req: ResFolderReq):
    """폴더 삭제 — 안의 결과 파일과 하위 폴더는 지우지 않고 한 단계 위(상위 폴더)로 올린 뒤 폴더를 없앤다.
    (맨 위 폴더면 results 바로 아래로. 하위 폴더는 안의 구조 그대로 올라감)"""
    import shutil
    src = _res_folder_path(req.name)
    if src == RESULT_DIR or not src.is_dir():
        raise HTTPException(404, f"폴더 없음: {req.name}")
    parent = src.parent
    where = f"상위 폴더 '{_res_rel(parent)}'" if parent != RESULT_DIR else "results 바로 아래"
    items = sorted([q for q in src.iterdir()
                    if (q.is_file() and q.suffix.lower() == ".csv")
                    or (q.is_dir() and not _hidden_dir(q.name))],
                   key=lambda q: (q.is_dir(), q.name.lower()))
    clash = [q.name + ("(폴더)" if q.is_dir() else "") for q in items if (parent / q.name).exists()]
    if clash:
        raise HTTPException(409, f"{where}에 같은 이름이 이미 있어 옮길 수 없습니다: "
                                 + ", ".join(clash[:5]) + " — 이름을 바꾼 뒤 다시 삭제하세요")
    moved = []
    for q in items:
        try:
            shutil.move(str(q), str(parent / q.name))
            moved.append(q.name)
        except PermissionError:
            raise HTTPException(409, f"{q.name}: {_IN_USE_MSG} (이미 옮긴 항목 {len(moved)}개)")
    left = [x.relative_to(src).as_posix() for x in src.rglob("*") if x.is_file()]
    if not left:
        try:
            shutil.rmtree(src)
        except Exception as e:
            raise HTTPException(409, f"내용은 옮겼지만 폴더를 지우지 못했습니다: {e}")
    return {"moved": moved, "removed": not left, "left": left[:10], "to": _res_rel(parent)}


@app.post("/api/result-folders/move")
def move_result_folder(req: ResFolderReq):
    """폴더를 (안의 내용 그대로) 다른 폴더 안으로 옮김. parent='' → results 바로 아래."""
    src = _res_folder_path(req.name)
    if src == RESULT_DIR or not src.is_dir():
        raise HTTPException(404, f"폴더 없음: {req.name}")
    dst_parent = _res_folder_path(req.parent)
    if not dst_parent.is_dir():
        raise HTTPException(404, f"옮길 곳 폴더 없음: {req.parent}")
    ns = os.path.normcase(str(src.resolve()))
    nd = os.path.normcase(str(dst_parent.resolve()))
    if nd == ns or nd.startswith(ns + os.sep):
        raise HTTPException(400, "폴더를 자기 자신이나 자기 하위 폴더 안으로 옮길 수 없습니다")
    if _samefile(src.parent, dst_parent):
        return {"folder": _res_rel(src), "path": str(src), "moved": False}
    dst = dst_parent / src.name
    if dst.exists():
        raise HTTPException(409, f"옮길 곳에 같은 이름의 폴더(또는 파일)가 이미 있습니다: {src.name}")
    try:
        os.rename(src, dst)
    except PermissionError:
        raise HTTPException(409, f"폴더 '{src.name}': {_IN_USE_MSG}")
    except OSError as e:   # 경로가 너무 김(Windows 260자) 등
        raise HTTPException(400, f"폴더 '{src.name}' 을(를) 옮길 수 없습니다: {e.strerror or e}")
    return {"folder": _res_rel(dst), "path": str(dst), "moved": True}


@app.post("/api/result-files/move")
def move_result_files(req: ResMoveReq):
    """결과 파일을 하위 폴더로 이동 (folder='' → results 바로 아래)."""
    import shutil
    dst_dir = _res_folder_path(req.folder)
    dst_dir.mkdir(parents=True, exist_ok=True)
    moved, errors = [], []
    for name in req.names:
        src = _res_find(name)
        if src is None:
            errors.append(f"{name}: 파일 없음")
            continue
        dst = dst_dir / src.name
        if _samefile(src.parent, dst_dir):
            continue
        if dst.exists():
            errors.append(f"{name}: 대상 폴더에 같은 이름 파일이 있음")
            continue
        try:
            shutil.move(str(src), str(dst))
            moved.append(src.name)
        except PermissionError:
            errors.append(f"{name}: {_IN_USE_MSG}")
        except Exception as e:
            errors.append(f"{name}: {e}")
    return {"moved": moved, "errors": errors, "folder": _res_folder_of(dst_dir / "x")}


@app.post("/api/result-files/rename")
def rename_result_files(req: ResRenameReq):
    """결과 파일 이름 변경 (여러 개를 한 번에 — 케이스의 merged·calc·formula 를 같이).
    전부 검사한 뒤 바꾸고, 중간에 실패하면 이미 바꾼 것을 되돌린다.
    also_source: 원본 케이스 폴더(_sources.json)에 있는 같은 이름 사본도 이름 변경."""
    plan = []
    for r in req.renames:
        old = Path(str(r.get("old", ""))).name
        new = _check_name(r.get("new", ""), "파일 이름")
        if not new.lower().endswith(".csv"):
            raise HTTPException(400, f"파일 이름은 .csv 로 끝나야 합니다: {new}")
        src = _res_file(old)
        if src.name == new:
            continue
        plan.append((src.name, new, src))
    if not plan:
        return {"renamed": [], "copies": []}
    news = [n.lower() for _, n, _ in plan]
    if len(set(news)) != len(news):
        raise HTTPException(400, "새 이름이 서로 겹칩니다")
    for old, new, src in plan:
        hit = _res_find(new)
        if hit is not None and not _samefile(hit, src):
            folder = _res_folder_of(hit)
            raise HTTPException(409, f"같은 이름 파일이 이미 있습니다: {new}"
                                     + (f" (폴더 '{folder}')" if folder else ""))
    done = []
    try:
        for old, new, src in plan:
            dst = src.parent / new
            os.rename(src, dst)
            done.append((src, dst, old, new))
    except Exception as e:
        for a, b, _, _ in reversed(done):
            try:
                os.rename(b, a)
            except Exception:
                pass
        if isinstance(e, PermissionError):
            raise HTTPException(409, f"{old}: {_IN_USE_MSG}")
        raise HTTPException(500, f"{old}: 이름 변경 실패 — {e}")

    sources = _sources_load()
    copies = []
    for _, _, old, new in done:
        sd = sources.pop(old, None)
        if sd is None:
            continue
        sources[new] = sd
        if not req.also_source:
            continue
        o, n = Path(sd) / old, Path(sd) / new
        if not o.exists():
            copies.append({"file": old, "status": "없음", "dir": sd})
        elif n.exists() and not _samefile(o, n):
            copies.append({"file": old, "status": "같은 이름 있음", "dir": sd})
        else:
            try:
                os.rename(o, n)
                copies.append({"file": new, "status": "변경", "dir": sd})
            except PermissionError:
                copies.append({"file": old, "status": "사용 중", "dir": sd})
            except Exception as e:
                copies.append({"file": old, "status": f"실패: {e}", "dir": sd})
    _sources_save(sources)
    for c in copies:
        print(f"  [rename] 원본 사본 {c['file']}: {c['status']} ({c['dir']})")
    return {"renamed": [{"old": o, "new": n, "folder": _res_folder_of(d)} for _, d, o, n in done],
            "copies": copies}


@app.get("/api/results/{fn:path}")
def download(fn: str):
    from urllib.parse import unquote
    fn = unquote(fn)
    p = _res_file(fn)
    return FileResponse(p, media_type="text/csv", filename=p.name)


@app.delete("/api/results/{fn:path}")
def delete_result(fn: str):
    """결과 파일 삭제 + 출처 메타데이터 정리."""
    import json
    from urllib.parse import unquote
    fn = unquote(fn)
    p = _res_file(fn)
    try:
        p.unlink()
    except PermissionError:
        raise HTTPException(409, f"{p.name}: {_IN_USE_MSG}")
    fn = p.name
    # 메타데이터에서도 제거
    meta_path = RESULT_DIR / "_sources.json"
    if meta_path.exists():
        try:
            sources = json.loads(meta_path.read_text("utf-8"))
            sources.pop(fn, None)
            meta_path.write_text(json.dumps(sources, ensure_ascii=False, indent=2), "utf-8")
        except: pass
    return {"deleted": fn}

@app.get("/api/preview/{fn}")
def preview(fn: str, max_rows: int = 3000):
    p = _res_file(fn)
    df = pd.read_csv(p, nrows=max_rows)
    if "Time" in df.columns: df["Time"] = df["Time"].astype(str)
    df = df.where(pd.notnull(df), None)
    return {
        "columns": df.columns.tolist(),
        "data": df.values.tolist(),
        "total_rows": sum(1 for _ in open(p)) - 1,
        "numeric_columns": df.select_dtypes(include=[np.number]).columns.tolist(),
    }

@app.delete("/api/session/{sid}")
def delete_session(sid: str):
    sessions.pop(sid, None)
    return {"status": "deleted"}


# ══════════════════════════════════════════════
#  프론트엔드 서빙
# ══════════════════════════════════════════════
# (STATIC은 상단에서 정의됨)
# ══════════════════════════════════════════════
#  Viewer 데이터 API
# ══════════════════════════════════════════════
@app.get("/api/viewer-data/{fn:path}")
def viewer_data(fn: str, max_rows: int = 7200):
    """calc/merged CSV를 컬럼별 배열 JSON으로 반환 (Viewer용)."""
    from urllib.parse import unquote
    fn = unquote(fn)
    p = _res_find(fn)
    if p is None:
        print(f"  [viewer-data] 404: {fn}, RESULT_DIR={RESULT_DIR}")
        raise HTTPException(404, f"파일 없음: {fn}")
    try:
        df = pd.read_csv(p)
        # 이 수정 이전에 만든 merged/calc 파일도 그래프 카테고리 이름으로 보이도록 별칭 보강
        try:
            from calculator import apply_std_aliases
            df = apply_std_aliases(df, log=False)
        except Exception:
            pass
    except Exception as e:
        raise HTTPException(400, f"CSV 읽기 실패: {str(e)}")
    if len(df) > max_rows:
        step = max(1, len(df) // max_rows)
        df = df.iloc[::step].reset_index(drop=True)
    
    # 숫자 변환 시도 (문자열로 읽힌 컬럼 복구)
    for c in df.columns:
        if not pd.api.types.is_numeric_dtype(df[c]) and c != "Time":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    
    # NaN → None 처리 + 숫자 컬럼만
    cols = {}
    for c in df.columns:
        if pd.api.types.is_numeric_dtype(df[c]):
            s = df[c].round(4)
            cols[c] = [None if pd.isna(v) else float(v) for v in s]
    return {"filename": fn, "rows": len(df), "columns": list(cols.keys()), "data": cols}


@app.get("/api/ref-saturation/{name}")
def ref_saturation(name: str):
    """냉매 포화선 데이터 반환 (P-h 선도 배경용)."""
    try:
        from properties import resolve_refrigerant
        cp_name = resolve_refrigerant(name)
        from CoolProp.CoolProp import PropsSI
        Tc = PropsSI("Tcrit", cp_name) - 273.15
        Pc = PropsSI("Pcrit", cp_name) / 1e5
        hc = PropsSI("H", "T", Tc + 273.15, "P", Pc * 1e5, cp_name) / 1000

        Tmin = max(-50, PropsSI("Tmin", cp_name) - 273.15 + 1)
        temps = np.linspace(Tmin, Tc - 0.5, 200)
        hl, hv, P = [], [], []
        for t in temps:
            try:
                tk = t + 273.15
                p = PropsSI("P", "T", tk, "Q", 0, cp_name) / 1e5
                h_l = PropsSI("H", "T", tk, "Q", 0, cp_name) / 1000
                h_v = PropsSI("H", "T", tk, "Q", 1, cp_name) / 1000
                P.append(round(p, 4)); hl.append(round(h_l, 2)); hv.append(round(h_v, 2))
            except:
                pass
        return {"name": name, "coolprop_name": cp_name,
                "critical": {"T": round(Tc, 2), "P": round(Pc, 3), "h": round(hc, 2)},
                "sat": {"P": P, "hl": hl, "hv": hv}}
    except Exception as e:
        raise HTTPException(400, f"냉매 포화선 생성 실패: {str(e)}")


@app.post("/api/compute-enthalpy")
def compute_enthalpy(req: dict):
    """
    T + P 배열로부터 냉매 엔탈피 일괄 계산 (P-h 선도용).
    merged CSV에 h 컬럼이 없을 때 프론트엔드에서 호출.
    입력: {T_Comp_In:[], T_Comp_Out:[], T_Cond_Out:[], P_Comp_In:[], P_Comp_Out:[], refrigerant, patm}
    출력: {h_Comp_In:[], h_Comp_Out:[], h_Cond_Out:[]}
    """
    try:
        from properties import resolve_refrigerant
        from CoolProp.CoolProp import PropsSI

        ref = resolve_refrigerant(req.get("refrigerant", "R290"))
        patm = req.get("patm", 101.325)  # kPa

        def calc_h(T_arr, P_arr_barg):
            """T[°C] + P[barg] → h[kJ/kg] 배열."""
            if not T_arr or not P_arr_barg:
                return None
            result = []
            for t, p in zip(T_arr, P_arr_barg):
                if t is None or p is None:
                    result.append(None)
                    continue
                try:
                    p_pa = (p + patm / 100) * 1e5  # barg → Pa
                    h = PropsSI("H", "T", t + 273.15, "P", p_pa, ref) / 1000  # J/kg → kJ/kg
                    result.append(round(h, 2))
                except:
                    result.append(None)
            return result

        out = {}
        T_ci = req.get("T_Comp_In")
        T_co = req.get("T_Comp_Out")
        T_cd = req.get("T_Cond_Out")
        P_in = req.get("P_Comp_In")
        P_out = req.get("P_Comp_Out")

        if T_ci and P_in:
            out["h_Comp_In"] = calc_h(T_ci, P_in)
        if T_co and P_out:
            out["h_Comp_Out"] = calc_h(T_co, P_out)
        if T_cd and P_out:
            out["h_Cond_Out"] = calc_h(T_cd, P_out)

        return out
    except Exception as e:
        raise HTTPException(400, f"엔탈피 계산 실패: {str(e)}")


# ══════════════════════════════════════════════
#  커스텀 수식 엔진
# ══════════════════════════════════════════════
@app.post("/api/eval-formulas")
def eval_formulas(req: dict):
    """
    CSV 파일에 수식을 적용하여 새 컬럼 추가.
    입력: {filename: str, formulas: [{name, expr, enabled}]}
    pandas df.eval() 사용 — 기존 컬럼 참조 가능.
    """
    fn = req.get("filename")
    formulas = req.get("formulas", [])
    if not fn:
        raise HTTPException(400, "filename 필요")

    p = _res_file(fn)

    try:
        df = pd.read_csv(p)
    except Exception as e:
        raise HTTPException(400, f"CSV 읽기 실패: {str(e)}")

    results = []
    errors = []
    for f in formulas:
        name = f.get("name", "").strip()
        expr = f.get("expr", "").strip()
        if not name or not expr or not f.get("enabled", True):
            continue
        try:
            # 안전성: 위험 키워드 차단
            dangerous = ["import ", "exec(", "__(", "open(", "os.", "sys.", "subprocess"]
            if any(d in expr.lower() for d in dangerous):
                errors.append({"name": name, "error": "허용되지 않는 키워드"})
                continue
            df[name] = df.eval(expr)
            results.append({"name": name, "expr": expr, "rows": int(df[name].notna().sum())})
        except Exception as e:
            errors.append({"name": name, "error": str(e)})

    # 결과 저장
    out_name = Path(fn).name.replace(".csv", "_formula.csv")
    df.to_csv(_res_target(out_name, near=p), index=False)

    return {
        "output": out_name,
        "applied": results,
        "errors": errors,
        "total_columns": len(df.columns),
        "total_rows": len(df),
    }


@app.post("/api/preview-formula")
def preview_formula(req: dict):
    """수식 1개를 미리보기 (처음 10행만)."""
    fn = req.get("filename")
    expr = req.get("expr", "").strip()
    if not fn or not expr:
        raise HTTPException(400, "filename, expr 필요")

    p = _res_file(fn)

    try:
        df = pd.read_csv(p, nrows=100)
        dangerous = ["import ", "exec(", "__(", "open(", "os.", "sys.", "subprocess"]
        if any(d in expr.lower() for d in dangerous):
            raise HTTPException(400, "허용되지 않는 키워드")
        result = df.eval(expr)
        sample = [None if pd.isna(v) else round(float(v), 4) for v in result.head(10)]
        return {"preview": sample, "dtype": str(result.dtype)}
    except Exception as e:
        raise HTTPException(400, f"수식 오류: {str(e)}")


@app.get("/api/formula-columns/{fn}")
def formula_columns(fn: str):
    """수식에서 참조할 수 있는 컬럼 목록."""
    p = _res_file(fn)
    df = pd.read_csv(p, nrows=1)
    return {"columns": df.columns.tolist(), "numeric": df.select_dtypes(include=[np.number]).columns.tolist()}


@app.get("/api/version")
def get_version():
    """현재 버전 정보 — 최신 파일 수정 날짜 기반."""
    import subprocess
    # 1순위: git 커밋 날짜
    try:
        date_str = subprocess.check_output(
            ["git", "log", "-1", "--format=%ci"],
            cwd=str(BASE_DIR), stderr=subprocess.DEVNULL
        ).decode().strip()[:10]  # "2026-03-23"
        commit = subprocess.check_output(
            ["git", "log", "-1", "--format=%h"],
            cwd=str(BASE_DIR), stderr=subprocess.DEVNULL
        ).decode().strip()
        return {"date": date_str, "commit": commit, "base_dir": str(BASE_DIR)}
    except:
        pass
    # 2순위: server.py 수정 날짜
    try:
        import datetime
        mtime = os.path.getmtime(BASE_DIR / "server.py")
        date_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        return {"date": date_str, "commit": "", "base_dir": str(BASE_DIR)}
    except:
        return {"date": "unknown", "commit": "", "base_dir": str(BASE_DIR)}


@app.post("/api/update")
def self_update():
    """git으로 최신 코드 받기 + 서버 재시작. Setting 값은 유지됨.

    저장소가 비공개(HPWD-DataManager)로 통일되어 익명 ZIP 다운로드가 불가하므로
    git fetch + reset --hard origin/main 방식을 사용한다.
    (results/, config/saves/ 등 untracked 파일은 reset --hard로 삭제되지 않음)
    """
    import subprocess
    import yaml as _yaml

    git_dir = BASE_DIR / ".git"
    if not git_dir.exists():
        return {"status": "error",
                "message": "git 저장소가 아닙니다. git clone 으로 설치했는지 확인하세요.\n"
                           "  git clone https://github.com/kimjy2576/HPWD-DataManager.git"}

    def _git(*args, timeout=60):
        return subprocess.run(["git", *args], cwd=str(BASE_DIR),
                              capture_output=True, text=True, timeout=timeout)

    try:
        # 0. 현재 설정 백업 (머지용)
        cfg_path = BASE_DIR / "config" / "default_config.yaml"
        user_cfg = None
        if cfg_path.exists():
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    user_cfg = _yaml.safe_load(f)
            except Exception:
                pass

        before = _git("rev-parse", "--short", "HEAD").stdout.strip()

        # 1. 원격 최신 가져오기
        r = _git("fetch", "origin", "main", timeout=90)
        if r.returncode != 0:
            return {"status": "error",
                    "message": f"git fetch 실패 (인증 문제일 수 있음):\n{r.stderr.strip()[:400]}"}

        # 2. 로컬을 원격 상태로 강제 동기화 (tracked 파일만 덮어씀)
        r = _git("reset", "--hard", "origin/main")
        if r.returncode != 0:
            return {"status": "error", "message": f"git reset 실패:\n{r.stderr.strip()[:400]}"}

        after = _git("rev-parse", "--short", "HEAD").stdout.strip()

        # 3. 설정 머지 (유저 값 우선, 새 키만 default에서)
        if user_cfg and cfg_path.exists():
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    new_cfg = _yaml.safe_load(f)
                merged_cfg = _deep_merge_config(new_cfg, user_cfg)
                with open(cfg_path, "w", encoding="utf-8") as f:
                    _yaml.safe_dump(merged_cfg, f, allow_unicode=True,
                                    sort_keys=False, default_flow_style=False)
                print("[update] Setting 값 유지 (유저 설정 머지 완료)")
            except Exception as e:
                print(f"[update] 설정 머지 실패: {e} (새 default 사용)")

        if before == after:
            return {"status": "uptodate", "commit": after,
                    "message": f"이미 최신입니다 ({after}). 재시작하지 않음."}

        # 4. 서버 재시작 (백그라운드)
        import threading
        def restart():
            import time; time.sleep(1)
            os._exit(0)  # run.py의 루프가 uvicorn 재시작
        threading.Thread(target=restart, daemon=True).start()

        return {"status": "updated", "before": before, "after": after,
                "message": f"업데이트 완료 ({before} → {after}). 서버 재시작 중... Setting 값 유지됨."}
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "git 명령 시간 초과 (네트워크 확인)"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _deep_merge_config(new_cfg, user_cfg):
    """
    유저 설정을 새 default에 머지.
    - 유저에게 있는 키는 유저 값 사용
    - 유저에게 없는 새 키는 default 값 사용 (신기능 자동 반영)
    - dict는 재귀 머지, list/scalar는 유저 값으로 교체
    """
    if not isinstance(new_cfg, dict) or not isinstance(user_cfg, dict):
        return user_cfg if user_cfg is not None else new_cfg
    result = {}
    # 새 default의 모든 키를 순회 (순서 유지)
    for key, new_val in new_cfg.items():
        if key in user_cfg:
            user_val = user_cfg[key]
            if isinstance(new_val, dict) and isinstance(user_val, dict):
                result[key] = _deep_merge_config(new_val, user_val)
            else:
                result[key] = user_val  # 유저 값 우선
        else:
            result[key] = new_val  # 새로 추가된 키는 default 사용
    # 유저에게만 있는 키도 유지 (유저가 수동 추가한 경우)
    for key, user_val in user_cfg.items():
        if key not in result:
            result[key] = user_val
    return result


@app.get("/api/paths")
def paths_info():
    """실제 저장 경로와 파일 현황 — '파일이 어디 있냐' 진단용."""
    import json
    files = []
    try:
        for f in sorted(_iter_results(), key=os.path.getmtime, reverse=True)[:50]:
            files.append({"name": f.name, "folder": _res_folder_of(f), "size": f.stat().st_size,
                          "mtime": f.stat().st_mtime})
    except Exception:
        pass
    sources = {}
    meta = RESULT_DIR / "_sources.json"
    if meta.exists():
        try:
            sources = json.loads(meta.read_text("utf-8"))
        except Exception:
            pass
    return {
        "base_dir": str(BASE_DIR),
        "result_dir": str(RESULT_DIR),
        "result_dir_exists": RESULT_DIR.exists(),
        "cwd": os.getcwd(),
        "file_count": len(files),
        "files": files,
        "sources": sources,
    }


@app.get("/api/build")
def build_info():
    """실행 중인 index.html의 수정 시각/해시 — 브라우저 캐시 확인용."""
    import hashlib, datetime as _dt
    f = STATIC / "index.html"
    if not f.exists():
        return {"exists": False}
    raw = f.read_bytes()
    return {
        "exists": True,
        "mtime": _dt.datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds"),
        "size": len(raw),
        "sha1": hashlib.sha1(raw).hexdigest()[:12],
    }


@app.get("/")
def index():
    f = STATIC / "index.html"
    if not f.exists():
        return HTMLResponse("<h1>HPWD Data Manager</h1>")
    resp = HTMLResponse(f.read_text("utf-8"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
