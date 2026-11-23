# -*- coding: utf-8 -*-
"""
HPWD Data Manager — 데이터 진단 도구 (AMS .csv / 계측 .dat·.txt·.tsv)

실제 파일을 앱(Merge)과 '같은 코드 경로'로 읽어서, 원본 값 없이 요약 리포트만 만든다.
리포트 텍스트만 공유하면 되므로 데이터가 PC 밖으로 나가지 않는다.
(포함되는 값: 개수, 칸 수, 시간 길이, 주요 변수의 최소/중앙/최대 같은 집계값뿐.
 .dat 등 계측 파일은 형식 확인용으로 첫 줄·열 이름도 표시 — --mask 로 숨김)

사용:
  python tools/diagnose.py <AMS.csv | 계측.dat | 케이스폴더 | 상위폴더> [...]
  AMS 바이너리 .dat 는 같은 폴더에 같은 이름의 CSV(AMS 프로그램에서 내보낸 같은 측정)가 있으면
  둘을 비교해 형식을 학습하고 config/ams_dat_formats/ 에 저장한다 → 이후 앱이 .dat 를 바로 읽음.
  python tools/diagnose.py <경로> --mask      파일/폴더 이름 가리기
  또는 diagnose.bat 위에 파일/폴더를 끌어다 놓기

출력: 콘솔 + results/_diagnose/report_YYYYmmdd_HHMMSS.txt
"""
from __future__ import annotations

import contextlib
import csv
import datetime
import io
import os
import re
import subprocess
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass

SENTINELS = (65535, 65534, 32767, -32768)
ENCODINGS = ("utf-8-sig", "utf-8", "cp949", "latin1")
RX_DATE = re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}")
RX_HMS = re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}")
RX_MS = re.compile(r"^\s*\d+:\d+(\.\d+)?\s*$")

# 표준명 → 설명 (주요 변수 통계 대상)
KEY_VARS = {
    "HP_CompCurrentHz": "압축기 현재 Hz",
    "HP_CompTargetHz": "압축기 목표 Hz",
    "HP_SuperHeatDegree": "과열도 [K]",
    "HP_EEV_Position": "EEV 개도",
    "HP_FanSpeed": "팬 속도",
    "Heatpump_DuctInTemp": "덕트 입구 온도",
    "Heatpump_DuctOutTemp": "덕트 출구 온도",
    "Heatpump_EvaInTemp": "증발기 입구 온도",
    "Heatpump_EvaOutTemp": "증발기 출구 온도",
    "Heatpump_CompTemp": "압축기 토출(바디) 온도",
    "Po_WD": "소비 전력",
    "E_WD": "적산 전력량",
}

# 다운스트림 계산이 요구하는 변수 (하나라도 있으면 충족되는 대체명 그룹)
REQUIRED = [
    ("Stage1 fTemp 계산", [("Heatpump_DuctOutTemp", "T_Air_Cond_Out"),
                           ("Heatpump_DuctInTemp", "T_Air_Eva_In"),
                           ("Heatpump_EvaInTemp", "T_Eva_In")]),
    ("건조 구간 필터", [("HP_CompTargetHz",), ("HP_FanSpeed",), ("HP_EEV_Position",)]),
    ("전력/에너지", [("Po_WD",), ("E_WD",)]),
    ("행정 구분", [("SubProcess",)]),
]


# ──────────────────────────────────────────────────────────────
class Report:
    ICON = {"err": "❌", "warn": "⚠️", "ok": "✅", "info": "ℹ️"}

    def __init__(self):
        self.lines: list[str] = []
        self.findings: list[tuple[str, str, str]] = []
        self.ctx = ""

    def add(self, s: str = ""):
        print(s, flush=True)
        self.lines.append(s)

    def h(self, title: str):
        self.add("")
        self.add("━" * 64)
        self.add(f"■ {title}")
        self.add("━" * 64)

    def sub(self, title: str):
        self.add("")
        self.add(f"[{title}]")

    def flag(self, level: str, msg: str):
        self.add(f"  {self.ICON[level]} {msg}")
        if level in ("err", "warn"):
            self.findings.append((level, self.ctx, msg))


WARN_LOG: list = []   # 마지막 quiet() 호출 중 발생한 경고 (분류명 문자열)


def quiet(fn, *a, **kw):
    """함수 실행 중 print 출력·경고를 캡처해서 (결과, 출력줄 목록) 반환."""
    import warnings
    buf = io.StringIO()
    WARN_LOG.clear()
    with contextlib.redirect_stdout(buf), warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        res = fn(*a, **kw)
    WARN_LOG.extend(type(x.message).__name__ for x in w)
    return res, [ln for ln in buf.getvalue().splitlines() if ln.strip()]


def fmt_num(v) -> str:
    try:
        v = float(v)
    except Exception:
        return "-"
    if v != v:
        return "-"
    return f"{v:.0f}" if abs(v) >= 100 else f"{v:.3g}"


def time_kind(v) -> str:
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return "빈 값"
    if RX_DATE.search(s):
        return "날짜 포함"
    if RX_HMS.match(s):
        return "HH:MM:SS"
    if RX_MS.match(s):
        return "MM:SS.f"
    try:
        float(s)
        return "숫자"
    except ValueError:
        return "기타"


def detect_encoding(path: Path, max_lines: int | None = None) -> str:
    for enc in ENCODINGS:
        try:
            with open(path, encoding=enc) as f:
                for i, _ in enumerate(f):
                    if max_lines and i >= max_lines:
                        break
            return enc
        except (UnicodeDecodeError, UnicodeError):
            continue
        except Exception:
            continue
    return "latin1"


