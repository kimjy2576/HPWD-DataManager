"""
ams_proj.py — AMS 프로그램 .dat 를 같은 폴더의 프로젝트 파일(.proj)로 바로 읽기 (형식 학습 불필요)

hpwd-studio-task 이슈 #13 의 변환 코드(AMS_Data.dat + *.proj → AMS_Data.csv)와 같은 방식이다.

  · .proj (XML) 의 <Channel Type= Name= Number=> 순서대로 기록 안 위치가 정해진다.
      기록 = [시각 8바이트 double LE][채널 …][끝 1바이트]
      - 일반 계측 채널(전력계·유량계·온도 등) = 4바이트 float32 LE
      - STX ~ ETX = 제품 통신(스마트) 패킷 — 패킷 길이 = ETX 의 Number.
        그 사이 채널(스마트 패킷 안 항목)은 위치 계산에서 빠진다.
  · 시각: Unix 초(double). 현지 시각을 UTC 처럼 저장한 값이라 UTC 로 풀면 현지 시각
    (원 코드: fromtimestamp() − 9시간 = 한국 시간 PC 에서 UTC 로 푼 것과 같음 — 여기선 PC 시간대와 무관)
  · 스마트 패킷 값: 원 코드의 바이트 위치·비트 그대로. 패킷 종류([3])마다 바뀌는 값은 직전 값을 유지.
  · 원 코드처럼 처음 11개 기록은 버린다 (패킷 종류별 값이 한 번씩 채워지기 전).

원 코드와 다른 점 (값은 같고 표시만):
  · Time 에 날짜까지 넣음 ('2026-11-20 10:00:00.123') — BR 등과 날짜까지 맞춰 병합
  · 'FanSpeed' → 'FanSpeed[/100]', 'SuperHeatDegree' → 'SuperHeatDegree[*10]'
    (값은 원 코드와 같음. AMS 프로그램 CSV 와 같은 배율 표기라 앱의 AMS 배율 설정이 그대로 적용됨)
  · 계측 채널 값이 NaN/inf 면 원 코드는 0, 여기선 빈 값
  · 채널 이름의 한자(功率·耗电 등)는 '_' 로 바꾸지 않고 둠 (앱의 AMS 이름 매핑이 그대로 적용)
  · 원 코드 48개 외에 EXTRA_SMART(Heatpump_Comp Current)를 더 읽음. 위치는
      ① .proj 패킷 채널 정의의 숫자 값(속성·자식 요소, 10진·16진, 바이트·비트·마지막 바이트 위치) —
         원 코드 바이트 위치와 맞아 위치 정보로 확인될 때만
      ② 같은 폴더에 같은 측정의 AMS CSV(AMS 프로그램 변환)가 있으면 시각으로 짝지어 패킷에서 같은 값을 찾음 —
         찾은 위치는 .proj 내용별로 config/ams_dat_formats/proj_learned.json 에 기억 (같은 .proj 면 CSV 없이도)
    둘 다 안 되면 이유를 로그에 (+ 📋 .proj 채널 창에 그 채널의 .proj 정의 XML)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pandas as pd

SKIP_FIRST = 11          # 원 코드: cnt > 10 부터 기록
_TS_WIN_MAX = 32536799999.0       # Windows 에서 datetime.fromtimestamp() 가 되는 최대값 (원 코드가 읽히는 범위)
_LAYOUT_CACHE: dict = {}

# 원 코드의 계측 채널 이름 정리 규칙 (위에서부터 차례로)
_RENAME = [
    (lambda s: "power" in s.lower(), "Active Power"),
    (lambda s: "watt" in s.lower(), "Watt_Hour"),
    (lambda s: "cold" in s.lower() and "flow" in s.lower(), "cold_water"),
    (lambda s: "cold" in s.lower() and "folw" in s.lower(), "cold_water"),
    (lambda s: "소비전력" in s, "Watt_Hour"),
    (lambda s: "전력량" in s, "Watt_Hour"),
    (lambda s: "전력" in s, "Active Power"),
    (lambda s: "냉수" in s, "cold_water"),
    (lambda s: "냉수유량" in s, "cold_water"),
    (lambda s: "hot" in s.lower() and "folw" in s.lower(), "hot_water"),
    (lambda s: "hot" in s.lower() and "flow" in s.lower(), "hot_water"),
    (lambda s: "rpm" in s.lower(), "RPM"),
]
# 원 코드: [^a-zA-Z0-9_ㄱ-ㅎㅏ-ㅣ가-힣\s] → '_'  (+ 한자는 그대로 둠)
_RX_BAD = re.compile(r"[^a-zA-Z0-9_ㄱ-ㅎㅏ-ㅣ가-힣一-鿿\s]")


# ──────────────────────────────────────────────
#  .proj 찾기·읽기
# ──────────────────────────────────────────────
def find_proj(dat_path) -> list:
    """.dat 와 같은 폴더의 프로젝트 파일 후보 (이름에 '.proj' — 원 코드와 같음). 같은 이름·최근 것 먼저."""
    p = Path(dat_path)
    try:
        cands = [q for q in p.parent.iterdir() if q.is_file() and ".proj" in q.name.lower()]
    except OSError:
        return []

    def key(q):
        try:
            mt = q.stat().st_mtime
        except OSError:
            mt = 0
        return (q.stem.lower() != p.stem.lower(), -mt)
    return sorted(cands, key=key)


def _xml_root(path: Path):
    raw = Path(path).read_bytes()
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        pass
    for enc in ("utf-8-sig", "cp949", "utf-16"):
        try:
            txt = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        txt = re.sub(r"^\s*<\?xml[^>]*\?>", "", txt)
        try:
            return ET.fromstring(txt)
        except ET.ParseError:
            continue
    raise ValueError("XML 로 읽을 수 없음")


def _num(v):
    """숫자 글자 → 정수 (10진, 16진 '0x1D'·'1Dh'·'&H1D', '51.7' 같은 바이트.비트는 앞 숫자). 아니면 None."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError, OverflowError):
        pass
    m = re.fullmatch(r"(?:0x|&h|\$)([0-9a-f]+)|([0-9a-f]+)h", s, re.I)
    if m:
        return int(m.group(1) or m.group(2), 16)
    return None


def parse_proj(path) -> dict:
    """.proj → 기록 배치 {rec, stx, etx, chans: [(열 이름, 시작, 끝)], proj}.  (원 코드 parsing() 앞부분)"""
    root = _xml_root(path)
    chans = [(el.get("Type"), el.get("Name"), el.get("Number"))
             for el in root.iter() if str(el.tag).split("}")[-1] == "Channel"]
    if not chans:
        raise ValueError("<Channel> 항목이 없음")
    rows, in_smart = [], False
    for typ, name, num in chans:
        name = "" if name is None else str(name)
        if name == "STX":
            rows.append({"name": name, "num": num, "kind": "stx"}); in_smart = True
        elif name == "ETX":
            rows.append({"name": name, "num": num, "kind": "etx"}); in_smart = False
        elif not in_smart:
            rows.append({"name": name, "num": num, "kind": "normal"})
        # STX~ETX 사이(스마트 패킷 안 항목)는 위치 계산에서 빠짐 (원 코드 use_flag 0)
    for r in rows:
        if r["kind"] == "stx":
            r["cnt"] = 1
        elif r["kind"] == "etx":
            r["cnt"] = _num(r["num"])
            if not r["cnt"] or r["cnt"] < 1:
                raise ValueError(f"ETX 의 Number(패킷 길이)를 읽을 수 없음: {r['num']!r}")
        else:
            r["cnt"] = 4
    stx_start = None
    for i, r in enumerate(rows):
        if i == 0:
            r["start"] = 8
        elif r["kind"] == "etx":
            if stx_start is None:
                raise ValueError("ETX 앞에 STX 가 없음")
            r["start"] = stx_start          # 원 코드: ETX 는 STX 위치에서 시작 → 패킷 전체 길이를 차지
        else:
            r["start"] = rows[i - 1]["start"] + rows[i - 1]["cnt"]
        if r["kind"] == "stx":
            stx_start = r["start"]
        r["end"] = r["start"] + r["cnt"]
    # 계측 채널 이름: 원 코드 규칙으로 정리 → 같은 이름은 _1, _2 …
    seen, used = {}, set()
    for r in rows:
        n = r["name"]
        if r["kind"] == "normal":
            for cond, new in _RENAME:
                if cond(n):
                    n = new
            n = _RX_BAD.sub("_", n).strip() or f"CH{r['num']}"
        if n not in seen:
            seen[n] = 0
            col = n
        else:
            seen[n] += 1
            col = f"{n}_{seen[n]}"
        while col in used:
            col += "_"
        used.add(col)
        r["col"] = col
    stx = next((r for r in rows if r["kind"] == "stx"), None)
    etx = next((r for r in rows if r["kind"] == "etx"), None)
    return {
        "proj": str(path),
        "rec": int(max(r["end"] for r in rows) + 1),              # 원 코드: array_cnt = 끝 위치 최대 + 1
        "stx": int(stx["start"]) if stx and etx else None,
        "etx": int(etx["end"] + 1) if stx and etx else None,       # 원 코드: etx_no = ETX 끝 + 1
        "chans": [(r["col"], int(r["start"]), int(r["end"])) for r in rows if r["kind"] == "normal"],
        "n_smart_items": sum(1 for _ in chans) - len(rows),
    }


