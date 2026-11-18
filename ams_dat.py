"""
AMS 바이너리 .dat — 같은 측정을 AMS 프로그램이 CSV 로 내보낸 파일과 비교해 형식을 '학습'하고,
학습한 형식으로 CSV 없이 바로 읽는다.

AMS 프로그램의 .dat 는 형식 문서가 없는 바이너리라 그냥은 읽을 수 없다. 하지만 같은 측정의
(.dat, .csv) 한 쌍이 있으면 두 파일을 맞대어
  · 헤더 길이와 기록(패킷) 하나의 길이
  · CSV 각 열이 기록 안 어느 위치에 어떤 형식(정수/실수, 바이트 순서)·배율로 들어 있는지
  · 시각을 어떻게 저장하는지 (FILETIME·SYSTEMTIME·Unix 시각·경과 ms·헤더 시작 시각 + 간격 …)
  · 상태 열(행정 정보 등)의 코드 → 이름 표
를 찾을 수 있다. 결과는 '형식 정보'(JSON) 로 config/ams_dat_formats/ 에 저장한다.
형식 정보에는 측정값이 들어가지 않는다 (위치·형식·배율·상태 이름표·헤더 앞 8바이트뿐).
학습·검증은 전부 사용자 PC 안에서 이뤄진다 (tools/diagnose.py 가 실행).

가정: 헤더 뒤에 같은 길이의 기록이 반복되는 형식(고정 길이 패킷). 압축·가변 길이 형식이면
학습이 실패하고, 리포트에 그 이유(엔트로피·반복 주기 등)가 남는다.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

FORMAT_DIR = Path(__file__).resolve().parent / "config" / "ams_dat_formats"
SPEC_VERSION = "ams-dat-1"

# ══════════════════════════════════════════════
#  1. 바이너리 첫인상 (CSV 없이도 가능)
# ══════════════════════════════════════════════
_SIGS = [
    (b"SQLite format 3\x00", "SQLite 데이터베이스"),
    (b"\x1f\x8b", "gzip 압축"),
    (b"PK\x03\x04", "zip 압축(또는 xlsx)"),
    (b"7z\xbc\xaf\x27\x1c", "7z 압축"),
    (b"BZh", "bzip2 압축"),
    (b"\xfd7zXZ\x00", "xz 압축"),
    (b"\x89HDF\r\n\x1a\n", "HDF5"),
    (b"MATLAB 5.0 MAT-file", "MATLAB .mat"),
    (b"TDSm", "NI TDMS"),
    (b"\x00\x01\x00\x00\x00\xff\xff\xff\xff\x01\x00\x00\x00", ".NET BinaryFormatter 직렬화"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "OLE2 복합 문서(구형 Office 형식 등)"),
]
_COMPRESSED = ("gzip", "zip", "7z", "bzip2", "xz", "zlib")


def read_bytes(path, limit: int | None = None) -> bytes:
    with open(path, "rb") as f:
        return f.read() if limit is None else f.read(limit)


def entropy(b: bytes) -> float:
    """바이트 엔트로피 (비트/바이트). 압축·암호화 데이터는 8 에 가깝다."""
    if not b:
        return 0.0
    cnt = np.bincount(np.frombuffer(b, np.uint8), minlength=256).astype(float)
    p = cnt[cnt > 0] / len(b)
    return float(-(p * np.log2(p)).sum())


_RX_ASCII = re.compile(rb"[\x20-\x7e]{5,}")
_RX_U16 = re.compile(rb"(?:[\x20-\x7e]\x00|[\x00-\xff][\xac-\xd7]){4,}")
_RX_CP949 = re.compile(rb"(?:[\xb0-\xc8][\xa1-\xfe]|[\x20-\x7e]){4,}")


def find_strings(buf: bytes, limit: int = 65536, max_n: int = 60) -> list:
    """앞부분에 들어 있는 글자열 (헤더의 모델명·채널 이름 등) → [(위치, 글자, 인코딩)]."""
    head = buf[:limit]
    out = [(m.start(), m.group().decode("ascii"), "ascii") for m in _RX_ASCII.finditer(head)]
    for m in _RX_U16.finditer(head):
        try:
            s = m.group().decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        if sum(ch.isalnum() for ch in s) >= 0.6 * len(s):
            out.append((m.start(), s, "utf-16"))
    for m in _RX_CP949.finditer(head):
        g = m.group()
        if re.search(rb"[\xb0-\xc8][\xa1-\xfe]", g):
            try:
                out.append((m.start(), g.decode("cp949"), "cp949"))
            except UnicodeDecodeError:
                pass
    out.sort()
    return out[:max_n]


def find_period(a: np.ndarray, maxp: int = 4096, win: int = 131072) -> dict | None:
    """고정 길이 기록의 길이 추정.

    a[i] == a[i+p] 인 비율을 p 마다 구하면, 기록 길이(와 그 배수)에서 주변보다 툭 튀어나온다.
    가장 크게 튄 p 의 약수 중 충분히 튀는 가장 작은 값을 기본 주기로 본다."""
    n_all = len(a)
    if n_all < 256:
        return None
    start = min(n_all // 8, 65536)          # 헤더를 피해서
    seg = a[start:]
    maxp = int(min(maxp, len(seg) // 4))
    if maxp < 4:
        return None
    n = int(min(win, len(seg) - maxp))
    base = seg[:n]
    ratio = np.zeros(maxp + 1)
    for p in range(1, maxp + 1):
        ratio[p] = np.count_nonzero(base == seg[p:p + n]) / n
    prom = np.zeros_like(ratio)
    for p in range(4, maxp + 1):
        lo, hi = max(2, p - 8), min(maxp, p + 8)
        nb = np.concatenate([ratio[lo:p], ratio[p + 1:hi + 1]])
        prom[p] = ratio[p] - (np.median(nb) if len(nb) else 0)
    best = int(np.argmax(prom[4:]) + 4)
    if prom[best] < 0.05:
        return None
    p0 = best
    for k in range(64, 1, -1):
        if best % k == 0 and best // k >= 4 and prom[best // k] >= 0.6 * prom[best]:
            p0 = best // k
            break
    return {"p": p0, "prom": float(prom[p0]), "ratio": float(ratio[p0])}


def _blocks(a: np.ndarray, start: int, p: int) -> np.ndarray | None:
    n = (len(a) - start) // p
    if n < 2:
        return None
    return a[start:start + n * p].reshape(n, p)


def periodic_start(a: np.ndarray, p: int, max_header: int = 1 << 20) -> int:
    """기록이 시작되는 위치 추정 (파일 끝이 잘리지 않았다고 보고 위상 = 크기 % p).

    연속한 두 블록의 바이트 일치율이 본문 수준에 이르는 첫 블록. 0 으로 채운 헤더 영역은
    서로 같아도 '기록'이 아니므로 바이트 종류가 3가지 이상인 블록만 기록으로 본다."""
    ph = len(a) % p
    B = _blocks(a, ph, p)
    if B is None or len(B) < 4:
        return ph
    m = min(len(B) - 1, max_header // p + 4)
    sim = (B[:m] == B[1:m + 1]).mean(1)
    varied = np.array([len(np.unique(B[k])) >= 3 for k in range(m)])
    body = np.median(sim[varied]) if varied.any() else np.median(sim)
    ok = (sim >= 0.5 * body) & varied
    for k in range(m - 3):
        if ok[k] and ok[k + 1] and ok[k + 2]:
            return ph + k * p
    return ph


def constant_bytes(B: np.ndarray, thr: float = 0.999) -> list:
    """기록마다 같은 값인 바이트 위치 [(위치, 값)] — 동기 바이트·예약 칸 등."""
    out = []
    if B is None or not len(B):
        return out
    samp = B[np.linspace(0, len(B) - 1, min(len(B), 2000)).astype(int)]
    for j in range(B.shape[1]):
        vals, cnt = np.unique(samp[:, j], return_counts=True)
        k = int(np.argmax(cnt))
        if cnt[k] >= thr * len(samp):
            out.append((j, int(vals[k])))
    return out


def first_look(buf: bytes) -> dict:
    a = np.frombuffer(buf, np.uint8)
    info = {"size": len(buf), "head_hex": buf[:16].hex(" "), "sig": None,
            "entropy": entropy(buf[: 4 << 20]), "strings": []}
    for magic, name in _SIGS:
        if buf.startswith(magic):
            info["sig"] = name
            break
    if info["sig"] is None and len(buf) > 2 and buf[0] == 0x78 and buf[1] in (0x01, 0x5E, 0x9C, 0xDA):
        info["sig"] = "zlib 압축으로 보임"
    per = find_period(a)
    info["period"] = per
    if per:
        p = per["p"]
        st = periodic_start(a, p)
        info["start"] = st
        info["records"] = (len(a) - st) // p
        info["tail"] = (len(a) - st) % p
        info["const"] = constant_bytes(_blocks(a, st, p))
    # 글자열은 헤더 구간에서만 (기록 안의 숫자 바이트가 우연히 글자처럼 보이는 것 제외)
    info["strings"] = find_strings(buf[: info.get("start", 65536)])
    return info


# ══════════════════════════════════════════════
#  2. CSV 시간 열 → 초
# ══════════════════════════════════════════════
def _unroll(sec: np.ndarray, period: float) -> np.ndarray:
    if not len(sec):
        return sec
    d = np.diff(sec, prepend=sec[0])
    jumps = np.cumsum(np.nan_to_num(d) < -period / 2)
    return sec + jumps * period


def csv_time_seconds(s: pd.Series):
    """AMS CSV Time → (초, 종류, 기준 날짜). 종류: 'datetime' | 'clock' | 'elapsed'.
    preprocessor.normalize_ams_time 과 같은 판별 규칙 (다만 소수점 이하 초를 버리지 않음)."""
    t = s.astype(str).str.strip()
    n = len(t)
    if n and t.str.contains(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", regex=True).mean() >= 0.5:
        try:
            d = pd.to_datetime(t, errors="coerce", format="mixed")
        except (TypeError, ValueError):
            d = pd.to_datetime(t, errors="coerce")
        if d.notna().any():
            base = d.dropna().iloc[0].normalize()
            return (d - base).dt.total_seconds().to_numpy(float), "datetime", base
    colons = t.str.count(":")
    mode = int(colons.mode().iloc[0]) if n else 0
    if mode >= 2:
        p = t.str.extract(r"^\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
        sec = (pd.to_numeric(p[0], errors="coerce") * 3600 + pd.to_numeric(p[1], errors="coerce") * 60
               + pd.to_numeric(p[2], errors="coerce")).to_numpy(float)
        return _unroll(sec, 86400.0), "clock", None
    if mode == 1:
        p = t.str.extract(r"^\s*(\d+):(\d+(?:\.\d+)?)\s*$")
        mm = pd.to_numeric(p[0], errors="coerce")
        sec = (mm * 60 + pd.to_numeric(p[1], errors="coerce")).to_numpy(float)
        if np.nanmax(mm.to_numpy(float)) < 60:
            sec = _unroll(sec, 3600.0)
        return sec, "elapsed", None
    return pd.to_numeric(t, errors="coerce").to_numpy(float), "elapsed", None


# ══════════════════════════════════════════════
#  3. 기록 안의 값 후보
# ══════════════════════════════════════════════
_ALL_DT = ["u1", "i1", "<u2", "<i2", ">u2", ">i2", "<u4", "<i4", ">u4", ">i4",
           "<f4", ">f4", "<u8", "<i8", ">u8", ">i8", "<f8", ">f8"]
_FAST_DT = ["u1", "<u2", "<i2", ">u2", ">i2", "<u4", ">u4", "<f4", ">f4"]
_RX_SIGNED = re.compile(r"temp|tmp|온도|degree|과열|superheat|_t_|evap|eva_", re.I)   # 음수가 될 수 있는 값
_NICE_A = [1, 0.1, 0.01, 0.001, 1e-4, 1e-5, 1e-6, 1e-7, 10, 100, 1000, 0.5, 0.25, 0.2,
           0.125, 0.05, 0.02, 0.0625, 2, 4, 5]


def _records(a: np.ndarray, start: int, p: int, ext: int = 8) -> np.ndarray | None:
    """start 부터 p 바이트씩 자른 기록 + 다음 기록 앞 ext 바이트 (기록 경계에 걸친 값도 보이게)."""
    B = _blocks(a, start, p)
    if B is None:
        return None
    ext = min(ext, p)
    nxt = np.zeros((len(B), ext), np.uint8)
    nxt[:-1] = B[1:, :ext]
    return np.concatenate([B, nxt], axis=1)


def _field(E: np.ndarray, off: int, dt: str) -> np.ndarray:
    s = np.dtype(dt).itemsize
    return np.ascontiguousarray(E[:, off:off + s]).view(np.dtype(dt)).ravel()


def _fields(E: np.ndarray, dts=_ALL_DT, keep_const: bool = False):
    """E (K×W 바이트) → 모든 위치·형식으로 읽은 값 (K×nf) 과 [(위치, 형식)]."""
    K, W = E.shape
    mats, meta = [], []
    for dt in dts:
        s = np.dtype(dt).itemsize
        if W < s:
            continue
        win = np.lib.stride_tricks.sliding_window_view(E, s, axis=1)
        v = np.ascontiguousarray(win).view(np.dtype(dt)).reshape(K, W - s + 1).astype(np.float64)
        mats.append(v)
        meta.extend((o, dt) for o in range(W - s + 1))
    if not mats:
        return np.zeros((K, 0)), []
    F = np.concatenate(mats, axis=1)
    with np.errstate(all="ignore"):
        ok = np.isfinite(F).all(0)
        if not keep_const:
            ok &= np.where(ok, F.std(0), 0) > 0
        isflt = np.array([m[1][-2] == "f" for m in meta])
        absF = np.abs(np.where(np.isfinite(F), F, 0))
        weird = (absF.max(0) > 1e12) | ((absF < 1e-30) & (F != 0)).any(0)
        ok &= ~(isflt & weird)
    idx = np.where(ok)[0]
    return F[:, idx], [meta[i] for i in idx]


def _corr(F: np.ndarray, Y: np.ndarray) -> np.ndarray:
    with np.errstate(all="ignore"):
        Fz = (F - F.mean(0)) / F.std(0)
        Yz = (Y - Y.mean(0)) / Y.std(0)
        return (Fz.T @ Yz) / F.shape[0]


def _snap_a(a: float) -> float:
    for n in _NICE_A:
        for sgn in (1, -1):
            if abs(a - sgn * n) <= 1e-6 * n:
                return float(sgn * n)
    return float(f"{a:.12g}")


def _snap_b(b: float, scale: float = 1.0) -> float:
    if abs(b) <= 1e-6 * max(1.0, scale):          # float32 반올림 등으로 생긴 아주 작은 치우침
        return 0.0
    for d in range(0, 7):
        r = round(b, d)
        if abs(b - r) <= 1e-7 * max(1.0, abs(b)):
            return float(r) + 0.0
    return float(f"{b:.12g}")


def _decimals(y: np.ndarray) -> int:
    y = y[np.isfinite(y)][:3000]
    for d in range(0, 7):
        if np.all(np.abs(y - np.round(y, d)) < 1e-9 * np.maximum(1, np.abs(y))):
            return d
    return 7


def _fit(raw: np.ndarray, y: np.ndarray):
    rm, ym = raw.mean(), y.mean()
    den = ((raw - rm) ** 2).sum()
    a = ((raw - rm) * (y - ym)).sum() / den if den else 0.0
    a = _snap_a(a) + 0.0
    b = _snap_b(ym - a * rm, float(np.max(np.abs(y))) if len(y) else 1.0)
    return a, b, float(np.max(np.abs(a * raw + b - y)))


# ══════════════════════════════════════════════
#  4. 시각 형식
# ══════════════════════════════════════════════
_TICKS_UNIX = 621355968000000000      # .NET DateTime 0001-01-01 → 1970-01-01 (100ns)
_FT_UNIX = 116444736000000000         # FILETIME 1601-01-01 → 1970-01-01 (100ns)


def _abs_time(raw: np.ndarray, kind: str) -> np.ndarray:
    """숫자 → Unix 초 (float). kind: filetime | ticks | unix_s | unix_ms | unix_us | ole"""
    r = raw.astype(np.float64)
    if kind == "filetime":
        return (r - _FT_UNIX) / 1e7
    if kind == "ticks":
        rr = raw.astype(np.int64) & 0x3FFFFFFFFFFFFFFF      # DateTime.ToBinary 의 Kind 비트 제거
        return (rr.astype(np.float64) - _TICKS_UNIX) / 1e7
    if kind == "unix_s":
        return r
    if kind == "unix_ms":
        return r / 1e3
    if kind == "unix_us":
        return r / 1e6
    if kind == "ole":
        return (r - 25569.0) * 86400.0
    raise ValueError(kind)


_ABS_KINDS = {8: ("filetime", "ticks", "unix_ms", "unix_us", "unix_s", "ole"), 4: ("unix_s",)}
_Y2000, _Y2100 = 946684800.0, 4102444800.0


def _tod(unix_s: np.ndarray, tz_s: float) -> np.ndarray:
    return np.mod(unix_s + tz_s, 86400.0)


def _close_tod(a: np.ndarray, b: np.ndarray, tol: float = 1.0) -> float:
    d = np.abs(np.mod(a - b + 43200.0, 86400.0) - 43200.0)
    return float(np.mean(d <= tol))


def _local_offset_s() -> float:
    return float((_dt.datetime.now().astimezone().utcoffset() or _dt.timedelta(0)).total_seconds())


def _find_time(E: np.ndarray, S: np.ndarray, kind: str, tz_local: float, p: int):
    """기록 안의 시각 칸 찾기. E: 표본 기록 바이트, S: 같은 행의 CSV 시각(초)."""
    tzs = [0.0] + ([tz_local] if tz_local else [])
    fin = np.isfinite(S)
    if fin.mean() < 0.9:
        return None
    E, S = E[fin], S[fin]
    if kind in ("clock", "datetime"):
        Stod = np.mod(S, 86400.0)
        # (1) 숫자 하나로 된 절대 시각
        for size, kinds in _ABS_KINDS.items():
            for end in ("<", ">"):
                dts = [f"{end}u{size}", f"{end}i{size}"] + ([f"{end}f8"] if size == 8 else [])
                for dt in dts:
                    for o in range(0, p - size + 1):
                        raw = _field(E, o, dt)
                        if not np.isfinite(raw).all():
                            continue
                        for kd in kinds:
                            if kd == "ole" and dt[-2] != "f":
                                continue
                            if kd != "ole" and dt[-2] == "f" and kd not in ("unix_s", "unix_ms"):
                                continue
                            with np.errstate(all="ignore"):
                                u = _abs_time(raw, kd)
                            if not (np.isfinite(u).all() and u.min() > _Y2000 and u.max() < _Y2100):
                                continue
                            for tz in tzs:
                                if _close_tod(_tod(u, tz), Stod) >= 0.98:
                                    return {"kind": kd, "offset": o, "dtype": dt, "tz": tz}
        # (2) SYSTEMTIME (연·월·요일·일·시·분·초·ms 를 2바이트씩)
        for end in ("<", ">"):
            for o in range(0, p - 16 + 1):
                v = np.ascontiguousarray(E[:, o:o + 16]).view(np.dtype(f"{end}u2")).reshape(len(E), 8)
                yr, mo, dw, dd, hh, mi, ss, ms = (v[:, k] for k in range(8))
                if not ((yr >= 2000).all() and (yr <= 2100).all() and (mo >= 1).all() and (mo <= 12).all()
                        and (dd >= 1).all() and (dd <= 31).all() and (hh <= 23).all() and (mi <= 59).all()
                        and (ss <= 59).all() and (ms <= 999).all()):
                    continue
                tod = hh * 3600.0 + mi * 60.0 + ss + ms / 1000.0
                if _close_tod(tod, Stod) >= 0.98:
                    return {"kind": "systemtime", "offset": o, "dtype": f"{end}u2", "tz": 0.0}
        # (3) 시·분·초가 1바이트씩 붙어 있는 경우 (MCU 시계)
        for o in range(0, p - 3 + 1):
            for order in ("hms", "smh"):
                x = E[:, o:o + 3].astype(float)
                hh, mi, ss = (x[:, 0], x[:, 1], x[:, 2]) if order == "hms" else (x[:, 2], x[:, 1], x[:, 0])
                if not ((hh <= 23).all() and (mi <= 59).all() and (ss <= 59).all()) or np.std(ss) == 0:
                    continue
                if _close_tod(hh * 3600 + mi * 60 + ss, Stod) >= 0.98:
                    return {"kind": "hms_u1", "offset": o, "order": order, "tz": 0.0}
    # (4) 경과 시간 카운터 (ms·0.1s·s …): CSV 시각과 정확히 1차 관계
    F, meta = _fields(E, ["<u2", ">u2", "<u4", ">u4", "<u8", ">u8", "<f4", ">f4", "<f8", ">f8"])
    if F.shape[1]:
        C = _corr(F, S.reshape(-1, 1))[:, 0]
        for k in np.argsort(-np.abs(np.nan_to_num(C))):
            if not np.isfinite(C[k]) or abs(C[k]) < 0.99999:
                break
            raw = F[:, k]
            rm, sm = raw.mean(), S.mean()
            a = ((raw - rm) * (S - sm)).sum() / ((raw - rm) ** 2).sum()
            unit = next((u for u in (1.0, 0.1, 0.01, 0.001, 1e-4, 1e-6, 1e-7) if abs(a / u - 1) < 2e-3), None)
            if unit is None:
                continue
            a = unit
            err = float(np.max(np.abs(a * raw + (sm - a * rm) - S)))
            if err <= 1.0:
                d = np.diff(raw)
                if (d >= 0).mean() >= 0.98:        # 증가하는 값이어야 시각
                    o, dt = meta[k]
                    return {"kind": "counter", "offset": o, "dtype": dt, "unit_s": a}
    return None


_RX_HDR_DT = re.compile(rb"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})[ T_]+(\d{1,2}):(\d{2}):(\d{2})")


def _header_time(head: bytes, sf: dict | None):
    """헤더의 시작 시각(학습 때 찾은 위치·형식) → Unix 초 (현지 시각 기준). 못 읽으면 None."""
    if not sf or head is None:
        return None
    try:
        o = int(sf.get("offset", 0))
        if sf["kind"] == "text":
            m = _RX_HDR_DT.search(head)
            if not m:
                return None
            yr, mo, dd, hh, mi, ss = (int(x) for x in m.groups())
            return (pd.Timestamp(year=yr, month=mo, day=dd, hour=hh, minute=mi, second=ss)
                    - pd.Timestamp("1970-01-01")).total_seconds()
        if sf["kind"] == "systemtime":
            v = [int(x) for x in np.frombuffer(head[o:o + 16], np.dtype(sf["dtype"]))]
            return (pd.Timestamp(year=v[0], month=v[1], day=v[3], hour=v[4], minute=v[5], second=v[6])
                    - pd.Timestamp("1970-01-01")).total_seconds() + v[7] / 1000.0
        dt = np.dtype(sf["dtype"])
        raw = np.frombuffer(head[o:o + dt.itemsize], dt)
        u = float(_abs_time(raw, sf["kind"])[0]) + float(sf.get("tz", 0.0))
        return u if _Y2000 < u < _Y2100 else None
    except Exception:
        return None


def _decode_time(R: np.ndarray, t: dict, n: int, head: bytes | None = None):
    """기록 → CSV 와 같은 꼴의 시각 열. 절대 시각이면 'YYYY-MM-DD HH:MM:SS.fff', 아니면 경과 초."""
    kd = t["kind"]
    if kd in ("filetime", "ticks", "unix_s", "unix_ms", "unix_us", "ole"):
        u = _abs_time(_field(R, t["offset"], t["dtype"]), kd) + t.get("tz", 0.0)
        ts = pd.to_datetime(np.round(u * 1000).astype("int64"), unit="ms")
        return ts.strftime("%Y-%m-%d %H:%M:%S.%f").str.slice(0, 23)
    if kd == "systemtime":
        v = np.ascontiguousarray(R[:, t["offset"]:t["offset"] + 16]).view(np.dtype(t["dtype"])).reshape(len(R), 8)
        ts = pd.to_datetime(pd.DataFrame({"year": v[:, 0], "month": v[:, 1], "day": v[:, 3], "hour": v[:, 4],
                                          "minute": v[:, 5], "second": v[:, 6], "ms": v[:, 7]}), errors="coerce")
        return ts.dt.strftime("%Y-%m-%d %H:%M:%S.%f").str.slice(0, 23)
    if kd == "hms_u1":
        x = R[:, t["offset"]:t["offset"] + 3].astype(int)
        hh, mi, ss = (x[:, 0], x[:, 1], x[:, 2]) if t["order"] == "hms" else (x[:, 2], x[:, 1], x[:, 0])
        return pd.Series([f"{h:02d}:{m:02d}:{s:02d}" for h, m, s in zip(hh, mi, ss)])
    if kd == "counter":
        raw = _field(R, t["offset"], t["dtype"]).astype(np.float64)
        if t["dtype"][-2] in "ui" and len(raw) > 1:          # 정수 카운터가 한 바퀴 돌면 이어 붙임
            d = np.diff(raw)
            d[d < 0] += float(1 << (8 * np.dtype(t["dtype"]).itemsize))
            raw = np.concatenate([[0.0], np.cumsum(d)])
        else:
            raw = raw - raw[0]
        el = raw * t["unit_s"]
        st = _header_time(head, t.get("start_from"))
        if st is not None:
            ts = pd.to_datetime(np.round((st + el) * 1000).astype("int64"), unit="ms")
            return ts.strftime("%Y-%m-%d %H:%M:%S.%f").str.slice(0, 23)
        return pd.Series(el)
    if kd == "index":
        el = np.arange(n) * t["interval_s"]
        st = _header_time(head, t.get("start_from"))
        if st is not None:
            ts = pd.to_datetime(np.round((st + el) * 1000).astype("int64"), unit="ms")
            return ts.strftime("%Y-%m-%d %H:%M:%S.%f").str.slice(0, 23)
        return pd.Series(el)
    raise ValueError(kd)


def _header_start_time(head: bytes, first_tod: float, tz_local: float):
    """헤더 안에서 측정 시작 시각 찾기 (기록별 시각이 없을 때). first_tod: 첫 기록의 하루 중 초."""
    a = np.frombuffer(head, np.uint8)
    tzs = [0.0] + ([tz_local] if tz_local else [])
    want = np.array([first_tod])
    for size, kinds in _ABS_KINDS.items():
        if len(a) < size:
            continue
        win = np.ascontiguousarray(np.lib.stride_tricks.sliding_window_view(a, size))
        for end in ("<", ">"):
            for dt in [f"{end}u{size}", f"{end}i{size}"] + ([f"{end}f8"] if size == 8 else []):
                raw = win.view(np.dtype(dt)).ravel()
                for kd in kinds:
                    if (kd == "ole") != (dt[-2] == "f") and not (dt[-2] == "f" and kd == "unix_s"):
                        continue
                    with np.errstate(all="ignore"):
                        u = _abs_time(raw, kd)
                    ok = np.isfinite(u) & (u > _Y2000) & (u < _Y2100)
                    for o in np.where(ok)[0][:5000]:
                        for tz in tzs:
                            if _close_tod(_tod(np.array([u[o]]), tz), want, tol=1.5) == 1.0:
                                return {"kind": kd, "offset": int(o), "dtype": dt, "tz": tz, "unix": float(u[o] + tz)}
    if len(a) >= 16:
        for end in ("<", ">"):
            v = np.ascontiguousarray(np.lib.stride_tricks.sliding_window_view(a, 16)).view(np.dtype(f"{end}u2"))
            yr, mo, dw, dd, hh, mi, ss, ms = (v[:, k] for k in range(8))
            ok = ((yr >= 2000) & (yr <= 2100) & (mo >= 1) & (mo <= 12) & (dd >= 1) & (dd <= 31)
                  & (hh <= 23) & (mi <= 59) & (ss <= 59) & (ms <= 999))
            for o in np.where(ok)[0]:
                tod = hh[o] * 3600.0 + mi[o] * 60.0 + ss[o] + ms[o] / 1000.0
                if _close_tod(np.array([tod]), want, tol=1.5) == 1.0:
                    try:
                        u = (pd.Timestamp(year=int(yr[o]), month=int(mo[o]), day=int(dd[o]), hour=int(hh[o]),
                                          minute=int(mi[o]), second=int(ss[o])) - pd.Timestamp("1970-01-01")).total_seconds()
                    except ValueError:
                        continue
                    return {"kind": "systemtime", "offset": int(o), "dtype": f"{end}u2", "tz": 0.0,
                            "unix": float(u + ms[o] / 1000.0)}
    for m in re.finditer(rb"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})[ T_]+(\d{1,2}):(\d{2}):(\d{2})", head):
        yr, mo, dd, hh, mi, ss = (int(x) for x in m.groups())
        try:
            u = (pd.Timestamp(year=yr, month=mo, day=dd, hour=hh, minute=mi, second=ss)
                 - pd.Timestamp("1970-01-01")).total_seconds()
        except ValueError:
            continue
        if _close_tod(np.array([hh * 3600.0 + mi * 60 + ss]), want, tol=1.5) == 1.0:
            return {"kind": "text", "offset": m.start(), "tz": 0.0, "unix": float(u)}
    return None


# ══════════════════════════════════════════════
#  5. 학습
# ══════════════════════════════════════════════
def _csv_parts(df: pd.DataFrame):
    tcol = "Time" if "Time" in df.columns else df.columns[0]
    num, txt = {}, {}
    for c in df.columns:
        if c == tcol:
            continue
        s = df[c]
        st = s.astype(str).str.strip()
        nn = s.notna() & (st != "") & (st.str.lower() != "nan")
        if nn.sum() == 0:
            continue
        v = pd.to_numeric(s, errors="coerce")
        if v[nn].notna().mean() >= 0.98:
            num[c] = v.to_numpy(float)
        else:
            txt[c] = st.where(nn, None).to_numpy(object)
    S, kind, base = csv_time_seconds(df[tcol])
    return tcol, num, txt, S, kind, base


def _match_count(R, Y, rows, delta, stride=1, thr=0.99999):
    j = rows * stride + delta
    v = (j >= 0) & (j < len(R))
    if v.sum() < 20:
        return -1
    F, _ = _fields(R[j[v]], _FAST_DT)
    Yv = Y[rows[v]]
    good = np.isfinite(Yv).all(0) & (Yv.std(0) > 0)
    if not good.any() or not F.shape[1]:
        return 0
    C = _corr(F, Yv[:, good])
    return int((np.abs(np.nan_to_num(C)) >= thr).any(0).sum())


def _counter_deltas(R, packet: np.ndarray, nrec: int = 4096) -> list:
    """CSV 패킷 번호 열과 기록 안의 1씩 증가하는 칸을 맞춰 행↔기록 어긋남(δ) 후보 구하기."""
    out = []
    y = packet[np.isfinite(packet)].astype(np.int64)
    if len(y) < 10:
        return out
    E = R[:min(len(R), nrec)]
    for dt in ("u1", "<u2", ">u2", "<u4", ">u4"):
        bits = np.dtype(dt).itemsize * 8
        mod = 1 << bits
        F, meta = _fields(E, [dt])
        for k in range(F.shape[1]):
            v = F[:, k].astype(np.int64)
            if np.mean(np.mod(np.diff(v), mod) == 1) < 0.95:
                continue
            hit = np.where(np.mod(v - y[0], mod) == 0)[0]
            out += [int(h) for h in hit[:3]]
    return sorted(set(out), key=abs)[:8]


def learn(buf: bytes, df: pd.DataFrame, tz_local: float | None = None) -> dict:
    """(.dat 바이트, 같은 측정의 CSV 를 앱 경로로 읽은 DataFrame) → 학습 결과.

    반환 dict: ok, why, notes[], spec(성공 시), 검증·요약 정보."""
    res = {"ok": False, "why": "", "notes": []}
    note = res["notes"].append
    tz_local = _local_offset_s() if tz_local is None else tz_local
    a = np.frombuffer(buf, np.uint8)
    fl = first_look(buf)
    res["first_look"] = fl
    if fl["sig"] and any(k in fl["sig"] for k in _COMPRESSED):
        res["why"] = f"{fl['sig']} 형식 — 압축을 풀어야 해서 기록 구조를 직접 찾을 수 없음"
        return res
    if fl["entropy"] > 7.5:
        res["why"] = f"엔트로피 {fl['entropy']:.2f} 비트/바이트 — 압축되었거나 암호화된 데이터로 보임"
        return res
    if not fl["period"]:
        res["why"] = "같은 길이의 기록이 반복되는 구조를 찾지 못함 (가변 길이 패킷이거나 다른 형식)"
        return res
    p = fl["period"]["p"]
    start = fl["start"]

    tcol, num, txt, S, kind, base = _csv_parts(df)
    M = len(df)
    names = list(num)
    if not names:
        res["why"] = "CSV 에 숫자 열이 없음"
        return res
    Y = np.column_stack([num[c] for c in names])
    R = _records(a, start, p)
    res.update(p=p, rows=M, records=len(R))
    note(f"기록 길이 {p}바이트, 기록 시작 위치 {start}바이트 → 기록 {len(R):,}개 / CSV {M:,}행")

    # ── CSV 행 i ↔ 기록 (stride·i + δ) 찾기. stride: CSV 를 기록보다 듬성듬성(예: 8Hz → 1Hz) 내보낸 경우 ──
    rows0 = np.arange(min(M, 200))
    strides = [1]
    ratio = len(R) / max(M, 1)
    if round(ratio) >= 2 and abs(ratio - round(ratio)) <= 0.05 * round(ratio):
        strides.append(int(round(ratio)))
    pk = next((c for c in names if re.search(r"packet|패킷", str(c), re.I)), None)
    dcands = list(range(-64, 65))
    if pk is not None:
        dcands = _counter_deltas(R, num[pk]) + dcands
        dpk = np.diff(num[pk][np.isfinite(num[pk])])
        dpk = dpk[dpk > 0]
        if len(dpk) and np.median(dpk) >= 2:
            strides.append(int(np.median(dpk)))
    scores = {}
    for k_ in dict.fromkeys(strides):
        for d in dict.fromkeys(dcands):
            scores[(k_, d)] = _match_count(R, Y, rows0, d, k_)
    k_st, d0 = max(scores, key=lambda kd: (scores[kd], kd[0] == 1, -abs(kd[1])))
    if scores[(k_st, d0)] <= 0:
        res["why"] = ("CSV 열과 1:1 로 맞는 값을 기록 안에서 찾지 못함 — 같은 측정의 .dat/.csv 가 맞는지, "
                      "CSV 가 패킷을 묶어서(평균 등) 내보냈는지 확인 필요")
        return res
    if k_st > 1:
        note(f"CSV 는 기록 {k_st}개마다 1행씩 내보낸 것으로 보임 (.dat 에는 CSV 보다 {k_st}배 촘촘히 들어 있음)")

    # ── 위상(기록 경계) 바로잡기: 기록 시작을 잘못 잡으면 칸들이 δ 와 δ+1 로 갈린다 ──
    def exact_at(Rx, delta, rows):
        j = rows * k_st + delta
        v = (j >= 0) & (j < len(Rx))
        rr, jj = rows[v], j[v]
        F, meta = _fields(Rx[jj], _ALL_DT)
        found = {}
        if not F.shape[1]:
            return found, rr, jj
        Yv = Y[rr]
        good = np.isfinite(Yv).all(0) & (Yv.std(0) > 0)
        C = np.full((F.shape[1], Y.shape[1]), np.nan)
        if good.any():
            C[:, good] = _corr(F, Yv[:, good])
        for ci, name in enumerate(names):
            if not good[ci]:
                continue
            col = np.abs(np.nan_to_num(C[:, ci]))
            idx = np.where(col >= 0.99995)[0]
            if not len(idx):
                continue
            y = Yv[:, ci]
            dec = _decimals(y)
            tol = (0.5 * 10.0 ** (-dec) + 1e-9) if dec > 0 else 1e-6
            lst = []
            for k in idx:
                aa, bb, err = _fit(F[:, k], y)
                if err <= tol:
                    lst.append({"offset": meta[k][0], "dtype": meta[k][1], "a": aa, "b": bb})
            if lst:
                found[name] = lst
        return found, rr, jj

    near = {d: exact_at(R, d, rows0)[0] for d in (d0 - 1, d0, d0 + 1)}
    lo = d0 - 1 if len(near[d0 - 1]) >= len(near[d0 + 1]) else d0
    gA, gB = near[lo], near[lo + 1]              # gA: 낮은 δ (기록 뒷부분 = 다음 기록 앞부분)
    onlyA = {k: v for k, v in gA.items() if k not in gB}
    onlyB = {k: v for k, v in gB.items() if k not in gA}
    if onlyA and onlyB:
        a_min = min(min(c["offset"] for c in v) for v in onlyA.values())
        b_end = max(min(c["offset"] + np.dtype(c["dtype"]).itemsize for c in v) for v in onlyB.values())
        if b_end <= a_min < p:
            start += a_min
            R = _records(a, start, p)
            d0 = lo
            note(f"기록 경계 보정: 시작 위치 +{a_min}바이트")
        else:
            d0 = lo + 1 if len(gB) >= len(gA) else lo
    else:
        d0 = max((lo, lo + 1), key=lambda d: len(near[d]))

    # 헤더 쪽으로 기록을 더 거슬러 올라갈 수 있으면 (헤더를 길게 잡은 경우) 시작을 당김
    const = constant_bytes(_blocks(a, start, p))
    sync = [(j, v) for j, v in const if v not in (0x00, 0xFF)][:4]   # (헤더 쪽 확장용 임시 — 아래에서 다시 고름)
    back = 0
    while start - (back + 1) * p >= 0:
        blk = a[start - (back + 1) * p: start - back * p]
        if sync:
            if not all(blk[j] == v for j, v in sync):
                break
        elif len(np.unique(blk)) < 3:
            break
        else:
            sim = float(np.mean(blk == a[start - back * p: start - back * p + p]))
            if sim < 0.3:
                break
        back += 1
    if back:
        start -= back * p
        d0 += back
        R = _records(a, start, p)
        note(f"헤더 쪽으로 기록 {back}개 더 있음 → 시작 위치 {start}바이트")

    # ── 전체 표본(파일 전 구간에서 고르게)으로 칸 찾기 ──
    lo_r = max(0, -(d0 // k_st))
    while lo_r * k_st + d0 < 0:
        lo_r += 1
    hi_r = min(M, (len(R) - 1 - d0) // k_st + 1)
    if hi_r - lo_r < 20:
        res["why"] = "CSV 행과 맞는 기록이 너무 적음"
        return res
    K = int(min(600, hi_r - lo_r, max(100, 2_000_000 // ((p + 8) * len(_ALL_DT)))))
    rows = np.unique(np.linspace(lo_r, hi_r - 1, K).astype(int))
    found, rr, jj = exact_at(R, d0, rows)

    # 바이트 순서 다수결 → 동률 해소, 겹치지 않게 배정
    ends = [c["dtype"][0] for v in found.values() for c in v if c["dtype"][0] in "<>"]
    major = "<" if ends.count("<") >= ends.count(">") else ">"

    def rank(c, signed_pref=False):
        dt = c["dtype"]
        return (c["b"] != 0, abs(c["a"]) not in _NICE_A, dt[-2] == "f",
                dt[0] in "<>" and dt[0] != major, dt[-2] == ("u" if signed_pref else "i"), np.dtype(dt).itemsize)

    claimed: dict[int, str] = {}
    chosen, sign_unsure, width_unsure = {}, [], []
    for name in sorted(found, key=lambda k: (len(found[k]), names.index(k))):
        lst = sorted(found[name], key=lambda c: rank(c, bool(_RX_SIGNED.search(str(name)))))
        pick = None
        for c in lst:
            span = range(c["offset"], c["offset"] + np.dtype(c["dtype"]).itemsize)
            if all(b not in claimed for b in span) or any(
                    (o2["offset"], o2["dtype"]) == (c["offset"], c["dtype"]) for o2 in chosen.values()):
                pick = c
                break
        pick = pick or lst[0]
        if pick["offset"] >= p:            # 다음 기록 앞부분에서 찾은 칸은 쓰지 않음
            continue
        dt = pick["dtype"]
        endian = dt[0] if dt[0] in "<>" else ""
        alt = [c for c in lst if c["offset"] == pick["offset"] and c["dtype"][-1] == dt[-1]
               and c["dtype"][-2] in "ui" and c["dtype"][-2] != dt[-2]
               and (c["dtype"][0] if c["dtype"][0] in "<>" else "") == endian]
        if alt and dt[-2] in "ui":
            sign_unsure.append(name)
        for b in range(pick["offset"], pick["offset"] + np.dtype(dt).itemsize):
            claimed[b] = name
        chosen[name] = pick
    # 1바이트로 잡힌 칸 옆 바이트가 아무 열에도 안 쓰이고 늘 0 이면 2바이트 값의 상위 바이트로 봄
    Es = R[jj]
    for name, c in chosen.items():
        if c["dtype"] != "u1":
            continue
        o = c["offset"]
        nb = o + 1 if major == "<" else o - 1
        if 0 <= nb < p and nb not in claimed and not Es[:, nb].any():
            c.update(offset=min(o, nb), dtype=f"{major}u2")
            claimed[nb] = name
            width_unsure.append(name)

    # ── 시각 ──
    t = _find_time(Es[:, :p], S[rr], kind, tz_local, p)
    Sv = S[np.isfinite(S)]
    dS = np.diff(Sv)
    step = float(np.median(dS)) if len(dS) else 1.0
    if t is None:
        regular = bool(len(dS)) and np.mean(np.abs(dS - step) <= max(0.002, 0.01 * step)) >= 0.99
        if not regular:
            res["why"] = "시각이 저장된 위치를 찾지 못했고, CSV 시각 간격도 일정하지 않음"
            return res
        t = {"kind": "index", "interval_s": round(step / k_st, 6)}
    if t["kind"] in ("counter", "index") and kind in ("clock", "datetime"):
        # 기록에는 경과 시간(또는 아무것도) 없음 → 헤더에서 시작 시각을 찾아 시계 시각으로
        if t["kind"] == "counter":
            raw0 = _field(R[:1], t["offset"], t["dtype"]).astype(float)[0]
            rawk = _field(R[lo_r + d0:lo_r + d0 + 1], t["offset"], t["dtype"]).astype(float)[0]
            t0 = S[lo_r] - (rawk - raw0) * t["unit_s"]
        else:
            t0 = S[lo_r] - (step / k_st) * (lo_r * k_st + d0)
        hs = _header_start_time(buf[:start], float(np.mod(t0, 86400.0)), tz_local)
        if hs:
            t["start_from"] = {k: v for k, v in hs.items() if k != "unix"}
            note(f"시각: 헤더 {hs['offset']}바이트 위치의 시작 시각({hs['kind']}) + "
                 + (f"기록 안 경과 카운터({t['dtype']}, {t['unit_s']:g}초 단위)" if t["kind"] == "counter"
                    else f"{step:g}초 간격"))
        else:
            note("⚠️ 시계 시각을 못 찾음 — 경과 시간(0초부터)으로만 읽힘 → BR 등 다른 소스와 시간을 맞출 수 없음")
    elif t["kind"] == "index":
        note(f"기록마다 시각이 없음 → {step:g}초 간격 경과 시간으로 읽힘")
    elif t["kind"] == "counter":
        note(f"시각: 기록 안 {t['offset']}바이트 위치의 경과 카운터({t['dtype']}, {t['unit_s']:g}초 단위)")
    else:
        note(f"시각: {t['kind']} 형식, 기록 안 {t['offset']}바이트 위치"
             + (f" (UTC → 현지 {t['tz'] / 3600:+g}시간)" if t.get("tz") else ""))

    # ── 상태 열(글자): 코드 ↔ 이름 1:1 ──
    codes = []
    for name, arr in txt.items():
        lab = arr[rr]
        ok = np.array([x is not None for x in lab])
        if ok.sum() < 20:
            continue
        L = pd.Series(lab[ok]).astype(str)
        lf, luniq = pd.factorize(L)
        if len(luniq) < 2:
            continue
        best = None
        for dt in ("u1", "<u2", ">u2"):
            F, meta = _fields(Es[ok][:, :p], [dt])
            for k in range(F.shape[1]):
                code = F[:, k].astype(np.int64)
                nc = len(np.unique(code))
                if nc != len(luniq):
                    continue
                pair = np.unique(code * (len(luniq) + 1) + lf)
                if len(pair) != nc:
                    continue
                o = meta[k][0]
                score = (any(b in claimed for b in range(o, o + np.dtype(dt).itemsize)), dt != "u1")
                if best is None or score < best[0]:
                    mp = {}
                    for cv, lv in zip(code, L):
                        mp.setdefault(int(cv), lv)
                    best = (score, {"name": name, "offset": int(o), "dtype": dt, "map": {str(k2): v for k2, v in mp.items()}})
        if best:
            codes.append(best[1])
            for b in range(best[1]["offset"], best[1]["offset"] + np.dtype(best[1]["dtype"]).itemsize):
                claimed.setdefault(b, name)

    # ── 동기 바이트: 어느 칸에도 안 쓰였고 늘 같은 값인 바이트 (프레임 표시 등) ──
    used = set(claimed)
    if "offset" in t and t["kind"] not in ("index",):
        used |= set(range(t["offset"], t["offset"] + {"systemtime": 16, "hms_u1": 3}.get(
            t["kind"], np.dtype(t.get("dtype", "u1")).itemsize)))
    sync = [(j, v) for j, v in constant_bytes(_blocks(a, start, p)) if j not in used][:8]

    # ── 형식 정보 + 전체 검증 ──
    spec = {
        "format": SPEC_VERSION, "record_len": int(p), "header_len": int(start),
        "magic_hex": buf[:min(8, start)].hex() if start >= 4 else "",
        "sync": [[int(j), int(v)] for j, v in sync],
        "time_col": tcol, "time": t,
        "columns": [{"name": n, "offset": int(c["offset"]), "dtype": c["dtype"], "a": c["a"], "b": c["b"]}
                    for n, c in chosen.items()],
        "codes": codes,
        "csv_columns": [str(c) for c in df.columns],
        "learned": {"at": _dt.datetime.now().isoformat(timespec="seconds"), "rows": M, "records": int(len(R)),
                    "delta": int(d0), "stride": int(k_st), "sign_unsure": sign_unsure, "width_unsure": width_unsure},
    }
    # ── 전체 행으로 다듬기: 표본(600행)에서는 같아 보였던 후보(± 부호·폭) 중 전체와 가장 잘 맞는 것 ──
    Rall = _blocks(a, start, p)
    i0 = 0
    while i0 * k_st + d0 < 0:
        i0 += 1
    i1 = min(M, (len(Rall) - 1 - d0) // k_st + 1)
    jall = np.arange(i0, i1) * k_st + d0
    refined = []
    for col in spec["columns"]:
        name = col["name"]
        y = num[name][i0:i1]
        okm = np.isfinite(y)
        dec_ = _decimals(y[okm])
        tol = (0.5 * 10.0 ** (-dec_) + 1e-9) if dec_ > 0 else 1e-6

        def agree(c):
            if c["offset"] + np.dtype(c["dtype"]).itemsize > p:
                return -1.0
            v = c["a"] * _field(Rall[jall], c["offset"], c["dtype"]).astype(np.float64) + c["b"]
            return float(np.mean(np.abs(v[okm] - y[okm]) <= tol))
        cur = agree(col)
        if cur < 0.9999:
            best = max(found.get(name, []), key=agree, default=None)
            if best is not None and agree(best) > cur:
                col.update(offset=int(best["offset"]), dtype=best["dtype"], a=best["a"], b=best["b"])
                refined.append(name)
    if refined:
        note(f"전체 행 비교로 칸 형식을 다시 고른 열 {len(refined)}개 (표본에 드물게 나오는 값 — 무효값 65535 등)")
    dec = decode(buf, spec)
    spec["ranges"] = {}
    for c in spec["columns"]:
        v = pd.to_numeric(dec[c["name"]], errors="coerce")
        if v.notna().any():
            spec["ranges"][c["name"]] = [float(v.min()), float(v.max())]
    ver = verify(dec, df, d0, spec, k_st)
    res.update(spec=spec, verify=ver, delta=d0, start=start)
    mapped = set(chosen) | {c["name"] for c in codes}
    res["unmapped"] = [c for c in df.columns if c != tcol and c not in mapped]
    const_cols = [c for c in res["unmapped"] if c in num and np.nanstd(num[c]) == 0]
    res["unmapped_const"] = const_cols
    n_var = len([c for c in names if np.nanstd(num[c]) > 0]) + len([c for c in txt if len(set(txt[c]) - {None}) > 1])
    res["coverage"] = (len(mapped) / n_var) if n_var else 0.0
    good = ver["time_ok"] >= 0.99 and ver["overall"] >= 0.995 and res["coverage"] >= 0.8
    res["ok"] = bool(good)
    if not good:
        why = []
        if ver["time_ok"] < 0.99:
            why.append(f"시각 일치 {ver['time_ok'] * 100:.1f}%")
        if ver["overall"] < 0.995:
            why.append(f"값 일치 {ver['overall'] * 100:.2f}%")
        if res["coverage"] < 0.8:
            why.append(f"값이 변하는 열 중 위치를 찾은 비율 {res['coverage'] * 100:.0f}%")
        res["why"] = "검증 기준 미달 — " + ", ".join(why)
    return res


# ══════════════════════════════════════════════
#  6. 읽기·검증
# ══════════════════════════════════════════════
def _decode_records(R: np.ndarray, spec: dict, head: bytes | None = None) -> dict:
    n = len(R)
    out = {spec["time_col"]: _decode_time(R, spec["time"], n, head).to_numpy()}
    for c in spec["columns"]:
        raw = _field(R, c["offset"], c["dtype"])
        if c["a"] == 1 and c["b"] == 0 and c["dtype"][-2] in "ui":
            out[c["name"]] = raw.astype(np.int64)
        else:
            out[c["name"]] = np.round(c["a"] * raw.astype(np.float64) + c["b"], 9)
    for c in spec["codes"]:
        raw = _field(R, c["offset"], c["dtype"]).astype(np.int64)
        mp = {int(k): v for k, v in c["map"].items()}
        out[c["name"]] = np.array([mp.get(int(x), str(int(x))) for x in raw], dtype=object)
    return out


def decode(buf: bytes, spec: dict, start: int | None = None, nrows: int | None = None) -> pd.DataFrame:
    """형식 정보대로 .dat → DataFrame (CSV 와 같은 열 이름·순서, 위치를 모르는 열은 없음)."""
    a = np.frombuffer(buf, np.uint8)
    p = int(spec["record_len"])
    h = int(spec["header_len"] if start is None else start)
    n = max(0, (len(a) - h) // p)
    if nrows:
        n = min(n, int(nrows))
    df = pd.DataFrame(_decode_records(a[h:h + n * p].reshape(n, p), spec, bytes(buf[:h])))
    order = [c for c in spec["csv_columns"] if c in df.columns]
    return df[order]


def verify(dec: pd.DataFrame, df: pd.DataFrame, delta: int, spec: dict, stride: int = 1) -> dict:
    """해독 결과와 CSV 를 행마다 비교 (CSV 행 i ↔ 기록 stride·i + δ)."""
    M = len(df)
    i0 = 0
    while i0 * stride + delta < 0:
        i0 += 1
    i1 = min(M, (len(dec) - 1 - delta) // stride + 1)
    if i1 - i0 <= 0:
        return {"overall": 0.0, "time_ok": 0.0, "cols": {}, "rows": 0}
    A = dec.iloc[np.arange(i0, i1) * stride + delta].reset_index(drop=True)
    B = df.iloc[i0:i1].reset_index(drop=True)
    cols, tot, hit = {}, 0, 0
    for c in A.columns:
        if c == spec["time_col"]:
            continue
        x, y = A[c], B[c]
        yn = pd.to_numeric(y, errors="coerce")
        if pd.api.types.is_numeric_dtype(x) and yn.notna().mean() > 0.98:
            dec_ = _decimals(yn.to_numpy(float))
            tol = (0.5 * 10.0 ** (-dec_) + 1e-9) if dec_ > 0 else 1e-6
            m = np.abs(x.to_numpy(float) - yn.to_numpy(float)) <= tol
            m |= yn.isna().to_numpy() & x.isna().to_numpy()
        else:
            m = (x.astype(str).str.strip() == y.astype(str).str.strip()).to_numpy()
        cols[c] = float(m.mean())
        tot += len(m)
        hit += int(m.sum())
    # 시각: 하루 중 초로 비교 (CSV 가 소수점 이하를 버렸을 수 있어 1초 허용)
    Sx, kx, _ = csv_time_seconds(A[spec["time_col"]].astype(str))
    Sy, ky, _ = csv_time_seconds(B[spec["time_col"]].astype(str))
    if spec["time"]["kind"] in ("counter", "index") and not spec["time"].get("start_from"):
        Sx = Sx - Sx[0] + Sy[0]
    t_ok = _close_tod(np.nan_to_num(Sx), np.nan_to_num(Sy), tol=1.0) if len(Sx) else 0.0
    return {"overall": hit / tot if tot else 1.0, "time_ok": t_ok, "cols": cols, "rows": i1 - i0}


# ══════════════════════════════════════════════
#  7. 형식 정보 저장·찾기
# ══════════════════════════════════════════════
def save_spec(spec: dict, folder: Path = FORMAT_DIR) -> Path:
    """저장. 같은 기록 길이·헤더 시작 바이트의 형식 정보가 있으면 열을 합친다(새 학습 우선)."""
    folder.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(f"{spec['record_len']}|{spec['magic_hex']}".encode()).hexdigest()[:6]
    path = folder / f"ams_{spec['record_len']}B_{key}.json"
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if old.get("record_len") == spec["record_len"]:
                unsure_new = set(spec["learned"].get("sign_unsure", [])) | set(spec["learned"].get("width_unsure", []))
                unsure_old = set(old.get("learned", {}).get("sign_unsure", [])) | set(old.get("learned", {}).get("width_unsure", []))
                oldc = {c["name"]: c for c in old.get("columns", [])}
                spec["columns"] = [oldc[c["name"]] if (c["name"] in oldc and c["name"] in unsure_new
                                                         and c["name"] not in unsure_old) else c
                                   for c in spec["columns"]]
                have = {c["name"] for c in spec["columns"]}
                spec["columns"] += [c for c in old.get("columns", []) if c["name"] not in have]
                for k in ("ranges",):
                    for nm, rg in (old.get(k) or {}).items():
                        cur = spec.setdefault(k, {}).get(nm)
                        spec[k][nm] = [min(rg[0], cur[0]), max(rg[1], cur[1])] if cur else rg
                have_c = {c["name"]: c for c in spec["codes"]}
                for c in old.get("codes", []):
                    if c["name"] in have_c:
                        merged = dict(c["map"])
                        merged.update(have_c[c["name"]]["map"])
                        have_c[c["name"]]["map"] = merged
                    else:
                        spec["codes"].append(c)
                spec["csv_columns"] += [c for c in old.get("csv_columns", []) if c not in spec["csv_columns"]]
        except Exception:
            pass
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


_CACHE: dict = {}


def load_specs(folder: Path = FORMAT_DIR) -> list:
    if not folder.is_dir():
        return []
    out = []
    for f in sorted(folder.glob("*.json")):
        try:
            mt = f.stat().st_mtime
            if _CACHE.get(f) and _CACHE[f][0] == mt:
                out.append(_CACHE[f][1])
                continue
            sp = json.loads(f.read_text(encoding="utf-8"))
            if sp.get("format") == SPEC_VERSION:
                sp["_file"] = f.name
                _CACHE[f] = (mt, sp)
                out.append(sp)
        except Exception:
            continue
    return out


def _score(buf: bytes, spec: dict, start: int) -> float:
    """이 위치부터 형식 정보대로 읽었을 때 얼마나 그럴듯한지 (0~1, 안 되면 -1).

    시각(절대 시각이면 범위·증가)은 필수, 그 밖에 학습 때 값 범위(여유 50%)·상태 코드표·
    동기 바이트가 맞는 비율의 평균. 다른 장비의 바이너리를 잘못 읽지 않게 하는 확인."""
    a = np.frombuffer(buf, np.uint8)
    p = spec["record_len"]
    n = (len(a) - start) // p
    if n < 2:
        return -1.0
    # 앞 5% 는 건너뛰고 고르게 (위상만 맞으면 헤더 길이와 상관없이 같은 점수)
    idx = np.unique(np.linspace(int(0.05 * n), n - 1, min(n, 40)).astype(int))
    R = np.stack([a[start + k * p: start + (k + 1) * p] for k in idx])
    t = spec["time"]
    if t["kind"] in ("filetime", "ticks", "unix_s", "unix_ms", "unix_us", "ole"):
        with np.errstate(all="ignore"):
            u = _abs_time(_field(R, t["offset"], t["dtype"]), t["kind"])
        ok = np.isfinite(u) & (u > _Y2000) & (u < _Y2100)
        if ok.mean() < 0.9 or np.mean(np.diff(u[ok]) >= 0) < 0.9:
            return -1.0
    elif t["kind"] == "systemtime":
        v = np.ascontiguousarray(R[:, t["offset"]:t["offset"] + 16]).view(np.dtype(t["dtype"])).reshape(len(R), 8)
        ok = ((v[:, 0] >= 2000) & (v[:, 0] <= 2100) & (v[:, 1] >= 1) & (v[:, 1] <= 12)
              & (v[:, 4] <= 23) & (v[:, 5] <= 59))
        if ok.mean() < 0.9:
            return -1.0
    elif t["kind"] == "counter":
        raw = _field(R, t["offset"], t["dtype"]).astype(float)
        if np.mean(np.diff(raw) >= 0) < 0.8:
            return -1.0
    parts = []
    rg = spec.get("ranges") or {}
    for c in spec["columns"]:
        if c["name"] not in rg:
            continue
        lo, hi = rg[c["name"]]
        pad = 0.5 * (hi - lo) + 1.0
        v = c["a"] * _field(R, c["offset"], c["dtype"]).astype(np.float64) + c["b"]
        parts.append(float(np.mean((v >= lo - pad) & (v <= hi + pad))))
    for c in spec.get("codes", []):
        raw = _field(R, c["offset"], c["dtype"]).astype(np.int64)
        parts.append(float(np.mean([str(int(x)) in c["map"] for x in raw])))
    if spec.get("sync"):
        parts.append(float(np.mean([np.mean([R[r, j] == v for j, v in spec["sync"]]) for r in range(len(R))])))
    return float(np.mean(parts)) if parts else 0.5


def find_spec(path, specs: list | None = None, head: bytes | None = None, size: int | None = None):
    """이 .dat 에 맞는 학습된 형식 → (형식 정보, 기록 시작 위치) | (None, None).

    헤더 앞 8바이트가 같은 형식 정보를 먼저 시도하고, 동기 바이트·시각 증가로 그럴듯한지 확인한다.
    head 에 파일 앞부분만 줄 때는 size 에 실제 파일 크기를 준다 (기록 위상 = 크기 % 기록 길이)."""
    specs = load_specs() if specs is None else specs
    if not specs:
        return None, None
    buf = head if head is not None else read_bytes(path)
    size = len(buf) if size is None else int(size)
    specs = sorted(specs, key=lambda sp: not (sp.get("magic_hex") and buf[:len(sp["magic_hex"]) // 2].hex() == sp["magic_hex"]))
    for sp in specs:
        p, h = sp["record_len"], sp["header_len"]
        best = (0.0, None)
        # ① 위상(기록 경계) 고르기 — 학습 때 헤더 길이의 위상 → (크기 % 기록 길이) → 나머지 위상
        for ph in dict.fromkeys([h % p, size % p] + list(range(p))):
            if ph >= len(buf) - 2 * p:
                continue
            sc = _score(buf, sp, ph)
            if sc > best[0] + 1e-9:
                best = (sc, ph)
            if sc >= 0.999:
                break
        if best[1] is None or best[0] < 0.9:
            continue
        # ② 그 위상에서 기록이 시작되는 가장 앞 위치 (연속 3개가 기록다운 곳)
        a = np.frombuffer(buf, np.uint8)
        s1, lim = best[1], min(len(a) - 3 * p, max(h, 0) + (1 << 18))
        while s1 <= lim:
            if all(_rec_ok(a[s1 + k * p: s1 + (k + 1) * p], sp) for k in range(3)):
                return sp, s1
            s1 += p
    return None, None


def _rec_ok(rec: np.ndarray, spec: dict) -> bool:
    """기록 하나가 형식 정보대로 그럴듯한지 (헤더 바이트·0 으로 채운 영역이 아닌지)."""
    if len(np.unique(rec)) < 3:
        return False
    R = rec.reshape(1, -1)
    t = spec["time"]
    if t["kind"] in ("filetime", "ticks", "unix_s", "unix_ms", "unix_us", "ole"):
        with np.errstate(all="ignore"):
            u = _abs_time(_field(R, t["offset"], t["dtype"]), t["kind"])
        if not (np.isfinite(u).all() and _Y2000 < u[0] < _Y2100):
            return False
    elif t["kind"] == "systemtime":
        v = np.frombuffer(rec[t["offset"]:t["offset"] + 16].tobytes(), np.dtype(t["dtype"]))
        if not (2000 <= v[0] <= 2100 and 1 <= v[1] <= 12 and v[4] <= 23 and v[5] <= 59 and v[6] <= 59):
            return False
    if spec.get("sync") and np.mean([rec[j] == v for j, v in spec["sync"]]) < 0.9:
        return False
    for c in spec.get("codes", []):
        if str(int(_field(R, c["offset"], c["dtype"])[0])) not in c["map"]:
            return False
    rg = spec.get("ranges") or {}
    ins = []
    for c in spec["columns"]:
        if c["name"] in rg:
            lo, hi = rg[c["name"]]
            pad = 0.5 * (hi - lo) + 1.0
            v = c["a"] * float(_field(R, c["offset"], c["dtype"])[0]) + c["b"]
            ins.append(lo - pad <= v <= hi + pad)
    return (np.mean(ins) >= 0.6) if ins else True


def read_dat(path, nrows: int | None = None, log=print) -> pd.DataFrame:
    """학습된 형식으로 AMS .dat 읽기. 맞는 형식이 없으면 빈 DataFrame."""
    buf = read_bytes(path)
    sp, start = find_spec(path, head=buf)
    name = Path(path).name
    if sp is None:
        log(f"  [ams_dat] {name}: ⚠️ 학습된 AMS .dat 형식과 맞지 않음 — 케이스 확정 후 '파일 선택'에서 "
            f"같은 측정을 AMS 에서 내보낸 CSV 를 골라 [형식 학습] (또는 diagnose.bat)")
        return pd.DataFrame()
    df = decode(buf, sp, start, nrows)
    miss = [c for c in sp["csv_columns"] if c not in df.columns]
    log(f"  [ams_dat] {name}: AMS 바이너리 → 학습한 형식 '{sp.get('_file', '')}'로 읽음 "
        f"({len(df):,}행 × {len(df.columns)}열, 기록 {sp['record_len']}바이트"
        f"{f', 위치를 모르는 열 {len(miss)}개 제외' if miss else ''})")
    return df