def scan_widths(path: Path, enc: str, sep: str, header_row: int):
    """전체 행을 csv 규칙으로 스캔 — 칸 수 분포, 빈 행, 중간 헤더 반복."""
    widths: Counter = Counter()
    first_lines: dict[int, list[int]] = {}
    header, n, blank, rep_header, last_w = None, 0, 0, 0, None
    with open(path, encoding=enc, errors="replace", newline="") as f:
        rd = csv.reader(f, delimiter=sep)
        for i, row in enumerate(rd):
            if i < header_row:
                continue
            if i == header_row:
                header = row
                continue
            if not row or all(not c.strip() for c in row):
                blank += 1
                continue
            n += 1
            w = len(row)
            widths[w] += 1
            last_w = w
            lst = first_lines.setdefault(w, [])
            if len(lst) < 5:
                lst.append(rd.line_num)
            if header is not None and row == header:
                rep_header += 1
    return header, widths, first_lines, n, blank, rep_header, last_w


# ──────────────────────────────────────────────────────────────
def load_app():
    """앱 모듈 로드 (server 가 CoolProp 등을 import 하므로 출력은 숨김)."""
    try:
        cwd = os.getcwd()
        os.chdir(ROOT)
        (srv, pre), _ = quiet(lambda: (__import__("server"), __import__("preprocessor")))
        os.chdir(cwd)
        return srv, pre
    except Exception as e:
        print("\n[오류] 앱 모듈을 불러오지 못했습니다:", type(e).__name__, e)
        print("  run.bat 을 한 번 실행해 패키지를 설치한 뒤 다시 시도하세요.")
        traceback.print_exc()
        sys.exit(1)