# ──────────────────────────────────────────────
#  배치 확인 (.dat 와 맞는지)
# ──────────────────────────────────────────────
_SMALL_FILE = 8 << 20     # 이보다 작은 파일은 한 번에 읽어 메모리에서 자름 (네트워크 드라이브에서 여러 번 seek 하지 않게)
_HEAD_CACHE: dict = {}


def _read_records(path, rec: int, idx) -> np.ndarray:
    """기록 번호 idx 들의 바이트 (k, rec) — 필요한 부분만 읽음."""
    out = np.zeros((len(idx), rec), np.uint8)
    try:
        stt = Path(path).stat()
    except OSError:
        return out
    if stt.st_size <= _SMALL_FILE:
        key = (str(path), stt.st_size, stt.st_mtime)
        buf = _HEAD_CACHE.get(key)
        if buf is None:
            buf = Path(path).read_bytes()
            _HEAD_CACHE.clear()
            _HEAD_CACHE[key] = buf
        a = np.frombuffer(buf, np.uint8)
        for j, i in enumerate(idx):
            s = int(i) * rec
            if s + rec <= len(a):
                out[j] = a[s:s + rec]
        return out
    with open(path, "rb") as f:
        for j, i in enumerate(idx):
            f.seek(int(i) * rec)
            b = f.read(rec)
            if len(b) == rec:
                out[j] = np.frombuffer(b, np.uint8)
    return out


def _ts_ok(ts: np.ndarray) -> np.ndarray:
    """시각으로 쓸 수 있는 값 — 원 코드 datetime.fromtimestamp() 가 되는 범위 (0 < 초 < Windows 한계).
    0 으로 채운 기록(파일 끝에 미리 잡아 둔 자리 등)은 여기서 걸러짐."""
    return np.isfinite(ts) & (ts > 1.0) & (ts < _TS_WIN_MAX)


def _check(path, size: int, rec: int, stx) -> dict:
    """이 기록 길이로 읽었을 때 시각 열이 시각처럼 보이는지 — 원 코드는 아무 확인 없이 읽으므로
    '틀린 배치를 거르는' 정도로만 느슨하게 본다 (0 으로 채운 기록·다른 기준일·바뀌는 STX 바이트는 허용)."""
    n = size // rec
    out = {"rec": rec, "n": n, "rem": size - n * rec, "valid": 0.0, "mono": 0.0, "stx": None, "step": None,
           "ok": False, "score": 0.0}
    if n < 1:
        return out
    idx = np.unique(np.linspace(0, n - 1, min(n, 160)).astype(np.int64))
    R = _read_records(path, rec, idx)
    ts = np.ascontiguousarray(R[:, :8]).view("<f8").ravel()
    good = _ts_ok(ts)
    out["valid"] = float(good.mean())
    v, iv = ts[good], idx[good]
    out["t_med"] = float(np.median(v)) if len(v) else None
    if len(v) >= 2:
        out["mono"] = float((np.diff(v) >= -1.0).mean())
        span_rec = float(iv[-1] - iv[0])
        out["step"] = float((v[-1] - v[0]) / span_rec) if span_rec > 0 else None   # 기록 하나당 초
    elif len(v) == 1 and n == 1:
        out["mono"] = 1.0
    if stx is not None and stx < rec and good.any():
        b = R[good, stx]
        out["stx"] = float(np.bincount(b).max() / len(b))
    step_ok = out["step"] is None or 1e-4 <= out["step"] <= 600
    # 절반 넘는 기록에 시각 값이 있고, 그 시각이 대부분 늘어나고, 기록 간격이 그럴듯하면 맞는 배치
    out["ok"] = out["valid"] >= 0.5 and out["mono"] >= 0.9 and step_ok
    out["score"] = (out["valid"] + out["mono"] + 0.5 * (out["stx"] or 0) + (0.3 if out["rem"] == 0 else 0)
                    + (0.2 if step_ok else 0))
    return out


def _check_text(c: dict) -> str:
    s = (f"기록 {c['rec']}바이트로 읽으면 시각 값 있음 {c['valid']*100:.0f}% · 증가 {c['mono']*100:.0f}%"
         + (f" · 기록 간격 {c['step']:.3g}초" if c.get("step") else "")
         + (f" · STX 바이트 일정 {c['stx']*100:.0f}%" if c.get("stx") is not None else "")
         + (f" · 파일 끝 {c['rem']}바이트 남음" if c.get("rem") else ""))
    return s


# 시각 값의 기준일 후보 — 원 코드는 시:분:초만 쓰므로 기준일이 달라도 (하루 단위 차이라) 시각은 같음.
# 날짜까지 넣기 위해 2000~2046 년이 되는 기준일을 고름. 못 고르면 원 코드처럼 시각만.
_EPOCHS = (("Unix 1970", 0.0), ("1904 (LabVIEW·Mac)", -2082844800.0), ("1900", -2208988800.0),
           ("1601 (Windows)", -11644473600.0))


def _pick_epoch(t: float):
    for name, off in _EPOCHS:
        if 9.46e8 <= t + off <= 2.4e9:
            return name, off
    return None, None


def layout_for(dat_path, why: list | None = None) -> dict | None:
    """.dat 에 맞는 .proj 배치 → 배치 dict | None (why 에 이유).

    원 코드처럼 .proj 로 계산한 기록 길이로 읽는다. .proj 가 여럿이거나 AMS 버전에 따라 끝 바이트가 없을 수 있어
    후보(.proj × 기록 길이 ±1)를 _check 로 견줘 가장 그럴듯한 것을 고르고,
    시각 확인이 안 돼도 .proj 계산 길이가 파일 크기를 딱 나누면 원 코드처럼 그대로 읽는다."""
    p = Path(dat_path)
    try:
        st = p.stat()
    except OSError as e:
        if why is not None:
            why.append(f"파일을 열 수 없음: {e}")
        return None
    projs = find_proj(p)
    if not projs:
        if why is not None:
            why.append("같은 폴더에 AMS 프로젝트 파일(.proj)이 없음")
        return None
    key = (str(p), st.st_size, st.st_mtime,
           tuple((str(q), q.stat().st_mtime) for q in projs if q.exists()))
    if key in _LAYOUT_CACHE:
        found, reason = _LAYOUT_CACHE[key]
        if found is None and why is not None:
            why.append(reason)
        return found
    cands, notes = [], []
    for q in projs:
        try:
            L = parse_proj(q)
        except Exception as e:
            notes.append(f"{q.name}: 읽을 수 없음 ({e})")
            continue
        for k, rec in enumerate((L["rec"], L["rec"] - 1, L["rec"] + 1)):
            if rec < 9:
                continue
            c = _check(p, st.st_size, rec, L["stx"])
            cands.append((q, L, c, k))
            if k == 0:
                notes.append(f"{q.name}: " + _check_text(c))
                if c["ok"]:
                    break          # .proj 계산 길이로 맞으면 ±1 은 안 봄
    good = [x for x in cands if x[2]["ok"]]
    pick, weak = None, False
    if good:   # .proj 계산 길이 그대로(k=0)를 조금 우대
        pick = max(good, key=lambda x: x[2]["score"] + (0.25 if x[3] == 0 else 0.0))
    else:
        exact = [x for x in cands if x[3] == 0 and x[2]["rem"] == 0 and x[2]["n"] >= 1]
        if exact:
            pick, weak = exact[0], True
    if pick is None:
        found = None
        reason = "프로젝트 파일(.proj)의 채널 배치가 이 .dat 와 맞지 않음 — " + " / ".join(notes[:3])
    else:
        q, L, c, k = pick
        ep_name, ep_off = _pick_epoch(c["t_med"]) if c.get("t_med") is not None else (None, None)
        found = dict(L, rec=c["rec"], rec_proj=L["rec"], check=_check_text(c), weak=weak,
                     epoch=ep_name, epoch_off=ep_off)
        try:       # 원 코드 48개 외 변수(EXTRA_SMART)의 패킷 위치 — 못 구해도 읽기는 그대로
            t = channel_table(q)
            found["sig"], found["extra"] = t["sig"], t["extra"]
        except Exception as e:
            found["extra"] = [{"name": n, "why": f".proj 채널 정의를 해석하지 못함 ({e})"} for n in EXTRA_SMART]
        reason = ""
    _LAYOUT_CACHE[key] = (found, reason)
    if found is None and why is not None:
        why.append(reason)
    return found


