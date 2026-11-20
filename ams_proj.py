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
"""
from __future__ import annotations

import re
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
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
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
    held = []
    if L["stx"] is not None:
        P = A[:, L["stx"]:min(L["etx"], rec)]
        for name, v in _smart_columns(P):
            if isinstance(v, _Held):
                held.append(name)
            cols[name] = np.asarray(v)
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