def diagnose_ams(R: Report, path: Path, label: str, srv, pre, cfg, fr) -> dict:
    import numpy as np
    import pandas as pd

    info = {"ok": False, "first_sec": None, "last_sec": None, "dur_min": None}
    R.ctx = label
    R.h(f"AMS 파일: {label}")
    size_mb = path.stat().st_size / 1e6
    R.add(f"  크기 {size_mb:.1f} MB")

    # ── 1. 앱과 같은 경로로 읽기 ──
    R.sub("1. 읽기 (앱 read_source_csv 와 동일 경로)")
    srv._READ_NOTES.clear()
    t0 = time.perf_counter()
    try:
        df, out_lines = quiet(srv.read_source_csv, str(path), fr, "ams", 1)
    except Exception as e:
        R.flag("err", f"읽기 중 예외: {type(e).__name__}: {e}")
        return info
    t_read = time.perf_counter() - t0
    notes = [n.strip() for n in srv._READ_NOTES]
    if df is None or df.empty:
        R.flag("err", "읽기 결과가 비어 있음 — 앱에서도 이 파일은 빈 데이터로 처리됨")
        for n in notes:
            R.add(f"      · {n}")
        return info
    if "ParserWarning" in WARN_LOG:
        R.flag("warn", "칸 수가 다른 행 때문에 파일 전체를 느린 파서(python 엔진)로 읽음 — 큰 파일에서 읽기 시간이 크게 늘어나는 원인")
    mem = df.memory_usage(deep=True).sum() / 1e6
    R.add(f"  {len(df):,}행 × {len(df.columns)}열 | 읽기 {t_read:.1f}s | 메모리 {mem:.0f} MB")
    for n in notes:
        R.add(f"  · {n}")

    skip_cfg = (fr.get("ams") or {}).get("skip_rows", 1)
    header_row = skip_cfg
    for n in notes:
        m = re.search(r"skip_rows (\d+) → (\d+)", n)
        if m:
            header_row = int(m.group(2))
    if header_row != skip_cfg:
        R.flag("info", f"헤더 행 자동 조정 {skip_cfg} → {header_row} (설정의 스킵 행을 {header_row}(으)로 바꾸면 명확해짐)")

    extra = [c for c in df.columns if c.startswith("_extra_")]
    split = [c for c in df.columns if re.search(r"#\d+$", c)]
    unnamed = [c for c in df.columns if c.lower().startswith("unnamed")]
    if split:
        R.flag("info", f"쉼표 포함 헤더 분할 보정 {len(split)}칸 (정상 처리됨)")
    if extra:
        R.flag("warn", f"이름 없는 초과 칸 {len(extra)}개 — 헤더보다 데이터가 넓지만 원인 불명, 뒤쪽 컬럼 정렬 확인 필요")
    if unnamed:
        R.flag("warn", f"Unnamed 컬럼 {len(unnamed)}개 — 헤더 행 위치가 어긋났을 가능성")

    # ── 2. 원본 전체 스캔 ──
    R.sub("2. 원본 전체 스캔 (모든 행의 칸 수)")
    enc_full = detect_encoding(path)
    enc_head = detect_encoding(path, max_lines=8)
    if enc_full != enc_head:
        R.flag("warn", f"인코딩 혼재 — 앞 8행은 {enc_head}, 파일 전체는 {enc_full} (앱은 앞부분 기준, 뒤쪽 한글 깨질 수 있음)")
    else:
        R.add(f"  인코딩 {enc_full}")
    sep = "\t" if str(path).lower().endswith(".tsv") else ","
    try:
        header, widths, first_lines, n_data, blank, rep_header, last_w = scan_widths(path, enc_full, sep, header_row)
        hw = len(header or [])
        mode_w, mode_n = widths.most_common(1)[0] if widths else (0, 0)
        R.add(f"  헤더 {hw}칸 | 데이터 {n_data:,}행 | 최빈 칸 수 {mode_w} ({mode_n:,}행)")
        others = {w: c for w, c in widths.items() if w != mode_w}
        if others:
            desc = ", ".join(f"{w}칸 {c}행(예: {first_lines[w][0]}번째 줄)" for w, c in sorted(others.items()))
            only_last = len(others) == 1 and sum(others.values()) == 1 and last_w in others
            if only_last:
                R.flag("info", f"마지막 행만 칸 수 다름({last_w}칸) — 로깅 종료 시 잘린 줄로 보임, 영향 미미")
            else:
                R.flag("warn", f"칸 수가 다른 행 존재: {desc} — 로거 재시작/줄 깨짐 의심, 해당 행 값이 밀릴 수 있음")
        else:
            R.flag("ok", "모든 데이터 행의 칸 수 동일")
        if blank:
            R.flag("info", f"빈 행 {blank}개 (무시됨)")
        if rep_header:
            R.flag("warn", f"파일 중간에 헤더가 {rep_header}번 반복됨 — 로거 재연결로 보임, 해당 행은 문자로 읽혀 컬럼 전체가 문자형이 될 수 있음")
        if n_data and abs(len(df) - n_data) > max(2, n_data * 0.001):
            R.flag("warn", f"앱이 읽은 행 수({len(df):,})와 원본 데이터 행 수({n_data:,})가 다름")
    except Exception as e:
        R.flag("warn", f"원본 스캔 실패: {type(e).__name__}: {e}")

    # ── 3. 컬럼 품질 ──
    R.sub("3. 컬럼 품질 (문자 혼입 · 센서 무효값)")
    cm = cfg.get("column_mappings", {}).get("ams", {}) or {}
    mapped_raw = {raw for raw in cm if raw in df.columns}
    sub_raw = {raw for raw, std in cm.items() if std == "SubProcess"}
    mixed, sentinel_cols, allnan, const = [], [], 0, 0
    dq = df
    if "Time" in df.columns:   # 중간 헤더 반복 행은 섹션 2에서 보고했으므로 제외하고 검사
        hdr_rows = df.index[df["Time"].astype(str).str.strip().eq("Time")]
        if len(hdr_rows):
            dq = df.drop(index=hdr_rows)
            R.add(f"  · 중간 헤더 반복 행 {len(hdr_rows)}개는 제외하고 검사")
    for c in dq.columns:
        s = dq[c]
        if s.isna().all():
            allnan += 1
            continue
        if c == "Time" or c in sub_raw:
            continue
        vals = s
        if not pd.api.types.is_numeric_dtype(s):   # pandas 3 의 str 타입도 포함
            num = pd.to_numeric(s, errors="coerce")
            txt = s.astype(str).str.strip()
            nonnull = s.notna() & (txt != "") & (txt.str.lower() != "nan")
            k = int(nonnull.sum())
            bad = nonnull & num.isna()
            nb = int(bad.sum())
            if k and nb and nb < k * 0.5:
                tokens = Counter(txt[bad].str.slice(0, 15)).most_common(3)
                mixed.append((c, nb, k, tokens))
            vals = num
        if getattr(vals, "dtype", None) is not None and vals.dtype.kind in "fiu":
            sc = int(vals.isin(SENTINELS).sum())
            if sc:
                sentinel_cols.append((c, sc))
            if vals.nunique(dropna=True) <= 1:
                const += 1
    if mixed:
        for c, nb, k, tokens in mixed:
            used = c in mapped_raw
            std = cm.get(c, "")
            lvl = "warn" if used else "info"
            tk = ", ".join(f"'{t}'×{n}" for t, n in tokens)
            tag = f" → {std}" if used else " (미사용 컬럼)"
            R.flag(lvl, f"문자 혼입 '{c}'{tag}: {nb}/{k}칸이 숫자가 아님 [{tk}] — 해당 칸은 빈 값 처리됨")
    else:
        R.flag("ok", "숫자 컬럼에 문자 혼입 없음")
    if sentinel_cols:
        used = [(c, n) for c, n in sentinel_cols if c in mapped_raw]
        unused = len(sentinel_cols) - len(used)
        if used:
            R.flag("info", "센서 무효값(65535 등) — 앱이 빈 값 처리: " +
                   ", ".join(f"'{c}' {n}개" for c, n in sorted(used, key=lambda x: -x[1])[:8]))
        if unused:
            R.add(f"  · 미사용 컬럼 중 무효값 포함 {unused}개")
    R.add(f"  · 전부 빈 컬럼 {allnan}개 | 값이 하나뿐인 컬럼 {const}개")

    # ── 4. 전처리 (앱 preprocess_ams 와 동일) ──
    R.sub("4. 전처리 (앱 preprocess_ams 와 동일: 이름 변경 · 배율 · 행정 · 시간)")
    raw_time = df["Time"] if "Time" in df.columns else None
    if raw_time is None:
        R.flag("err", f"'Time' 컬럼 없음 — 앞 5개 컬럼: {list(df.columns[:5])}")
        return info
    try:
        out, lines = quiet(
            pre.preprocess_ams,
            df_raw=df,
            column_mapping=cm,
            scale_factors=cfg.get("ams_scale_factors", {}) or {},
            subprocess_mapping=cfg.get("subprocess_mapping", {}) or {},
            selected_columns=(cfg.get("selected_columns", {}) or {}).get("ams", []),
        )
    except Exception as e:
        R.flag("err", f"전처리 중 예외 → 앱 Merge 도 여기서 실패함: {type(e).__name__}: {e}")
        R.add("      " + traceback.format_exc().strip().splitlines()[-2].strip())
        return info
    for ln in (out.attrs.get("ams_diag") or lines):
        R.add(f"  · {ln.strip()}")
    missing_sel = [c for c in (cfg.get("selected_columns", {}) or {}).get("ams", []) if c not in out.columns]
    R.add(f"  결과 {len(out):,}행 × {len(out.columns)}열")
    if missing_sel:
        R.flag("info", f"설정의 선택 변수 중 없는 것 {len(missing_sel)}개: {missing_sel}")

    # 다운스트림 요구 변수
    for title, groups in REQUIRED:
        miss = [g[0] for g in groups if not any(n in out.columns for n in g)]
        if not miss:
            R.flag("ok", f"{title}: 필요 변수 모두 있음")
        else:
            R.flag("warn", f"{title}: 없는 변수 {miss} — 해당 계산은 건너뛰어짐")

    # 주요 변수 통계 + 물리 타당성
    R.sub("5. 주요 변수 (0·무효값 제외, 최소 / 중앙 / 최대)")
    for std, desc in KEY_VARS.items():
        if std not in out.columns:
            continue
        v = pd.to_numeric(out[std], errors="coerce")
        v = v[(v != 0) & ~v.isin(SENTINELS)].dropna()
        if v.empty:
            R.add(f"  {std:<22s} {desc:<16s} 값 없음(전부 0/빈 값)")
            continue
        med = float(v.median())
        R.add(f"  {std:<22s} {desc:<16s} {fmt_num(v.min()):>8s} / {fmt_num(med):>8s} / {fmt_num(v.max()):>8s}")
        if std == "HP_SuperHeatDegree" and med > 30:
            R.flag("warn", f"과열도 중앙값 {med:.1f} — 통상 범위(수 K)를 크게 넘음, 로거가 ×10으로 저장했을 가능성 (배율 0.1 필요)")
        if "Temp" in std and med > 150:
            R.flag("warn", f"{std} 중앙값 {med:.0f} — 온도로 보기 어려움, 배율(×0.1) 누락 의심")
        if std == "Po_WD" and med > 5000:
            R.flag("warn", f"소비 전력 중앙값 {med:.0f} — 단위/배율 확인 필요")
    if "HP_CompCurrentHz" in out.columns and "HP_CompTargetHz" in out.columns:
        a = pd.to_numeric(out["HP_CompCurrentHz"], errors="coerce")
        b = pd.to_numeric(out["HP_CompTargetHz"], errors="coerce")
        run = (a > 5) & (b > 5)
        if run.sum() > 10:
            r = float((b[run] / a[run]).median())
            if not 0.5 <= r <= 2.0:
                R.flag("warn", f"운전 구간 목표/현재 Hz 비 {r:.2f} — 둘 중 하나의 배율이 틀렸을 가능성")
    if "SubProcess" in out.columns:
        vc = out["SubProcess"].astype(str).value_counts().head(8)
        R.add("  SubProcess 분포: " + ", ".join(f"{k}:{v:,}" for k, v in vc.items()))
        non_num = [k for k in vc.index if not re.fullmatch(r"-?\d+(\.0)?", str(k)) and k != "nan"]
        if non_num:
            R.flag("warn", f"SubProcess 에 매핑 안 된 값 {non_num} — subprocess_mapping 에 추가 필요")

    # ── 6. 시간축 ──
    R.sub("6. 시간축")
    kinds = Counter(time_kind(v) for v in raw_time.values)
    R.add("  원본 형식: " + ", ".join(f"{k} {v:,}" for k, v in kinds.most_common()))
    norm = out["Time"]
    try:
        dt = pd.to_datetime(norm, errors="coerce", format="mixed")
    except (TypeError, ValueError):
        dt = pd.to_datetime(norm, errors="coerce")
    nat = int(dt.isna().sum())
    if nat:
        lvl = "err" if nat > len(dt) * 0.05 else "warn"
        R.flag(lvl, f"시간 변환 실패(NaT) {nat:,}행 ({nat/len(dt)*100:.1f}%) — 해당 행은 병합에서 빠짐")
    ok = dt.dropna()
    if len(ok) < 2:
        R.flag("err", "유효한 시간이 2개 미만 — 병합 불가")
        return info
    sec = (ok - ok.iloc[0]).dt.total_seconds()
    d = sec.diff().dropna()
    dur = float(sec.max() - sec.min())
    uniq = int(norm.nunique())
    back = int((d < 0).sum())
    big = int((d > 5).sum())
    rate = len(ok) / max(dur, 1.0)
    R.add(f"  길이 {dur/60:.1f}분 | 1초 격자 고유 {uniq:,}개 | 샘플링 약 {rate:.1f}Hz")
    if back:
        if d.min() < -43200:
            R.flag("warn", f"시간이 자정을 넘어 되돌아감({back}회) — 벽시계 시간 형식이면 병합 시 순서가 뒤집힐 수 있음")
        else:
            R.flag("warn", f"시간이 뒤로 가는 구간 {back}회 — 로거 재시작 또는 롤오버 미보정 의심")
    else:
        R.flag("ok", "시간이 단조 증가")
    if big:
        R.flag("info", f"5초 넘는 공백 {big}회 (최대 {d.max():.0f}초)")
    if uniq < dur * 0.9:
        R.flag("warn", f"1초 격자 고유값({uniq:,})이 길이({dur:.0f}초)보다 적음 — 데이터 공백 또는 시간 중복")
    if kinds.get("MM:SS.f", 0) > len(raw_time) * 0.5:
        R.flag("info", "시간이 MM:SS.f 형식(시 자리 없음) — Excel 로 열었다 저장한 파일일 수 있음. 60분마다 롤오버를 앱이 보정함")

    info.update(ok=True, dur_min=dur / 60, first_sec=float(sec.iloc[0]), last_sec=float(sec.iloc[-1]),
                first_t=str(norm.iloc[0]), last_t=str(norm.iloc[-1]))
    return info