# ──────────────────────────────────────────────
#  스마트 패킷 (원 코드 바이트 위치 그대로)
# ──────────────────────────────────────────────
class _Held(np.ndarray):
    """패킷 종류별로 바뀌는(직전 값 유지) 열 표시용."""
    def __new__(cls, a):
        return np.asarray(a).view(cls)


def _smart_columns(P: np.ndarray) -> list:
    """P = 기록별 STX~ETX 패킷 (n, L) uint8  →  [(열 이름, 값 배열)]  — 원 코드 headers 순서."""
    n, L = P.shape
    nan = np.full(n, np.nan)

    def b(i):
        return P[:, i].astype(np.float64) if i < L else nan

    def u16(i):
        return (P[:, i].astype(np.float64) * 256 + P[:, i + 1]) if i + 1 < L else nan

    def bit(i, k):                       # k = 오른쪽(LSB)부터 0,1,2… (원 코드 to_8bit_binary(x)[7-k])
        return ((P[:, i] >> k) & 1).astype(np.float64) if i < L else nan

    def hi4(i):                          # 원 코드 int(to_8bit_binary(x)[0:4], 2)
        return (P[:, i] >> 4).astype(np.float64) if i < L else nan

    typ = P[:, 3] if L > 3 else np.full(n, -1)

    def held(v, t):                      # 패킷 종류 t 일 때만 바뀌고 나머지는 직전 값 유지
        return _Held(pd.Series(np.where(typ == t, v, np.nan)).ffill().to_numpy())

    d13 = bit(13, 0)
    return [
        ("CurrentRpm", u16(9)),
        ("RequestRpm", u16(11)),
        ("motor_direction", np.where(np.isnan(d13), None,
                                     np.where(d13 == 0, "시계반대반향 회전(CCW)", "시계방향 회전(CW)"))),
        ("bleach_valve(atomizing)", bit(50, 2) + 18),
        ("dry_valve", bit(51, 7) + 10),
        ("filterclean_valve", bit(51, 6) + 18),
        ("Eev", u16(69)),
        ("CompTargetHz", b(72)),
        ("CompCurrentHz", b(73)),
        ("FanSpeed[/100]", u16(75) / 100),          # 원 코드 'FanSpeed' (같은 값)
        ("DryFtemp", u16(79)),
        ("SuperHeatDegree[*10]", u16(82)),          # 원 코드 'SuperHeatDegree' (같은 값)
        ("Q Current", u16(116)),
        ("남은 시간", held(u16(160), 1)),
        ("행정 정보1(대 행정 정보)", held(b(164), 1)),
        ("행정 정보2(소 행정 정보)", held(b(165), 1)),
        ("세탁 포량 level", held(hi4(172), 1)),
        ("세탁 포량감지 raw data", held(u16(173), 1)),
        ("탈수 포량 level", held(hi4(172), 2)),
        ("탈수 포량감지 raw data", held(u16(173), 2)),
        ("건조 외기 감지 값", held(hi4(158), 3)),
        ("_DRY_DUCT_IN_TEMP_", held(b(164), 3)),
        ("_DRY_DUCT_OUT_TEMP_", held(b(165), 3)),
        ("_DRY_EVA_IN_TEMP_", held(b(166), 3)),
        ("_DRY_EVA_OUT_TEMP_", held(b(167), 3)),
        ("RenewalTime", held(b(160), 3)),
        ("건조포량 level", held(hi4(172), 3)),
        ("건조포량 raw data", held(u16(173), 3)),
        ("Heatpump_CompTemp", held(b(175), 3)),
        ("Cycle 동작 횟수", held(b(155), 1)),
        ("Cooling Fan", b(71)),
        ("CompIpmTemp", b(74)),
        ("wash_thermistor_temperature", b(46)),
        ("drain_pump_onoff", bit(51, 4) + 2),
        ("water_sensor_frequency", b(52)),
        ("VIB_F_Y", u16(31)),
        ("VIB_R_Y", u16(33)),
        ("고객 선택 코스 정보", held(b(155), 0)),
        ("pre_wash_valve", bit(50, 3) + 16),
        ("steam_thermistor_temperature", b(48)),
        ("steam_valve", bit(50, 1) + 8),
        ("steam_heater", bit(50, 6)),
        ("Steam Short", held(bit(161, 2), 3)),
        ("Steam Long", held(bit(161, 3), 3)),
        ("hot_valve", bit(50, 0) + 14),
        ("cold_wash_valve", bit(50, 4) + 12),
        ("condensorclean valve", bit(110, 1)),
        ("Abs(IqeRef)", u16(29)),
    ]


# ──────────────────────────────────────────────
#  읽기
# ──────────────────────────────────────────────
def _times(ts: np.ndarray, off) -> pd.Series:
    """시각 값 → 글자. 기준일(off: Unix 초로 바꾸는 더하기 값)을 알면 '2026-11-20 10:00:00.123',
    모르면 원 코드처럼 시각만 '10:00:00.123'. 현지 시각을 UTC 처럼 저장한 값이라 UTC 로 풀면 현지 시각."""
    ok = _ts_ok(ts)
    t = np.where(ok, ts, 0.0)
    if off is None:
        ms = np.mod(np.rint(t * 1000.0), 86_400_000).astype(np.int64)
        s = pd.Series(pd.to_datetime(ms, unit="ms").strftime("%H:%M:%S.%f").str[:12], dtype=object)
    else:
        ms = np.rint((t + off) * 1000.0).astype(np.int64)
        ms[~ok] = 0
        s = pd.Series(pd.to_datetime(ms, unit="ms").strftime("%Y-%m-%d %H:%M:%S.%f").str[:23], dtype=object)
    s[~ok] = None
    return s


def read(dat_path, nrows: int | None = None, log=print, layout: dict | None = None):
    """.proj 로 .dat 읽기 → DataFrame (원 코드 CSV 와 같은 열) | 맞는 .proj 가 없으면 None."""
    p = Path(dat_path)
    why = []
    L = layout or layout_for(p, why)
    if L is None:
        return None
    rec = int(L["rec"])
    with open(p, "rb") as f:
        buf = f.read() if nrows is None else f.read((int(nrows) + SKIP_FIRST) * rec)
    n = len(buf) // rec
    if n <= 0:
        return pd.DataFrame()
    A = np.frombuffer(buf, np.uint8, count=n * rec).reshape(n, rec)
    ts = np.ascontiguousarray(A[:, :8]).view("<f8").ravel()
    cols = {"Time": _times(ts, L.get("epoch_off"))}
    held, extra_log = [], []
    if L["stx"] is not None:
        P = A[:, L["stx"]:min(L["etx"], rec)]
        for name, v in _smart_columns(P):
            if isinstance(v, _Held):
                held.append(name)
            cols[name] = np.asarray(v)
        # 원 코드 48개 외 변수 — .proj 정의의 위치 또는 같은 측정 AMS CSV 로 찾은 위치에서
        #   (2바이트는 원 코드처럼 앞 바이트*256 + 뒤 바이트, CSV 로 찾은 것은 그때 맞은 형식·배율)
        if L.get("extra"):
            try:
                ensure_extra(p, L, log)
            except Exception as e:
                extra_log.append(f"⚠️ 같은 측정 AMS CSV 로 위치 찾기 실패 ({type(e).__name__}: {e})")
        normal = {_norm(c) for c, _, _ in L["chans"]}
        for f in L.get("extra") or []:
            if f.get("normal") or f["name"] in cols or _norm(f["name"]) in normal:
                continue                      # 계측 채널로 읽힘 (또는 이미 있는 열)
            if "pos" not in f:
                if "why" in f:
                    more = f" · CSV: {f['why_csv']}" if f.get("why_csv") else (
                        "" if f.get("missing") else " · 같은 측정의 AMS CSV 를 .dat 와 같은 폴더에 두면 위치를 찾아 기억함")
                    extra_log.append(f"{'' if f.get('missing') else '⚠️ '}{f['name']} 못 읽음 — {f['why']}{more}")
                continue
            q0, sz = int(f["pos"]), int(f["size"])
            if q0 + sz > P.shape[1]:
                extra_log.append(f"⚠️ {f['name']} 못 읽음 — 위치 {q0} 가 이 파일의 패킷({P.shape[1]}바이트) 밖")
                continue
            v = P[:, q0].astype(np.float64)
            if sz == 2:
                nx = P[:, q0 + 1].astype(np.float64)
                v = v + nx * 256 if f.get("enc") == "le" else v * 256 + nx
            a = float(f.get("a", 1) or 1)
            if a != 1:
                v = v * a
            cols[f["name"]] = v
            span = f"{q0}~{q0 + 1}" if sz == 2 else f"{q0}"
            extra_log.append(f"{f['name']} 도 읽음 — 패킷 {span} ({f.get('how', '')})")
    for name, s, e in L["chans"]:
        if e > rec:
            continue
        v = np.ascontiguousarray(A[:, s:e]).view("<f4").ravel().astype(np.float64)
        v = np.where(np.isfinite(v), np.trunc(v * 1000) / 1000, np.nan)   # 원 코드 int(val*1000)/1000
        cols[name if name not in cols else name + "_ch"] = v
    df = pd.DataFrame(cols)
    keep = _ts_ok(ts)
    if n > SKIP_FIRST:                       # 원 코드: 처음 11개 기록은 버림
        keep[:SKIP_FIRST] = False
    n_bad = int((~_ts_ok(ts[SKIP_FIRST:] if n > SKIP_FIRST else ts)).sum())
    df = df[keep].reset_index(drop=True)     # 시각 값이 없는 기록(0 으로 채운 자리 등)은 뺌 — 원 코드는 00:00:00 으로 씀
    # 원 코드는 처음 11개 기록 안에 안 나온 패킷 종류가 있으면 멈춤 → 여기선 그 값들만 처음 나온 값으로 앞을 채움
    if held and len(df):
        df[held] = df[held].bfill()
    if nrows is not None:
        df = df.head(int(nrows))
    log(f"  [ams_dat] {p.name}: 프로젝트 파일 {Path(L['proj']).name} 의 채널 배치로 읽음 "
        f"({len(df):,}행 × {len(df.columns)}열, 기록 {rec}바이트"
        f"{', 계측 채널 ' + str(len(L['chans'])) + '개' if L['chans'] else ''}"
        f"{', 스마트 패킷 ' + str(L['etx'] - L['stx']) + '바이트' if L['stx'] is not None else ''}"
        f"{f', 시각 값 없는 기록 {n_bad:,}개 뺌' if n_bad else ''}"
        f"{', 끝 바이트 차이 보정' if rec != L.get('rec_proj', rec) else ''}"
        f"{', 시각 기준일 ' + L['epoch'] if L.get('epoch') and L['epoch'] != 'Unix 1970' else ''}"
        f"{', 날짜를 알 수 없어 시각만' if not L.get('epoch') else ''})")
    for m in extra_log:
        log(f"  [ams_dat] {p.name}: {m}")
    if L.get("weak"):
        log(f"  [ams_dat] {p.name}: ⚠️ 시각 값이 예상과 달라 확인하지 못하고 원 코드처럼 그대로 읽음 — {L.get('check', '')}")
    return df


def _valid_ends(path, rec: int, n: int):
    """시각 값이 있는 처음·마지막 기록의 시각 (원 코드처럼 처음 11개 기록은 건너뜀). 없으면 None."""
    lo = SKIP_FIRST if n > SKIP_FIRST else 0
    idx = np.unique(np.concatenate([np.arange(lo, min(n, lo + 32)),
                                    np.linspace(lo, n - 1, max(1, min(n - lo, 256))).astype(np.int64),
                                    np.arange(max(lo, n - 32), n)]))
    R = _read_records(path, rec, idx)
    ts = np.ascontiguousarray(R[:, :8]).view("<f8").ravel()
    ok = _ts_ok(ts)
    if not ok.any():
        return None
    good = idx[ok]

    def refine(a, b, last):          # a < b 사이 기록을 다 읽어 경계를 정확히
        seg = np.arange(a + 1, b)
        if not len(seg):
            return None
        t2 = np.ascontiguousarray(_read_records(path, rec, seg)[:, :8]).view("<f8").ravel()
        k = np.flatnonzero(_ts_ok(t2))
        return None if not len(k) else (seg[k[-1]], t2[k[-1]]) if last else (seg[k[0]], t2[k[0]])

    i0, t0 = good[0], ts[ok][0]
    prev = idx[idx < i0]
    r = refine(prev[-1], i0, False) if len(prev) else None
    if r:
        i0, t0 = r
    i1, t1 = good[-1], ts[ok][-1]
    nxt = idx[idx > i1]
    r = refine(i1, nxt[0], True) if len(nxt) else None
    if r:
        i1, t1 = r
    return float(t0), float(t1)


def time_bounds(dat_path) -> dict | None:
    """첫·마지막 기록 시각 → {"kind": "abs"|"tod", "start", "end"} (현지 시각 기준 초) | None."""
    p = Path(dat_path)
    L = layout_for(p)
    if L is None:
        return None
    rec = int(L["rec"])
    n = p.stat().st_size // rec
    if n < 1:
        return None
    ends = _valid_ends(p, rec, n)
    if ends is None:
        return None
    if L.get("epoch_off") is not None:
        t0, t1 = (round(x + L["epoch_off"], 3) for x in ends)
        return {"kind": "abs", "start": t0, "end": t1}
    t0, t1 = (round(x, 3) for x in ends)
    s0, s1 = t0 % 86400.0, t1 % 86400.0      # 기준일을 모르면 원 코드처럼 하루 중 시각
    return {"kind": "tod", "start": s0, "end": s1 + (86400.0 if s1 < s0 else 0.0)}


def describe(dat_path) -> str | None:
    """읽을 수 있으면 짧은 설명 ('프로젝트 파일 X.proj'), 아니면 None."""
    L = layout_for(dat_path)
    if not L:
        return None
    return f"프로젝트 파일 {Path(L['proj']).name}" + (" (시각 확인 못 함)" if L.get("weak") else "")


# ──────────────────────────────────────────────
#  프로젝트 파일 채널 정의 보기 (측정값 없음)
# ──────────────────────────────────────────────
# 원 코드가 스마트 패킷에서 꺼내는 값의 바이트 위치 (패킷 시작 STX = 0, 2바이트는 앞 바이트 위치).
# 패킷 종류([3])마다 바뀌는 값은 (위치, 종류).
KNOWN_POS = {
    "currentrpm": 9, "requestrpm": 11, "absiqeref": 29, "vibfy": 31, "vibry": 33,
    "washthermistortemperature": 46, "steamthermistortemperature": 48, "watersensorfrequency": 52,
    "eev": 69, "coolingfan": 71, "comptargethz": 72, "compcurrenthz": 73,
    "compipmtemp": 74, "fanspeed": 75, "dryftemp": 79, "superheatdegree": 82, "qcurrent": 116,
    "고객선택코스정보": (155, 0), "cycle동작횟수": (155, 1), "남은시간": (160, 1),
    "행정정보1대행정정보": (164, 1), "행정정보2소행정정보": (165, 1),
    "세탁포량level": (172, 1), "세탁포량감지rawdata": (173, 1), "탈수포량level": (172, 2), "탈수포량감지rawdata": (173, 2),
    "건조외기감지값": (158, 3), "dryductintemp": (164, 3), "dryductouttemp": (165, 3), "dryevaintemp": (166, 3),
    "dryevaouttemp": (167, 3), "renewaltime": (160, 3), "건조포량level": (172, 3), "건조포량rawdata": (173, 3),
    "heatpumpcomptemp": (175, 3),
}
# 원 코드 값 중 2바이트인 것 (나머지 고정 영역 값은 1바이트)
_KNOWN_2B = {"currentrpm", "requestrpm", "absiqeref", "vibfy", "vibry", "eev", "fanspeed", "dryftemp",
             "superheatdegree", "qcurrent"}


def _known_size(n: str) -> int:
    return 2 if n in _KNOWN_2B else 1


def _norm(name: str) -> str:
    """이름 비교용 — 배율 표기·공백·기호를 빼고 소문자 (한글 유지)."""
    s = re.sub(r"\[[^\]]*\]", "", str(name or "")).lower()
    return re.sub(r"[^0-9a-z가-힣]", "", s)