def diagnose_table(R: Report, path: Path, label: str, srv, cfg, fr, mask: bool) -> dict:
    """AMS 가 아닌 계측 파일(.dat/.txt/.tsv 등) — 형식(구조)과 시간 축만 요약. 데이터 값은 넣지 않음."""
    import pandas as pd

    R.ctx = label
    R.h(f"계측 파일: {label}")

    def hide(s: str) -> str:      # --mask: 파일 이름·열 이름('…') 가림
        if not mask:
            return s
        return re.sub(r"'[^']*'", "'…'", s.replace(path.name, label))
    R.add(f"  크기 {path.stat().st_size / 1e6:.2f} MB")
    kind, enc_hint = srv._file_kind(str(path))
    if kind == "binary":
        R.flag("err", "텍스트가 아닌 바이너리 파일 — 앱에서 읽을 수 없음. 측정 프로그램에서 CSV/텍스트로 내보내기 필요")
        if not mask:
            with open(path, "rb") as f:
                R.add(f"  앞 16바이트(형식 식별용): {f.read(16).hex(' ')}")
        return {"ok": False}
    if kind == "excel":
        R.flag("info", "확장자와 달리 엑셀 형식 파일 — Setting 파일 식별 규칙에서 이 확장자를 MX100 에 넣어야 읽힘")
        return {"ok": False}

    R.sub("1. 형식 판별 (앞 300줄)")
    st = srv._sniff_table(str(path), enc_hint)
    if st is None:
        R.flag("err", "표 형태(구분자로 나뉜 열 + 숫자 데이터 줄)를 찾지 못함 — 앱에서 읽지 않음")
        lines, enc = srv._read_head_lines(str(path), enc_hint, 8)
        R.add(f"  인코딩 {enc}")
        for i, ln in enumerate(lines[:8]):     # 값 없이 줄 모양만
            R.add(f"  {i + 1}번째 줄: {len(ln)}자, 탭 {ln.count(chr(9))} · 쉼표 {ln.count(',')} · "
                  f"세미콜론 {ln.count(';')} · 공백 {ln.count(' ')}")
        if not mask and lines:
            R.add(f"  첫 줄(형식 식별용): {lines[0][:80]}")
        return {"ok": False}
    sep_name = srv._SEP_NAMES.get(st["sep"], repr(st["sep"]))
    R.add(f"  인코딩 {st['enc']} | 구분자 {sep_name} | 소수점 '{st['dec']}'")
    R.add(f"  헤더 {st['header'] + 1}번째 줄" if st["header"] is not None else "  헤더 줄 없음 → 열 이름 Col1, Col2 …")
    R.add(f"  앞쪽 설명 {st['preamble']}줄 | 헤더 아래 단위·보조 줄 {st['units']}개 | "
          f"데이터 시작 {st['data_line'] + 1}번째 줄 | 데이터 {st['ncols']}칸")
    if st["dt_merge"]:
        R.flag("info", "공백 구분이라 날짜·시각이 두 칸으로 갈라져 있음 → 한 칸으로 합쳐 읽음")
    if not mask:
        R.add(f"  첫 줄(형식 식별용): {st['first']}")
        R.add(f"  열 이름: {', '.join(st['names'][:60])}{' …' if len(st['names']) > 60 else ''}")

    R.sub("2. 읽기 (앱과 같은 경로)")
    srv._READ_NOTES.clear()
    t0 = time.perf_counter()
    try:
        df, _ = quiet(srv.read_source_csv, str(path), fr, "nidaq", 0)
    except Exception as e:
        R.flag("err", f"읽기 중 예외: {type(e).__name__}: {e}")
        return {"ok": False}
    for n in srv._READ_NOTES:
        R.add(f"  · {hide(n.strip())}")
    if df is None or df.empty:
        R.flag("err", "읽기 결과가 비어 있음")
        return {"ok": False}
    R.add(f"  {len(df):,}행 × {len(df.columns)}열 | 읽기 {time.perf_counter() - t0:.1f}s")

    R.sub("3. 시간 축")
    raw_t = srv._pick_time_col(df.columns)
    ni, lines = quiet(srv._prep_generic_time, df.copy(), "NIDAQ")
    for ln in lines:
        R.add(f"  · {hide(ln.strip())}")
    R.add(f"  시간 열 {hide(repr(str(raw_t if raw_t is not None else df.columns[0])))} — 원본 형식 "
          f"{time_kind(df[raw_t].iloc[0] if raw_t is not None else df.iloc[0, 0])}")
    t = ni["Time"]
    if not pd.api.types.is_datetime64_any_dtype(t):
        try:
            t = pd.to_datetime(t, errors="coerce", format="mixed")
        except (TypeError, ValueError):
            t = pd.to_datetime(t, errors="coerce")
    ok = t.notna().mean() if len(t) else 0
    if ok < 0.9:
        R.flag("err" if ok < 0.5 else "warn", f"시간으로 해석되는 행 {ok * 100:.0f}% — 나머지 행은 병합에서 빠짐")
    tv = t.dropna()
    if len(tv) > 1:
        d = tv.diff().dt.total_seconds().dropna()
        back = int((d < 0).sum())
        R.add(f"  간격 중앙값 {d.median():g}초 | 길이 {(tv.iloc[-1] - tv.iloc[0]).total_seconds() / 60:.1f}분"
              f"{f' | 시간이 줄어드는 곳 {back}회(자정 넘김이면 정상 처리)' if back else ''}")
        if d.median() > 1.5:
            R.flag("info", f"측정 간격이 {d.median():g}초 — 병합 때 1초 격자에 맞춰 보간됨")

    R.sub("4. 열")
    num = ni.drop(columns=["Time"]).apply(pd.to_numeric, errors="coerce")
    empty = [c for c in num.columns if num[c].isna().all()]
    const = [c for c in num.columns if num[c].notna().any() and num[c].nunique() == 1]
    R.add(f"  숫자 열 {len(num.columns) - len(empty)}개 | 값이 전부 비거나 문자인 열 {len(empty)}개 | 값이 한 가지인 열 {len(const)}개")
    if empty and not mask:
        R.add(f"    비거나 문자인 열: {', '.join(map(str, empty[:20]))}")

    # 케이스 폴더 안에서 어느 소스로 분류되는지 (확장자 규칙 → 없으면 내용 판별)
    rules = active_rules(cfg, fr)
    nm = path.name.lower()
    owner = [k for k in ("mx100", "nidaq", "ams", "br")
             if k in rules and any(nm.endswith(e.lower()) for e in (rules[k].get("extensions") or []))]
    if owner:
        R.flag("info", f"식별 규칙상 {owner[0].upper()} 확장자 → 케이스 폴더에서 {owner[0].upper()} 로 분류")
    elif nm.endswith(srv._GENERIC_TEXT_EXTS):
        src, why = srv._auto_text_source(path, rules)
        R.flag("ok" if src else "warn", f"케이스 폴더에서 → {src.upper() if src else '미분류'} ({why})")
    return {"ok": True}


def csv_partner(dat: Path, extra: list | None = None) -> Path | None:
    """.dat 와 짝이 되는 'AMS 프로그램이 내보낸 같은 측정의 CSV' — 같은 폴더·같은 이름, 아니면 함께 끌어온 CSV 하나."""
    same = dat.with_suffix(".csv")
    if same.is_file():
        return same
    for c in dat.parent.glob("*"):
        if c.suffix.lower() == ".csv" and c.stem.lower() == dat.stem.lower():
            return c
    cs = [Path(x) for x in (extra or []) if Path(x).suffix.lower() == ".csv"]
    return cs[0] if len(cs) == 1 else None


def _proj_channels(R: Report, proj: Path, mask: bool):
    """프로젝트 파일 채널 정의 요약 (측정값 없음) — 통신 패킷 변수를 더 읽을 수 있는지 판단용."""
    import ams_proj as P
    try:
        t = P.channel_table(proj)
    except Exception as e:
        R.add(f"  프로젝트 파일 {proj.name} 채널 정의를 읽을 수 없음: {e}")
        return
    R.add(f"  프로젝트 파일 {t['proj']} 채널: 계측 {t['n_normal']}개 (모두 읽음) · 통신 패킷(STX~ETX) 안 {t['n_smart']}개 "
          f"(앱이 읽는 것 {t['n_smart_read']}개, 앱이 원 코드 위치로 패킷에서 꺼내는 값 {t['n_app_smart']}개)")
    R.add(f"    채널 속성: {', '.join(t['attrs'])}")
    h = t["pos_hints"][0] if t["pos_hints"] else None
    R.add("    " + (f"숫자 속성 '{h['attr']}' 이 원 코드 바이트 위치와 {h['known']}개 중 {h['matched']}개 일치 (차이 {h['shift']})"
                     if h else "통신 패킷 채널에 원 코드 바이트 위치와 견줄 숫자 속성 없음"))
    pc = t.get("pos_check") or {}
    R.add("    패킷 위치 정보: " + ("확인됨" if pc.get("ok") else f"확인 안 됨 — {pc.get('why', '-')}"))
    for f in t.get("extra") or []:      # 원 코드 48개 외에 더 읽는 변수 (Heatpump_Comp Current 등)
        if f.get("normal"):
            R.flag("ok", f"{f['name']}: 계측 채널로 읽음 ({f.get('col')})")
        elif "pos" in f:
            R.flag("ok", f"{f['name']}: .proj 위치 {f['pos']} ({f['size']}바이트, {f['how']}) 에서 읽음")
        else:
            R.flag("info" if f.get("missing") else "warn", f"{f['name']}: 못 읽음 — {f['why']}")
    if not mask:
        sm = [r for r in t["rows"] if r["zone"] == "smart"]
        for r in sm[:120]:
            R.add("      " + " | ".join(f"{k}={v}" for k, v in r["attrs"].items()) + (" ← 앱이 읽음" if r["read"] else ""))
        if len(sm) > 120:
            R.add(f"      … 외 {len(sm) - 120}개")