def _smart_read_names() -> dict:
    """앱이 스마트 패킷에서 읽는 열 {비교용 이름: 열 이름}."""
    P = np.zeros((1, 200), np.uint8)
    return {_norm(n): n for n, _ in _smart_columns(P)}


def _child_fields(el) -> dict:
    """<Channel> 자식 요소의 값 — 위치·크기가 속성이 아니라 자식에 적힌 .proj 용.
    {태그: 글자, 태그.속성: 값}. 같은 태그가 여러 번 나오는 자식(목록·열거값 등)은 뺌."""
    kids = list(el)
    tags = [str(c.tag).split("}")[-1] for c in kids]
    out = {}
    for c, tag in zip(kids, tags):
        if tags.count(tag) > 1:
            continue
        txt = (c.text or "").strip()
        if txt and len(txt) <= 40:
            out[tag] = txt
        for k, v in c.attrib.items():
            out[f"{tag}.{str(k).split('}')[-1]}"] = str(v)
    return out


def _xml_snip(el, limit: int = 900) -> str:
    try:
        s = ET.tostring(el, encoding="unicode")
    except Exception:
        return ""
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[:limit] + " …"


def channel_table(proj_path) -> dict:
    """프로젝트 파일의 <Channel> 정의 전체 (속성 + 자식 요소 값) — 계측/스마트 구역, 앱이 읽는지,
    그리고 스마트 채널의 숫자 값 중 원 코드의 바이트 위치와 맞는 것이 있는지 (다른 변수도 읽을 수 있는지 판단용)."""
    root = _xml_root(proj_path)
    L = parse_proj(proj_path)
    normal = iter(L["chans"])
    read_names = _smart_read_names()
    want_xml = {_norm(n) for n in EXTRA_SMART} | {"currentrpm", "compcurrenthz"}
    attrs, rows, xml, in_smart = [], [], {}, False
    for el in root.iter():
        if str(el.tag).split("}")[-1] != "Channel":
            continue
        a = {str(k).split("}")[-1]: str(v) for k, v in el.attrib.items()}
        for k, v in _child_fields(el).items():      # 자식 요소 값도 같은 표에 (이름이 겹치면 '(자식)')
            a.setdefault(k if k not in a else f"{k}(자식)", v)
        for k in a:
            if k not in attrs:
                attrs.append(k)
        name = a.get("Name") or ""
        if name == "STX":
            zone, in_smart = "stx", True
        elif name == "ETX":
            zone, in_smart = "etx", False
        else:
            zone = "smart" if in_smart else "normal"
        if _norm(name) in want_xml and zone == "smart":
            xml.setdefault(name, _xml_snip(el))
        r = {"zone": zone, "attrs": a, "read": None, "col": None, "pos": None}
        if zone == "normal":
            try:
                col, s, e = next(normal)
                r.update(read=True, col=col, pos=f"{s}~{e - 1}")
            except StopIteration:
                pass
        elif zone == "smart":
            col = read_names.get(_norm(name))
            r.update(read=bool(col), col=col)
        rows.append(r)
    smart = [r for r in rows if r["zone"] == "smart"]
    t = {
        "proj": Path(proj_path).name, "attrs": attrs, "rows": rows, "rec": L["rec"], "stx": L["stx"], "etx": L["etx"],
        "n_normal": sum(1 for r in rows if r["zone"] == "normal"),
        "n_app_smart": len(read_names), "pos_hints": _pos_hints(smart, attrs), "xml": xml,
    }
    t["sig"] = _proj_sig(t)
    # 원 코드 48개 외에 더 꺼내는 변수 (EXTRA_SMART) — 읽히면 표에 ✓
    t["extra"] = _extra_from_table(t)
    ok_extra = {_norm(f.get("proj_name") or f["name"]): f for f in t["extra"] if "pos" in f}
    sp = _smart_positions_from_table(t)
    t["pos_check"] = {"ok": sp["ok"], "why": sp["why"], "attr": sp.get("attr"), "shift": sp.get("shift"),
                      "tf": sp.get("tf"), "matched": sp.get("matched")}
    pos2col = {}       # 위치 정보를 믿을 수 있으면: 이름이 달라도 원 코드가 읽는 위치면 그 열 (예: 'HP Fan QCurrent' = 116 'Q Current')
    if sp["ok"]:
        for k, v in KNOWN_POS.items():
            if not isinstance(v, tuple) and k in read_names:
                pos2col.setdefault(v, (read_names[k], _known_size(k)))

    def span(p, size):
        return f"패킷 {p}" if size == 1 else f"패킷 {p}~{p + size - 1}"

    for r in smart:
        n = _norm(r["attrs"].get("Name", ""))
        kp = KNOWN_POS.get(n)
        if r["read"]:
            if kp is not None:
                r["pos"] = (f"패킷 {kp[0]}, 종류 {kp[1]}" if isinstance(kp, tuple) else span(kp, _known_size(n)))
        elif n in ok_extra:
            f = ok_extra[n]
            r.update(read=True, col=f["name"], pos=span(f["pos"], f["size"]))
        elif sp["ok"]:
            p = _pos_val(r, sp["h"], _known_size(n))
            hit = pos2col.get(p) if p is not None else None
            if hit:
                r.update(read=True, col=hit[0], pos=span(p, hit[1]))
    t["n_smart"], t["n_smart_read"] = len(smart), sum(1 for r in smart if r["read"])
    return t


# ──────────────────────────────────────────────
#  통신 패킷 채널의 바이트 위치 — .proj 정의에서
# ──────────────────────────────────────────────
# 원 코드에는 없지만 AMS CSV 에는 있는 변수 — .proj 정의나 같은 측정의 AMS CSV 로 위치를 찾아 읽음
EXTRA_SMART = ("Heatpump_Comp Current",)

_MUX_FROM = 155          # 원 코드: 이 위치부터는 패킷 종류([3])마다 뜻이 바뀜
_SIZE_KEYS = ("size", "length", "len", "bytes", "bytecount", "bytesize", "bytelength", "datasize", "datalength",
              "datalen", "nbytes", "numbytes")
_BITLEN_KEYS = ("bitlength", "bitlen", "bitsize", "bits", "numbits", "bitcount", "bitwidth", "nbits")
_TYPE_KEYS = ("datatype", "dtype", "valuetype", "vartype", "format", "data_type", "datatypename", "type")
# 위치 값 해석: byte = 바이트 위치, bit = 비트 위치(÷8), last = 여러 바이트 값의 마지막 바이트 위치
_TF_TEXT = {"byte": "바이트 위치", "bit": "비트 위치(÷8)", "last": "값의 마지막 바이트 위치"}


def _pos_hints(smart: list, attrs: list) -> list:
    """스마트 채널의 숫자 값(속성·자식) ↔ 원 코드 바이트 위치 — 이름이 같은 채널끼리 견줘 차이가 일정한 것."""
    hints = []
    for k in attrs:
        if k in ("Name", "Type"):
            continue
        for tf in ("byte", "bit", "last"):
            diffs, known = [], 0
            for r in smart:
                n = _norm(r["attrs"].get("Name", ""))
                kp = KNOWN_POS.get(n)
                v = _num(r["attrs"].get(k))
                if kp is None or v is None:
                    continue
                known += 1
                if tf == "bit":
                    if v % 8:
                        continue
                    v //= 8
                elif tf == "last":
                    if isinstance(kp, tuple):
                        continue
                    v -= _known_size(n) - 1
                diffs.append(v - (kp[0] if isinstance(kp, tuple) else kp))
            if known >= 3 and diffs:
                d, cnt = max(((d, diffs.count(d)) for d in set(diffs)), key=lambda x: (x[1], -abs(x[0])))
                hints.append({"attr": k, "tf": tf, "matched": cnt, "known": known, "shift": d})
    # 같은 값이면 바이트 위치 > 비트 > 마지막 바이트 순으로 (단순한 해석 먼저)
    order = {"byte": 0, "bit": 1, "last": 2}
    hints.sort(key=lambda h: (-h["matched"], order[h["tf"]]))
    return hints