def diagnose_binary(R: Report, path: Path, label: str, srv, cfg, fr, mask: bool, partner: Path | None) -> dict:
    """바이너리 .dat — 구조 첫인상, 학습된 AMS 형식과 맞는지, (짝 CSV 가 있으면) 형식 학습."""
    import ams_dat as A

    R.ctx = label
    R.h(f"바이너리 파일: {label}")
    buf = A.read_bytes(path)
    fl = A.first_look(buf)
    R.add(f"  크기 {len(buf) / 1e6:.2f} MB | 엔트로피 {fl['entropy']:.2f} 비트/바이트"
          f"{' (압축·암호화 수준)' if fl['entropy'] > 7.5 else ''}"
          + (f" | 형식 서명: {fl['sig']}" if fl["sig"] else ""))
    if not mask:
        R.add(f"  앞 16바이트: {fl['head_hex']}")
    if fl["period"]:
        R.add(f"  같은 길이 기록 반복: {fl['period']['p']}바이트 × {fl['records']:,}개 "
              f"(기록 시작 {fl['start']}바이트, 끝에 남는 {fl['tail']}바이트)")
    else:
        R.add("  같은 길이 기록이 반복되는 구조는 안 보임")
    if fl["strings"] and not mask:
        R.add("  앞부분 글자: " + " | ".join(f"@{o} {t[:30]}" for o, t, _ in fl["strings"][:12]))

    # ① 같은 폴더의 AMS 프로젝트 파일(.proj) 채널 배치로 바로 읽히는지 (학습 불필요)
    import ams_proj as P
    why = []
    lay = P.layout_for(path, why)
    if lay is not None:
        df, _ = quiet(P.read, path, None, lambda *a: None, lay)
        R.flag("warn" if lay.get("weak") else "ok",
               f"프로젝트 파일 {Path(lay['proj']).name} 의 채널 배치로 앱에서 AMS 로 바로 읽힘 (학습 불필요): "
               f"{len(df):,}행 × {len(df.columns)}열, 기록 {lay['rec']}바이트"
               + (" — 시각 값을 확인하지 못해 원 코드처럼 그대로 읽음" if lay.get("weak") else ""))
        R.add(f"    확인: {lay['check']}")
        R.add(f"    시각 기준일: {lay.get('epoch') or '알 수 없음 → 원 코드처럼 시:분:초만'}"
              + (f" | .proj 계산 기록 {lay['rec_proj']}바이트 → 실제 {lay['rec']}바이트" if lay['rec'] != lay.get('rec_proj') else ""))
        if not mask and len(df):
            R.add(f"    시각 {df['Time'].iloc[0]} ~ {df['Time'].iloc[-1]}")
            R.add("    계측 채널: " + (", ".join(c for c, _, _ in lay["chans"][:20]) or "없음"))
        _proj_channels(R, Path(lay["proj"]), mask)
        return {"ok": True}
    R.add(f"  프로젝트 파일(.proj)로는 못 읽음: {why[0] if why else '-'}")
    for q in P.find_proj(path)[:1]:
        _proj_channels(R, q, mask)

    sp, st = A.find_spec(path, head=buf)
    if sp is not None:
        df, lines = quiet(A.decode, buf, sp, st)
        R.flag("ok", f"학습된 AMS 형식({sp.get('_file')})과 일치 → 앱에서 AMS 로 읽힘: {len(df):,}행 × {len(df.columns)}열")
        miss = [c for c in sp["csv_columns"] if c not in df.columns]
        if miss:
            R.add(f"    형식 정보에 위치가 없는 열 {len(miss)}개" + ("" if mask else f": {', '.join(miss[:15])}"))
        if partner is None:
            return {"ok": True}

    if partner is None:
        R.flag("info", "AMS 프로그램에서 이 .dat 를 CSV 로 내보낸 파일을 같은 폴더에 같은 이름(.csv)으로 두고 "
                       "다시 실행하면 형식을 학습함 (한 번만 하면 같은 형식의 .dat 는 앱이 바로 읽음)")
        return {"ok": False}

    R.sub("AMS 형식 학습 (.dat ↔ 같은 측정의 CSV)")
    R.add(f"  짝 CSV: {'(이름 가림)' if mask else partner.name}")
    srv._READ_NOTES.clear()
    df, _ = quiet(srv.read_source_csv, str(partner), fr, "ams", 1)
    if df is None or df.empty:
        R.flag("err", "짝 CSV 를 읽지 못함")
        return {"ok": False}
    t0 = time.perf_counter()
    res, _ = quiet(A.learn, buf, df)
    for n in res["notes"]:
        R.add(f"  · {n}")
    v = res.get("verify")
    if v:
        R.add(f"  검증: 값 일치 {v['overall'] * 100:.3f}% | 시각 일치 {v['time_ok'] * 100:.2f}% ({v['rows']:,}행) "
              f"| 학습 {time.perf_counter() - t0:.1f}s")
        sp_new = res["spec"]
        R.add(f"  찾은 열 {len(sp_new['columns'])}개 + 상태 코드 열 {len(sp_new['codes'])}개 / "
              f"값이 변하는 열 중 {res['coverage'] * 100:.0f}%")
        low = {k: x for k, x in v["cols"].items() if x < 0.999}
        if low:
            R.add("  일치율 낮은 열: " + ", ".join(f"{'열' if mask else k} {x * 100:.1f}%" for k, x in list(low.items())[:10]))
        if res.get("unmapped"):
            cst = set(res.get("unmapped_const", []))
            R.add(f"  위치를 못 찾은 열 {len(res['unmapped'])}개 (그중 학습 파일에서 값이 일정했던 열 {len(cst)}개)"
                  + ("" if mask else ": " + ", ".join(res["unmapped"][:20])))
            if cst:
                R.flag("info", "값이 일정했던 열은 위치를 특정할 수 없음 — 그 값이 변하는 다른 측정의 (.dat, .csv) "
                               "쌍으로 한 번 더 학습하면 형식 정보에 추가됨")
        lr = sp_new["learned"]
        if lr["sign_unsure"]:
            R.add(f"  ± 부호를 학습 파일로 확정 못 한 열 {len(lr['sign_unsure'])}개 → 온도류 이름은 ±, 나머지는 + 로 가정")
        if lr["width_unsure"]:
            R.add(f"  1/2바이트 폭을 추정한 열 {len(lr['width_unsure'])}개 (옆 바이트가 늘 0)")
    if res["ok"]:
        out = A.save_spec(res["spec"])
        R.flag("ok", f"형식 학습 성공 → {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out} 저장. "
                     f"이제 앱이 같은 형식의 AMS .dat 를 CSV 없이 바로 읽음 (서버 재시작 불필요)")
    else:
        R.flag("err", f"형식 학습 실패 — {res['why']}")
    return {"ok": bool(res["ok"])}