def _size_attr(a: dict, bits: bool = False):
    """채널 정의에서 크기(바이트) — 모르면 None, 비트 값이면 0.  bits: 위치가 비트 단위인 .proj (크기도 비트일 수 있음)."""
    low = {}
    for k, v in a.items():
        kl = str(k).lower().replace("(자식)", "")
        low.setdefault(kl, v)
        low.setdefault(kl.split(".")[-1], v)
    for k in _BITLEN_KEYS:
        n = _num(low.get(k))
        if n in (8, 16, 32, 64):
            return n // 8
        if n is not None and 0 < n < 8:
            return 0
    for k in _SIZE_KEYS:
        n = _num(low.get(k))
        if bits and n in (8, 16, 32, 64):
            return n // 8
        if not bits and n in (1, 2, 4, 8):
            return n
    for k in _TYPE_KEYS:
        if k not in low:
            continue
        v = re.sub(r"[\s_]", "", str(low[k]).lower())
        if k == "type" and v in ("smart", "normal", "analog", "digital", "stx", "etx", ""):
            continue                                                      # 채널 종류 표시일 뿐
        m = re.search(r"(?<!\d)(8|16|32|64)-?bit", v)                    # '16bit', '16-bit', '16 Bit Unsigned'
        if m:
            return int(m.group(1)) // 8
        m = re.search(r"(?<!\d)([1248])-?bytes?", v)                      # '2byte', '2bytes'
        if m:
            return int(m.group(1))
        if "bit" in v or "bool" in v:
            return 0
        if any(s in v for s in ("64", "double", "qword")):
            return 8
        if any(s in v for s in ("32", "dword", "float", "single", "long", "real")):
            return 4
        if any(s in v for s in ("16", "word", "short")):
            return 2
        if any(s in v for s in ("8", "byte", "char")):
            return 1
    return None


def _pos_val(r: dict, h: dict, size: int | None = None):
    """채널 r 의 패킷 바이트 위치 (STX = 0) — 위치 해석 h 로. 'last' 는 크기를 알아야 시작 위치가 나옴."""
    v = _num(r["attrs"].get(h["attr"]))
    if v is None:
        return None
    if h.get("tf") == "bit":
        if v % 8:
            return None
        v //= 8
    p = v - h["shift"]
    if h.get("tf") == "last":
        if not size:
            return None
        p -= size - 1
    return p


def _verify_pos_attr(t: dict, h: dict):
    """위치 해석 h 를 믿을 수 있나 → (맞은 수, 견준 수, 못 믿는 이유 | "").
    · 고정 구역(패킷 종류마다 안 바뀌는 곳) 원 코드 값들이 거의 다 (80% 이상, 3개 이상) 같은 차이로 맞아야 하고
    · 맞은 채널들이 .proj 순서대로 1씩만 늘어나면 위치가 아니라 순번일 수 있으므로 (1바이트 값이 이어질 때 우연히 맞음) 안 믿음."""
    fixed, i = [], 0
    for r in t["rows"]:
        if r["zone"] != "smart":
            continue
        i += 1
        n = _norm(r["attrs"].get("Name", ""))
        kp = KNOWN_POS.get(n)
        if kp is None or isinstance(kp, tuple) or _num(r["attrs"].get(h["attr"])) is None:
            continue
        fixed.append((i, _pos_val(r, h, _known_size(n)), kp))
    hits = [(i, kp) for i, p, kp in fixed if p == kp]
    a, n = len(hits), len(fixed)
    what = f"'{h['attr']}'" + ("" if h.get("tf", "byte") == "byte" else f"({_TF_TEXT[h['tf']]})")
    if a < 3 or a < 0.8 * n:
        return a, n, f"{what} 값이 원 코드 바이트 위치와 {n}개 중 {a}개만 맞음"
    if all(k2 - k1 == i2 - i1 for (i1, k1), (i2, k2) in zip(hits, hits[1:])):
        return a, n, (f"{what} 값이 원 코드 위치와 맞긴 하지만 맞은 {a}개가 .proj 순서대로 1씩 늘어나는 값뿐이라 "
                      f"위치인지 순번인지 알 수 없음")
    return a, n, ""


def _smart_positions_from_table(t: dict) -> dict:
    """통신 패킷 채널의 바이트 위치(STX = 0)·크기 — .proj 의 위치 값이 원 코드 위치와 맞을 때만.
    크기: 크기·자료형 정의가 있으면 그것, 없으면 다음 채널까지 간격(1 이면 1바이트, 그 이상이면 2바이트) —
    이 추정이 원 코드 값들의 크기와 맞는지도 확인."""
    if not t.get("pos_hints"):
        return {"ok": False, "chans": {},
                "why": (".proj 통신 패킷 채널 정의에 바이트 위치로 볼 숫자가 없음 "
                        "(원 코드 변수와 이름이 같은 채널이 3개 미만이거나, 위치가 숫자로 적혀 있지 않음)")}
    whys = []
    for h in t["pos_hints"][:12]:
        fixed_ok, fixed_n, why = _verify_pos_attr(t, h)
        if not why:
            break
        whys.append(why)
    else:
        return {"ok": False, "chans": {}, "why": "위치로 믿을 만한 값이 없음 — " + whys[0]}
    plen = (t["etx"] - t["stx"]) if t.get("stx") is not None else 0
    bits = h["tf"] == "bit"
    smart = []
    for r in t["rows"]:
        if r["zone"] == "smart":
            sz = _size_attr(r["attrs"], bits)
            p = _pos_val(r, h, sz)
            if p is not None:
                smart.append((r["attrs"].get("Name", ""), p, sz))
    poss = sorted({p for _, p, _ in smart})

    def gap_size(p):
        nxt = [q for q in poss if q > p]
        return 1 if ((nxt[0] - p) if nxt else (plen - 1 - p)) <= 1 else 2

    agree = tot = 0
    for name, p, sz in smart:
        n = _norm(name)
        if n in KNOWN_POS and not isinstance(KNOWN_POS[n], tuple):
            tot += 1
            agree += (sz if sz is not None else gap_size(p)) == _known_size(n)
    chans = {name: (p, sz if sz is not None else gap_size(p), "크기 정의" if sz is not None else "다음 채널까지 간격")
             for name, p, sz in smart}
    return {"ok": True, "why": "", "attr": h["attr"], "shift": h["shift"], "tf": h["tf"], "h": h, "chans": chans,
            "size_check": (agree, tot), "plen": plen, "matched": (fixed_ok, fixed_n)}


def _extra_from_table(t: dict) -> list:
    """EXTRA_SMART 각각 → 읽는 방법.
      {"name", "pos", "size", "enc", "a", "how", ...}  패킷에서 읽음 (같은 측정 CSV 로 찾은 것이 있으면 그것이 먼저)
      {"name", "normal", "col"}                       계측 채널로 이미 읽음
      {"name", "why", "xml"?, "missing"?}            못 읽는 이유 (.proj 에 그 이름이 아예 없으면 missing)"""
    sp = _smart_positions_from_table(t)
    by = {_norm(k): (k, v) for k, v in sp["chans"].items()}
    smart_names = {_norm(r["attrs"].get("Name", "")): r["attrs"].get("Name", "") for r in t["rows"] if r["zone"] == "smart"}
    normal = {_norm(r["attrs"].get("Name", "")): r.get("col") for r in t["rows"] if r["zone"] == "normal"}
    learned = learned_fields(t.get("sig"))
    out = []
    for name in EXTRA_SMART:
        n = _norm(name)
        if n in normal and n not in smart_names:
            out.append({"name": name, "normal": True, "col": normal[n]})
            continue
        lh = learned.get(name)
        if lh and lh.get("pos") is not None:
            out.append(_learned_entry(name, lh, smart_names.get(n)))
            continue
        xml = (t.get("xml") or {}).get(smart_names.get(n, ""), "")
        if n not in smart_names:
            out.append({"name": name, "why": ".proj 통신 패킷 채널에 이 이름이 없음", "missing": True})
            continue
        if not sp["ok"]:
            out.append({"name": name, "why": sp["why"], "xml": xml})
            continue
        hit = by.get(n)
        if hit is None:
            why = (f"이 채널의 크기를 몰라 시작 위치를 정할 수 없음 ({_TF_TEXT['last']})" if sp["tf"] == "last"
                   else f".proj 의 이 채널에 위치 값 '{sp['attr']}' 이 없음")
            out.append({"name": name, "why": why, "xml": xml})
            continue
        p, size, how = hit[1]
        agree, tot = sp["size_check"]
        if size == 0:
            why = "비트 값으로 정의됨 — 아직 지원 안 함"
        elif size not in (1, 2):
            why = f"{size}바이트 값으로 정의됨 — 정수/실수 형식 확인 필요 (원 코드 값은 1·2바이트뿐)"
        elif p >= _MUX_FROM:
            why = f"위치 {p} 는 패킷 종류마다 뜻이 바뀌는 구간이라 종류 정보가 필요함"
        elif p < 1 or p + size > sp["plen"]:
            why = f"위치 {p} 가 패킷 범위를 벗어남"
        elif how != "크기 정의" and tot and agree < 0.8 * tot:
            why = f"크기 정의가 없고, 간격으로 추정한 크기가 원 코드 값과 {tot}개 중 {agree}개만 맞음"
        else:
            tf = "" if sp["tf"] == "byte" else f", {_TF_TEXT[sp['tf']]}"
            out.append({"name": name, "pos": int(p), "size": int(size), "enc": "be", "a": 1.0,
                        "how": f".proj '{sp['attr']}'{tf} (크기: {how})",
                        "proj_name": hit[0], "attr": sp["attr"], "shift": sp["shift"], "matched": sp["matched"]})
            continue
        out.append({"name": name, "why": why, "xml": xml})
    return out


# ──────────────────────────────────────────────
#  같은 측정의 AMS CSV 로 위치 찾기 (.proj 로 못 찾을 때) — 찾은 위치는 .proj 내용별로 기억
# ──────────────────────────────────────────────
_LEARN_PATH = Path(__file__).resolve().parent / "config" / "ams_dat_formats" / "proj_learned.json"
_LEARN_LOCK = threading.Lock()
_LEARN_MEMO: dict = {}       # (sig, .dat, CSV, 크기·수정시각, 변수) → 결과 — 같은 실행 중엔 다시 안 함 (실패 포함)
_LEARN_ROWS = 30000          # CSV 에서 볼 최대 행


def _proj_sig(t: dict) -> str:
    """.proj 의 통신 패킷 정의 내용 → 짧은 지문 (같은 내용의 .proj 면 폴더가 달라도 같음)."""
    items = [[r["zone"], sorted(r["attrs"].items())] for r in t["rows"] if r["zone"] != "normal"]
    raw = json.dumps([t.get("stx"), t.get("etx"), t.get("rec"), items], ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _learned_all() -> dict:
    try:
        d = json.loads(_LEARN_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def learned_fields(sig) -> dict:
    """같은 .proj 내용으로 배운 위치 {변수: {...}}."""
    if not sig:
        return {}
    return ((_learned_all().get(sig) or {}).get("fields")) or {}


def _learned_entry(name: str, v: dict, proj_name=None) -> dict:
    """배운 위치 기록 → L["extra"] 항목."""
    return {"name": name, "pos": int(v["pos"]), "size": int(v["size"]), "enc": v.get("enc", "be"),
            "a": float(v.get("a", 1) or 1), "how": f"같은 측정 AMS CSV '{v.get('csv', '')}' 로 찾은 위치",
            "learned": v, "proj_name": proj_name}


def _learned_put(sig: str, proj_name: str, fields: dict) -> None:
    with _LEARN_LOCK:
        d = _learned_all()
        e = d.setdefault(sig, {"proj": proj_name, "fields": {}})
        e["proj"] = proj_name
        e.setdefault("fields", {}).update(fields)
        try:
            _LEARN_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _LEARN_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, _LEARN_PATH)
        except OSError:
            pass


def _csv_header(path: Path):
    """CSV 머리글 행 찾기 → {row, cols, enc, sep} | None (앞 40줄 안에서 Time 열이 있는 줄)."""
    try:
        with open(path, "rb") as f:
            raw = f.read(1 << 18)
    except OSError:
        return None
    encs = ["utf-16"] if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ["utf-8-sig", "cp949", "latin-1"]
    for enc in encs:
        try:
            txt = raw.decode(enc, errors="strict" if enc != "latin-1" else "replace")
        except UnicodeDecodeError:
            # 잘린 마지막 글자 때문일 수 있음 — 끝을 조금 버리고 다시
            try:
                txt = raw[:-4].decode(enc)
            except UnicodeDecodeError:
                continue
        lines = txt.splitlines()[:40]
        for i, line in enumerate(lines):
            sep = max((",", "\t", ";"), key=line.count)
            cols = [c.strip().strip('"').strip() for c in line.split(sep)]
            if len(cols) >= 3 and any(_is_time_col(c) for c in cols):
                return {"row": i, "cols": cols, "enc": enc, "sep": sep}
    return None


def _is_time_col(c) -> bool:
    return str(c).strip().lower() in ("time", "시간", "시각", "datetime", "date time", "date/time", "timestamp", "logtime")


def _twin_csvs(dat_path, names) -> list:
    """.dat 와 같은 폴더의 CSV 중 names 열이 있는 것 (같은 이름 먼저)."""
    p = Path(dat_path)
    want = {_norm(n): n for n in names}
    out = []
    try:
        files = [f for f in p.parent.iterdir() if f.is_file() and f.suffix.lower() == ".csv"]
    except OSError:
        return out
    for f in files:
        h = _csv_header(f)
        if not h:
            continue
        norms = {}
        for c in h["cols"]:
            norms.setdefault(_norm(c), c)
        hit = {want[n]: norms[n] for n in want if n in norms}
        if hit:
            h.update(path=f, hit=hit, tcol=next(c for c in h["cols"] if _is_time_col(c)), norms=norms)
            out.append(h)
    out.sort(key=lambda h: (h["path"].stem.lower() != p.stem.lower(), -h["path"].stat().st_mtime))
    return out


def _match_rate(X: np.ndarray, y: np.ndarray, a: float) -> np.ndarray:
    """X (m×k) 를 a 배 한 값이 y (m) 와 같은 비율 — 열별."""
    return np.isclose(X * a, y[:, None], rtol=1e-6, atol=1e-6).mean(axis=0)


_SCALES = (1.0, 0.1, 0.01, 10.0, 100.0)


def _learn_from_csv(dat_path, L: dict, h: dict, names: list) -> dict:
    """같은 측정의 AMS CSV(h) 와 .dat 기록을 시각으로 짝지어, names 값이 패킷의 어느 바이트에 있는지 찾음.
    → {"fields": {변수: {...}}, "why": {변수: 이유}, "pairs": 짝 수, "ref": 기준 변수 일치율}"""
    import ams_dat                                    # 지연 import (ams_dat 이 이 모듈을 import 함)
    p = Path(dat_path)
    out = {"fields": {}, "why": {}, "pairs": 0, "ref": None}
    # ── CSV: 시각 + 찾을 변수 + 원 코드 위치를 아는 변수(짝이 맞는지 확인용)
    refs = {n: c for n, c in h["norms"].items() if n in KNOWN_POS and not isinstance(KNOWN_POS[n], tuple)}
    keep = {h["tcol"], *h["hit"].values(), *refs.values()}
    try:
        df = pd.read_csv(h["path"], sep=h["sep"], skiprows=h["row"], header=0, encoding=h["enc"],
                         usecols=lambda c: str(c).strip().strip('"').strip() in keep,
                         nrows=_LEARN_ROWS, low_memory=False, on_bad_lines="skip")
    except Exception as e:
        out["why"] = {n: f"CSV '{h['path'].name}' 을 읽지 못함 ({type(e).__name__})" for n in names}
        return out
    df.columns = [str(c).strip().strip('"').strip() for c in df.columns]
    if h["tcol"] not in df.columns or df.empty:
        out["why"] = {n: f"CSV '{h['path'].name}' 에 시각 값이 없음" for n in names}
        return out
    sec, kind, _ = ams_dat.csv_time_seconds(df[h["tcol"]])
    if kind == "elapsed":
        out["why"] = {n: f"CSV '{h['path'].name}' 의 Time 이 경과 시간이라 .dat 기록과 짝지을 수 없음" for n in names}
        return out
    ct = np.mod(np.asarray(sec, float), 86400.0)
    # ── .dat 기록 전체
    rec = int(L["rec"])
    buf = p.read_bytes()
    n = len(buf) // rec
    if n < 30 or L.get("stx") is None:
        out["why"] = {k: ".dat 기록이 너무 적음" for k in names}
        return out
    A = np.frombuffer(buf, np.uint8, count=n * rec).reshape(n, rec)
    ts = np.ascontiguousarray(A[:, :8]).view("<f8").ravel()
    ok = _ts_ok(ts)
    vi = np.flatnonzero(ok)
    if len(vi) < 30:
        out["why"] = {k: ".dat 에 시각 값이 있는 기록이 너무 적음" for k in names}
        return out
    dt = np.mod(ts[vi] + float(L.get("epoch_off") or 0.0), 86400.0)
    srt = np.argsort(dt)
    dts, vis = dt[srt], vi[srt]
    step = float(np.median(np.diff(dts)[np.diff(dts) > 0])) if len(dts) > 2 else 1.0
    cok = np.flatnonzero(np.isfinite(ct))
    frac = bool(np.any(np.abs(ct[cok] - np.round(ct[cok])) > 1e-6))     # CSV 시각에 소수점 이하 초가 있나
    tol = max(0.55 * step, 0.002) if frac else max(1.0, 0.55 * step)

    def pair(shift):
        c = np.mod(ct[cok] + shift, 86400.0)
        j = np.searchsorted(dts, c)
        j0, j1 = np.clip(j - 1, 0, len(dts) - 1), np.clip(j, 0, len(dts) - 1)
        d0, d1 = np.abs(c - dts[j0]), np.abs(c - dts[j1])
        pick = np.where(d1 < d0, j1, j0)
        good = np.minimum(d0, d1) <= tol
        return vis[pick[good]], cok[good]

    best = None
    for hrs in [0] + [x for k in range(1, 13) for x in (k, -k)]:       # 시간대가 다르게 적힌 CSV 도 (정시 단위)
        ri, ci = pair(hrs * 3600.0)
        if best is None or len(ri) > len(best[0]):
            best = (ri, ci, hrs)
        if len(ri) >= 0.5 * len(cok):
            break
    ri, ci, hrs = best
    if len(ri) < 30:
        out["why"] = {k: f"CSV '{h['path'].name}' 와 .dat 의 시각이 겹치지 않음 (같은 측정이 아닌 듯)" for k in names}
        return out
    out["pairs"] = int(len(ri))
    P = A[ri, L["stx"]:min(L["etx"], rec)].astype(np.float64)
    plen = P.shape[1]
    X1 = P
    X2b = P[:, :-1] * 256 + P[:, 1:]
    X2l = P[:, :-1] + P[:, 1:] * 256
    # ── 짝이 맞는지: 원 코드 위치를 아는 변수들이 같은 값인지
    rr = []
    for nn, col in refs.items():
        y = pd.to_numeric(df[col].iloc[ci], errors="coerce").to_numpy(float)
        f = np.isfinite(y)
        if f.sum() < 30 or len(np.unique(y[f])) < 3:
            continue
        kp, sz = KNOWN_POS[nn], _known_size(nn)
        x = (X2b[:, kp] if sz == 2 else X1[:, kp]) if kp + sz <= plen else None
        if x is None:
            continue
        rr.append(max(float(np.isclose(x[f] * a, y[f], rtol=1e-6, atol=1e-6).mean()) for a in _SCALES))
    out["ref"] = (round(float(np.mean(rr)), 3), len(rr)) if rr else None
    # ── 찾을 변수
    for name in names:
        col = h["hit"].get(name)
        if col is None:
            continue
        y = pd.to_numeric(df[col].iloc[ci], errors="coerce").to_numpy(float)
        f = np.isfinite(y)
        if f.sum() < 30:
            out["why"][name] = f"CSV '{h['path'].name}' 의 {col} 값이 거의 비어 있음"
            continue
        if len(np.unique(y[f])) < 3:
            out["why"][name] = (f"CSV '{h['path'].name}' 의 {col} 값이 거의 일정해서 ({len(np.unique(y[f]))}가지) "
                                f"위치를 정할 수 없음 — 압축기가 도는 구간이 있는 측정의 CSV 가 필요")
            continue
        yy = y[f]
        cands = []      # (일치율, 선호 순, 위치, 크기, 형식, 배율)
        for enc, X, size, pref in (("be", X2b, 2, 0), ("u8", X1, 1, 1), ("le", X2l, 2, 2)):
            Xf = X[f]
            for ai, a in enumerate(_SCALES):
                rate = _match_rate(Xf, yy, a)
                rate[0] = 0.0                                   # 0 = STX
                for q in np.argsort(-rate)[:3]:
                    cands.append((float(rate[q]), pref + ai * 3, int(q), size, enc, a))
        cands.sort(key=lambda c: (-c[0], c[1], c[2]))
        top = cands[0]
        thr = 0.97 if frac else 0.9
        if top[0] < thr:
            out["why"][name] = (f"CSV '{h['path'].name}' 의 {col} 와 같은 값이 패킷 안에 없음 "
                                f"(가장 비슷한 곳 위치 {top[2]}: {top[0]:.0%} 일치)")
            continue
        # 같은 값이 겹치지 않는 다른 곳에도 있으면 (드묾) — 앞 위치를 쓰고 표시
        other = [c for c in cands if c[0] >= top[0] - 0.02 and abs(c[2] - top[2]) > 1]
        out["fields"][name] = {
            "pos": top[2], "size": top[3], "enc": "be" if top[4] == "u8" else top[4], "a": top[5],
            "agree": round(top[0], 4), "n": int(f.sum()), "csv": h["path"].name, "col": col,
            "dat": p.name, "at": time.strftime("%Y-%m-%d %H:%M"),
            **({"also": sorted({c[2] for c in other})[:5]} if other else {}),
            **({"tz_hours": hrs} if hrs else {}),
        }
    return out


_ENSURE_MEMO: dict = {}


def ensure_extra(dat_path, L: dict, log=print) -> None:
    """L["extra"] 중 같은 측정 CSV 로 확인되지 않은 변수를 같은 폴더의 AMS CSV 로 찾아 L 에 반영 (찾으면 기억).
    .proj 로 찾은 위치도 CSV 가 있으면 확인 — 다르면 CSV 로 찾은 위치를 씀."""
    ex = L.get("extra") or []
    if L.get("stx") is None or not L.get("sig"):
        return
    stored = learned_fields(L["sig"])        # 다른 폴더에서 같은 .proj 로 이미 배운 위치 (이 배치를 읽은 뒤에 배웠을 수도)
    for i, f in enumerate(ex):
        v = stored.get(f["name"])
        if v and v.get("pos") is not None and not f.get("normal") and not f.get("learned"):
            ex[i] = _learned_entry(f["name"], v, f.get("proj_name"))
    need = [f["name"] for f in ex if not f.get("normal") and not f.get("learned")]
    if not need:
        return
    p = Path(dat_path)
    try:
        st = p.stat()
        dkey = (str(p), st.st_size, st.st_mtime, p.parent.stat().st_mtime, L["sig"], tuple(need))
    except OSError:
        return
    if dkey in _ENSURE_MEMO:
        res_list = _ENSURE_MEMO[dkey]
    else:
        res_list = []
        for h in _twin_csvs(p, need)[:3]:
            try:
                hs = h["path"].stat()
                key = (L["sig"], str(p), str(h["path"]), hs.st_size, hs.st_mtime, tuple(need))
            except OSError:
                continue
            if key not in _LEARN_MEMO:
                try:
                    _LEARN_MEMO[key] = _learn_from_csv(p, L, h, need)
                except Exception as e:
                    _LEARN_MEMO[key] = {"fields": {}, "why": {n: f"CSV '{h['path'].name}' 로 찾다 실패 ({type(e).__name__}: {e})"
                                                          for n in need}}
            res_list.append(_LEARN_MEMO[key])
            if all(n in _LEARN_MEMO[key]["fields"] for n in need):
                break
        _ENSURE_MEMO[dkey] = res_list
    found, whys = {}, {}
    for res in res_list:
        for n, v in res.get("fields", {}).items():
            found.setdefault(n, v)
        for n, w in res.get("why", {}).items():
            whys.setdefault(n, w)
    if found:
        _learned_put(L["sig"], Path(L["proj"]).name, found)
    for i, f in enumerate(ex):
        n = f["name"]
        if n in found:
            v = found[n]
            new = _learned_entry(n, v, f.get("proj_name"))
            if "pos" in f and (f["pos"], f["size"]) != (new["pos"], new["size"]):
                new["note"] = f".proj 로 본 위치 {f['pos']} 와 달라 CSV 로 찾은 위치를 씀"
            if "learned" not in f:
                span = f"{v['pos']}~{v['pos'] + 1}" if int(v["size"]) == 2 else f"{v['pos']}"
                scale = ("" if float(v.get("a", 1) or 1) == 1 else f", ×{float(v['a']):g}") + \
                    (", 낮은 바이트 먼저" if v.get("enc") == "le" and int(v["size"]) == 2 else "")
                log(f"  [ams_dat] {p.name}: {n} 위치를 같은 측정 AMS CSV '{v.get('csv', '')}' 로 찾음 → "
                    f"패킷 {span} ({v['size']}바이트{scale}, {float(v['agree']):.1%} 일치, {int(v['n']):,}개 시점)"
                    f"{' — ' + new['note'] if new.get('note') else ''} · 같은 .proj 면 다음부터 CSV 없이도 읽음")
            ex[i] = new
        elif "pos" not in f and n in whys:
            f["why_csv"] = whys[n]