def active_rules(cfg: dict, fr: dict) -> dict:
    act = None
    for sec in cfg.values():
        if isinstance(sec, dict) and "data_sources" in sec:
            act = {s.lower() for s in (sec["data_sources"] or [])}
    return {k: v for k, v in (fr or {}).items() if act is None or k in act}


def diagnose_case(R: Report, folder: Path, label: str, srv, pre, cfg, fr, mask: bool, counter: dict):
    R.ctx = label
    R.h(f"케이스 폴더: {label}")
    active = None
    for sec in cfg.values():
        if isinstance(sec, dict) and "data_sources" in sec:
            active = sec["data_sources"]
    try:
        files, lines = quiet(srv._classify_files, folder, fr, active)
    except Exception as e:
        R.flag("err", f"파일 분류 실패: {type(e).__name__}: {e}")
        return
    names = sorted((x.name for x in folder.iterdir() if x.is_file()), key=len, reverse=True)
    for ln in lines:
        if "내용 판별" in ln or "미분류" in ln:
            ln = ln.strip().replace(str(folder), label)
            if mask:
                for k, nm in enumerate(names):
                    ln = ln.replace(nm, f"파일#{k + 1}")
            R.add(f"  · {ln}")
    counts = {k: len(v) for k, v in files.items()}
    R.add("  분류 결과: " + ", ".join(f"{k.upper()} {v}개" for k, v in counts.items()))
    # AMS 외 계측 파일(NIDAQ: .tsv/.dat 등) — 형식·시간 축 요약
    for fn in files.get("nidaq", []):
        counter["dat"] = counter.get("dat", 0) + 1
        lab = f"DAT#{counter['dat']}" if mask else f"{label}/{fn}"
        diagnose_table(R, folder / fn, lab, srv, cfg, fr, mask)
    # 바이너리 .dat (AMS 형식으로 잡힌 것·못 잡힌 것 모두) — 구조·형식 일치, 짝 CSV 가 있으면 학습
    for x in sorted(folder.iterdir()):
        if x.is_file() and x.suffix.lower() == ".dat" and srv._file_kind(str(x))[0] == "binary":
            counter["dat"] = counter.get("dat", 0) + 1
            lab = f"DAT#{counter['dat']}" if mask else f"{label}/{x.name}"
            diagnose_binary(R, x, lab, srv, cfg, fr, mask, csv_partner(x))
    R.ctx = label
    ams = [fn for fn in files.get("ams", []) if not (fn.lower().endswith(".dat")
                                                     and srv._file_kind(str(folder / fn))[0] == "binary")]
    if not ams:
        if any(counts.values()):
            R.flag("info", "AMS 로 인식된 파일 없음 (다른 소스 파일만 있음)")
        else:
            R.flag("warn", "AMS 로 인식된 파일 없음 — 파일명/확장자/헤더 확인 필요")
        return
    present = {k: v for k, v in counts.items() if v}
    if len(set(present.values())) > 1:
        R.flag("warn", "소스별 파일 개수가 다름 " + str(present) +
               " — 앱은 i번째 파일끼리 짝지어 run 을 만들므로 일부 run 에 소스가 빠지거나 엇갈릴 수 있음")
    infos = []
    for fn in ams:
        counter["ams"] += 1
        lab = f"AMS#{counter['ams']}" if mask else f"{label}/{fn}"
        infos.append((lab, diagnose_ams(R, folder / fn, lab, srv, pre, cfg, fr)))
    if len(infos) > 1:
        R.ctx = label
        R.sub(f"케이스 내 AMS {len(infos)}개 연속성")
        for (la, a), (lb, b) in zip(infos, infos[1:]):
            if not (a.get("ok") and b.get("ok")):
                continue
            R.add(f"  {la}: {a['first_t']} ~ {a['last_t']}  →  {lb}: {b['first_t']} ~ {b['last_t']}")
            if str(b["first_t"]).startswith("00:00:0"):
                R.flag("warn", f"{lb} 가 00:00:00 부터 다시 시작 — 한 시험이 여러 파일로 나뉜 것이라면 "
                               f"앱은 별도 run 으로 처리함 (이어붙이기 필요)")


def git_rev() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=5).stdout.strip() or "?"
    except Exception:
        return "?"


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    mask = "--mask" in argv
    if not args:
        print(__doc__)
        return 1

    R = Report()
    print("앱 모듈 불러오는 중...", flush=True)
    srv, pre = load_app()
    import pandas as pd
    cfg = srv.DEFAULT_CFG
    fr = cfg.get("file_rules", {}) or {}

    R.add("HPWD Data Manager — 데이터 진단 리포트")
    R.add(f"생성 {datetime.datetime.now():%Y-%m-%d %H:%M:%S} | 앱 {git_rev()} | "
          f"Python {sys.version.split()[0]} | pandas {pd.__version__}")
    R.add(f"AMS 설정: 스킵 행 {(fr.get('ams') or {}).get('skip_rows')}, "
          f"확장자 {(fr.get('ams') or {}).get('extensions')}, 포함 패턴 {(fr.get('ams') or {}).get('include_patterns')}")
    R.add("※ 원본 데이터 값은 포함하지 않음 (개수·칸 수·시간 길이·주요 변수 집계값만)")

    counter = {"ams": 0, "case": 0}
    paths = [Path(a.strip().strip('"')) for a in args]
    # 바이너리 .dat 와 함께 끌어온 CSV 는 '형식 학습용 짝'으로 쓰고 따로 진단하지 않음
    used_csv = set()
    for p in paths:
        if p.is_file() and p.suffix.lower() == ".dat" and srv._file_kind(str(p))[0] == "binary":
            c = csv_partner(p, [str(x) for x in paths])
            if c is not None:
                used_csv.add(c.resolve())
    for a, p in zip(args, paths):
        if not p.exists():
            R.flag("err", f"경로 없음: {a}")
            continue
        if p.is_file() and p.resolve() in used_csv:
            continue
        try:
            if p.is_file() and p.suffix.lower() == ".dat" and srv._file_kind(str(p))[0] == "binary":
                counter["dat"] = counter.get("dat", 0) + 1
                lab = f"DAT#{counter['dat']}" if mask else p.name
                diagnose_binary(R, p, lab, srv, cfg, fr, mask, csv_partner(p, [str(x) for x in paths]))
            elif p.is_file() and p.suffix.lower() != ".csv":
                # .dat/.txt/.tsv 등 — 형식(구분자·헤더·단위 줄·시간 축) 요약
                counter["dat"] = counter.get("dat", 0) + 1
                lab = f"DAT#{counter['dat']}" if mask else p.name
                diagnose_table(R, p, lab, srv, cfg, fr, mask)
            elif p.is_file():
                counter["ams"] += 1
                lab = f"AMS#{counter['ams']}" if mask else p.name
                diagnose_ams(R, p, lab, srv, pre, cfg, fr)
            else:
                has_csv = any(x.suffix.lower() in (".csv", ".tsv", ".dat", ".txt")
                              for x in p.iterdir() if x.is_file())
                cases = [p] if has_csv else sorted(x for x in p.iterdir() if x.is_dir())
                for c in cases:
                    counter["case"] += 1
                    lab = f"CASE#{counter['case']}" if mask else c.name
                    diagnose_case(R, c, lab, srv, pre, cfg, fr, mask, counter)
        except Exception as e:
            R.flag("err", f"진단 중 예외 ({p.name}): {type(e).__name__}: {e}")
            R.add(traceback.format_exc())

    # ── 종합 ──
    R.h("종합")
    errs = [f for f in R.findings if f[0] == "err"]
    warns = [f for f in R.findings if f[0] == "warn"]
    R.add(f"  ❌ {len(errs)}개 | ⚠️ {len(warns)}개")
    for lvl, ctx, msg in errs + warns:
        R.add(f"  {Report.ICON[lvl]} [{ctx}] {msg}")
    if errs:
        verdict = "앱에서 정상 처리되지 않는 항목이 있음 — ❌ 부터 확인"
    elif warns:
        verdict = "처리는 되지만 결과 해석에 주의가 필요한 항목이 있음"
    else:
        verdict = "앱 처리 경로상 문제 없음"
    R.add("")
    R.add(f"  💡 {verdict}")

    out_dir = ROOT / "results" / "_diagnose"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"report_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt"
    out.write_text("\n".join(R.lines), encoding="utf-8")
    print(f"\n리포트 저장: {out}")
    print("이 리포트 파일 내용을 그대로 복사해서 전달하면 됩니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
