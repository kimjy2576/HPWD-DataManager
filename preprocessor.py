"""
preprocessor.py — 개별 데이터소스 전처리, 시간 동기화, 병합

[최적화]
- BlackRose 시간 파싱: .apply(lambda) → str accessor 벡터화
- sync_and_merge: pd.merge_asof 활용, 불필요 copy 제거
- to_numeric: apply 대신 infer_objects 또는 컬럼별 astype 활용
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dateutil import parser as dateutil_parser


# ══════════════════════════════════════════════
#  시간 파싱 유틸리티
# ══════════════════════════════════════════════
def parse_flexible_datetime(value):
    """다양한 형식의 날짜/시간 값을 datetime으로 변환한다."""
    if pd.isna(value) or value == "":
        return pd.NaT
    if isinstance(value, (datetime, pd.Timestamp)):
        return value
    if isinstance(value, (int, float)):
        return datetime(1899, 12, 30) + timedelta(days=value)

    s = str(value).strip()
    if "00:00:00" in s:
        s = s.replace("00:00:00", "").strip()
    if ":" not in s and "." in s and len(s) <= 8:
        s = s.replace(".", ":")

    try:
        return dateutil_parser.parse(s)
    except Exception:
        return pd.NaT


def _align_datetime_to_base(df: pd.DataFrame, base_date) -> pd.DataFrame:
    """
    Time 열을 base_date 기준으로 통일.
    [v2] 자정 넘김을 '연속 단조감소 탐지'로 판별 (hour 비교 제거).
    """
    if pd.isna(base_date):
        return df

    # Time 컬럼이 없으면 LogTime 등에서 폴백
    if "Time" not in df.columns:
        for alt in ["LogTime", "logtime", "LOGTIME", "time", "DateTime", "datetime", "Timestamp"]:
            if alt in df.columns:
                df = df.rename(columns={alt: "Time"})
                break
    if "Time" not in df.columns:
        return df

    temp_dt = pd.to_datetime(df["Time"], errors="coerce")
    time_comp = temp_dt - temp_dt.dt.normalize()
    base_midnight = pd.Timestamp(base_date).normalize()
    df["Time"] = base_midnight + time_comp

    # 자정 넘김 탐지: 시간이 감소하는 지점에서 +1일
    # (예: 23:59:59 → 00:00:00 에서 diff가 음수)
    if len(df) > 1:
        time_diff = df["Time"].diff()
        midnight_crossings = time_diff < pd.Timedelta(0)
        # 자정 넘김 이후 모든 행에 +1일씩 누적
        day_offset = midnight_crossings.cumsum()
        df["Time"] = df["Time"] + pd.to_timedelta(day_offset, unit="D")

    return df


# ══════════════════════════════════════════════
#  BlackRose 전처리
# ══════════════════════════════════════════════
def _fuzzy_col_match(df_columns, wanted_columns):
    """공백/언더스코어를 동일시하여 컬럼 매칭. 매칭된 실제 컬럼명 리스트 반환."""
    # 정규화 맵: "HP Fan Q Current" → "hp_fan_q_current"
    norm = lambda s: s.strip().replace(" ", "_").lower()
    col_map = {norm(c): c for c in df_columns}  # 정규화 → 실제
    result = []
    for w in wanted_columns:
        if w in df_columns:
            result.append(w)  # 정확 매칭 우선
        elif norm(w) in col_map:
            result.append(col_map[norm(w)])  # 퍼지 매칭
    return result


def preprocess_blackrose(
    df_raw: pd.DataFrame,
    selected_columns: list,
    column_mapping: dict,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    BlackRose 데이터 전처리.
    """
    if df_raw.empty:
        dummy = pd.DataFrame({"Time": [0]})
        return dummy.copy(), dummy.copy()

    df = df_raw.copy()
    df.columns = [col.strip() for col in df.columns]

    # LogTime 폴백: 다양한 시간 컬럼명 지원
    time_col = None
    for candidate in ["LogTime", "logtime", "Log Time", "Time", "DateTime", "Timestamp"]:
        if candidate in df.columns:
            time_col = candidate; break
    if not time_col:
        first = df.columns[0]
        try:
            pd.to_datetime(df[first].iloc[0])
            time_col = first
        except: pass
    if not time_col:
        print(f"[preprocess_br] 시간 컬럼 못 찾음. 컬럼: {df.columns[:10].tolist()}")
        dummy = pd.DataFrame({"Time": [0]})
        return dummy.copy(), dummy.copy()

    time_str = df[time_col].astype(str)
    df["Time"] = time_str.str.split(" ").str[1].str.split(".").str[0]

    # 필터링: SW_ProtectionCount > 0
    if "SW_ProtectionCount" in df.columns:
        df_main = df[df["SW_ProtectionCount"] > 0].copy()
    else:
        df_main = df.copy()
    if "MainProcess" in df.columns:
        df_sub = df[df["MainProcess"] == 10].copy()
    else:
        df_sub = pd.DataFrame(columns=df.columns)

    df_main.rename(columns=column_mapping, inplace=True)

    add_columns = ["Time", "SubProcess", "RemainTime"]
    matched = _fuzzy_col_match(df_main.columns, selected_columns)
    df_main = df_main[matched]
    df_add  = df_sub[[c for c in add_columns if c in df_sub.columns]]

    return df_main, df_add


# ══════════════════════════════════════════════
#  AMS 전처리
# ══════════════════════════════════════════════
def preprocess_ams(
    df_raw: pd.DataFrame,
    column_mapping: dict,
    scale_factors: dict,
    subprocess_mapping: dict,
    selected_columns: list,
) -> pd.DataFrame:
    """AMS 데이터 전처리."""
    if df_raw.empty:
        return pd.DataFrame({"Time": [0]})

    df = df_raw.copy()
    raw_cols = set(map(str, df.columns))
    df.rename(columns=column_mapping, inplace=True)
    # 한/중 전력 컬럼 등 여러 원본이 같은 표준명으로 바뀌면 중복 → 첫 번째만 유지
    df = df.loc[:, ~df.columns.duplicated(keep="first")]
    diag = []

    # 파일 중간에 헤더가 반복된 행(로거 재연결) 제거 — 남겨두면 모든 컬럼에 문자가 섞임
    if "Time" in df.columns:
        hdr_rows = df["Time"].astype(str).str.strip().eq("Time")
        if hdr_rows.any():
            diag.append(f"  [ams] ⚠️ 파일 중간 헤더 반복 {int(hdr_rows.sum())}행 제거 (로거 재연결로 보임)")
            df = df[~hdr_rows].reset_index(drop=True)

    # 매핑된 수치 컬럼: 숫자화 + 0xFFFF(65535) 무효값 → NaN
    mapped = list(dict.fromkeys(v for k, v in column_mapping.items()
                                if k in raw_cols and v in df.columns))
    n_sent = 0
    for c in mapped:
        if c in ("SubProcess", "Time"):
            continue
        v = pd.to_numeric(df[c], errors="coerce")
        if not pd.api.types.is_numeric_dtype(df[c]):   # pandas 3 의 str 타입 포함
            txt = df[c].astype(str).str.strip()
            nonnum = df[c].notna() & v.isna() & (txt != "") & (txt.str.lower() != "nan")
            nn = int(nonnum.sum())
            if nn:
                ex = txt[nonnum].str.slice(0, 12).value_counts().head(3).index.tolist()
                diag.append(f"  [ams] ⚠️ {c}: 숫자가 아닌 값 {nn}개 → 빈 값 처리 (예: {ex})")
        bad = v == 65535
        if bad.any():
            n_sent += int(bad.sum())
            v = v.mask(bad)
        df[c] = v
    if n_sent:
        diag.append(f"  [ams] 센서 무효값 65535 {n_sent}개 → 빈 값 처리")

    # 스케일 변환
    #   '[*10]', '[/100]' 표기가 있는 원본 컬럼은 로거가 배율을 명시한 것 → 설정 배율 적용
    #   표기가 없는 로거 버전은 배율이 다를 수 있으므로 자동 판정
    SUFFIXED = {"HP_CompTargetHz": "CompTargetHz[*10]",
                "HP_FanSpeed": "FanSpeed[/100]",
                "HP_SuperHeatDegree": "SuperHeatDegree[*10]"}
    for col, factor in scale_factors.items():
        if col not in df.columns:
            continue
        tag = SUFFIXED.get(col)
        if tag and tag not in raw_cols:
            factor = _infer_ams_scale(df, col, factor, diag)
            if factor == 1.0:
                continue
        df[col] = pd.to_numeric(df[col], errors="coerce") * factor

    if "SubProcess" in df.columns:
        df["SubProcess"] = df["SubProcess"].replace(subprocess_mapping)

    available = _fuzzy_col_match(df.columns, selected_columns)
    df = df[available].copy()

    # 진단: 주요 변수가 제자리에 들어왔는지 (0 제외 중앙값)
    parts = []
    for c in ["HP_CompCurrentHz", "HP_CompTargetHz", "Heatpump_DuctInTemp",
              "Heatpump_DuctOutTemp", "Heatpump_EvaInTemp", "Heatpump_EvaOutTemp", "Po_WD"]:
        if c not in df.columns:
            parts.append(f"{c}=없음")
            continue
        v = pd.to_numeric(df[c], errors="coerce")
        nz = v[(v != 0) & v.notna()]
        parts.append(f"{c}={nz.median():.1f}" if len(nz) else f"{c}=전부0")
    diag.append("  [ams] 주요 변수(0 제외 중앙값): " + ", ".join(parts))
    if "SubProcess" in df.columns:
        u = pd.Series(df["SubProcess"]).dropna().astype(str).unique()[:8]
        diag.append(f"  [ams] SubProcess 값: {list(u)}")
    df["Time"] = normalize_ams_time(df["Time"], diag)
    for ln in diag:
        print(ln)
    df.attrs["ams_diag"] = diag
    return df


def _infer_ams_scale(df, col, cfg_factor, diag):
    """스케일 표기가 없는 AMS 컬럼의 배율 판정.

    CompTargetHz: CompCurrentHz 와 운전 구간 비율로 판정 (≈10배면 ×10 저장)
    그 외: 원값 사용 + 중앙값을 로그로 남겨 사용자가 확인
    """
    v = pd.to_numeric(df[col], errors="coerce")
    if col == "HP_CompTargetHz" and "HP_CompCurrentHz" in df.columns:
        cur = pd.to_numeric(df["HP_CompCurrentHz"], errors="coerce")
        m = (v > 5) & (cur > 5)
        if int(m.sum()) >= 30:
            ratio = float((v[m] / cur[m]).median())
            if 7 <= ratio <= 13:
                diag.append(f"  [ams] {col}: 배율 표기 없음, CompCurrentHz 대비 {ratio:.1f}배 → ×{cfg_factor} 적용")
                return cfg_factor
            diag.append(f"  [ams] {col}: 배율 표기 없음, CompCurrentHz 대비 {ratio:.2f}배 → 원값 사용")
            return 1.0
    nz = v[(v != 0) & v.notna()]
    med = f"{nz.median():.1f}" if len(nz) else "-"
    diag.append(f"  [ams] {col}: 배율 표기 없음 → 원값 사용 (0 제외 중앙값 {med}, 설정 ×{cfg_factor} 미적용)")
    return 1.0


def normalize_ams_time(s: pd.Series, diag: list | None = None) -> pd.Series:
    """AMS Time 열을 'HH:MM:SS' 문자열로 정규화.

    지원 형식 (다수결로 판별):
      - 날짜 포함  '2024-01-01 10:00:00'  → 원본 유지 (to_datetime 이 처리)
      - HH:MM:SS(.f)  '10:23:45.123'      → 'HH:MM:SS'
      - MM:SS(.f)     '01:30.5'           → 경과시간 → 'HH:MM:SS'
          ※ pandas 는 이 형식을 '시:분'으로 오해석함
            ('00:01.0' → 1분, '59:59.9' → NaT) → 반드시 직접 환산
          ※ Excel 에서 열었다 저장하면 시(hour) 자리가 사라져
            60분마다 00:00.0 으로 되돌아감 → 롤오버 보정
      - 숫자(경과초)  '90.5'               → 'HH:MM:SS'
    소수점 이하 초는 버림 (이후 1초 격자로 리샘플되므로 영향 없음)
    """
    t = s.astype(str).str.strip()
    if len(t) == 0:
        return t

    def _to_hms(sec: pd.Series) -> pd.Series:
        sec = pd.to_numeric(sec, errors="coerce")
        whole = np.floor(sec)
        ok = whole.notna()
        out = pd.Series(np.nan, index=sec.index, dtype=object)
        w = whole[ok].astype("int64")
        out[ok] = ((w // 3600).astype(str).str.zfill(2) + ":"
                   + ((w % 3600) // 60).astype(str).str.zfill(2) + ":"
                   + (w % 60).astype(str).str.zfill(2))
        return out

    def _unroll(sec: pd.Series, period: float) -> pd.Series:
        """값이 period 의 절반 이상 급감하면 롤오버로 보고 period 를 누적 가산."""
        d = sec.diff()
        jumps = (d < -period / 2).cumsum()
        n = int(jumps.iloc[-1]) if len(jumps) and pd.notna(jumps.iloc[-1]) else 0
        if n:
            print(f"  [ams] Time 롤오버 {n}회 보정 (주기 {period/60:.0f}분)")
        return sec + jumps * period

    # 1) 날짜 포함 — 행마다 소수점 유무가 달라도 파싱되도록 format="mixed"
    #    (pandas 2 는 첫 행 형식으로 고정 추론 → 나머지 NaT 발생)
    if t.str.contains(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", regex=True).mean() >= 0.5:
        try:
            dt = pd.to_datetime(t, errors="coerce", format="mixed")
        except (TypeError, ValueError):
            dt = pd.to_datetime(t, errors="coerce")
        out = dt.dt.strftime("%Y-%m-%d %H:%M:%S")
        return out.where(dt.notna(), t)

    colons = t.str.count(":")
    mode = int(colons.mode().iloc[0]) if len(colons) else 0

    # 2) HH:MM:SS(.f)
    if mode >= 2:
        return t.str.slice(0, 8)

    # 3) MM:SS(.f) — 시 자리 없는 경과시간
    if mode == 1:
        p = t.str.extract(r"^\s*(\d+):(\d+(?:\.\d+)?)\s*$")
        sec = pd.to_numeric(p[0], errors="coerce") * 60 + pd.to_numeric(p[1], errors="coerce")
        # 분이 60 이상으로 계속 커지는 로거는 롤오버가 없으므로 그대로 둠
        if pd.to_numeric(p[0], errors="coerce").max() < 60:
            sec = _unroll(sec, 3600.0)
        print(f"  [ams] Time 형식 MM:SS.f 감지 → 경과시간으로 환산 "
              f"(예: '{t.iloc[0]}' ... '{t.iloc[-1]}', 총 {sec.max()/60:.1f}분)")
        return _to_hms(sec)

    # 4) 숫자 경과초
    num = pd.to_numeric(t, errors="coerce")
    if num.notna().mean() >= 0.5:
        print("  [ams] Time 이 숫자 — 경과초로 환산")
        return _to_hms(num)

    print(f"  [ams] Time 형식 판별 불가 — 원본 유지 (예: {t.iloc[0]})")
    return t


# ══════════════════════════════════════════════
#  MX100 전처리
# ══════════════════════════════════════════════
def preprocess_mx100(
    df_raw: pd.DataFrame,
    useless_columns: list,
) -> pd.DataFrame:
    """MX100 데이터 전처리."""
    if df_raw.empty:
        return pd.DataFrame({"Time": [0]})

    df = df_raw.copy()
    df.drop(index=0, inplace=True, errors="ignore")
    cols_to_drop = [df.columns[i] for i in useless_columns if i < len(df.columns)]
    df.drop(columns=cols_to_drop, inplace=True, errors="ignore")
    df.rename(columns={df.columns[0]: "Time"}, inplace=True)
    df.reset_index(drop=True, inplace=True)
    df = df.loc[:, ~df.columns.duplicated()]

    # [최적화] apply(pd.to_numeric) → 개별 컬럼 astype 시도
    num_cols = df.columns.difference(["Time"])
    for col in num_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# ══════════════════════════════════════════════
#  시간 동기화 & 병합
# ══════════════════════════════════════════════
def _robust_start_time(series):
    """기준 시작시각 — 시리즈 전체를 파싱해 첫 유효값을 사용.

    parse_flexible_datetime 은 문자열의 '00:00:00' 을 지우는 처리가 있어
    경과시간이 00:00:00 부터 시작하면 빈 문자열 → NaT 가 됨 (1행 병합 버그의 원인).
    """
    try:
        dt = pd.to_datetime(series, errors="coerce", format="mixed")
    except (TypeError, ValueError):
        dt = pd.to_datetime(series, errors="coerce")
    dt = dt.dropna()
    if len(dt):
        return dt.iloc[0]
    for v in series:
        t = parse_flexible_datetime(v)
        if pd.notna(t):
            return t
    return pd.NaT


def sync_and_merge(
    dfs: list[pd.DataFrame],
    data_time: str,
    df_br: pd.DataFrame,
    df_ams: pd.DataFrame,
    df_mx100: pd.DataFrame,
) -> pd.DataFrame:
    """
    여러 DataFrame을 기준 시간축(1초 간격)으로 리샘플링·병합.
    [v3 최적화] reindex를 한번에 처리, ref_df 이중 변환 제거.
    """
    ref_map = {"BR": df_br, "AMS": df_ams, "MX100": df_mx100}
    ref_df = ref_map.get(data_time, pd.DataFrame())
    # 지정된 기준 소스가 비어 있으면 사용 가능한 다른 소스로 자동 폴백
    if ref_df.empty or "Time" not in ref_df.columns:
        for alt in ["BR", "AMS", "MX100"]:
            cand = ref_map.get(alt, pd.DataFrame())
            if not cand.empty and "Time" in cand.columns:
                print(f"  [sync] 기준 시간축 '{data_time}' 없음 → '{alt}'로 자동 전환")
                data_time, ref_df = alt, cand
                break
    if ref_df.empty or "Time" not in ref_df.columns:
        print("  [sync] 사용 가능한 시간축 소스가 없음")
        return pd.DataFrame({"Time": pd.to_datetime([])})

    start_time = _robust_start_time(ref_df["Time"])
    print(f"  [sync] 기준시간: {start_time}, data_time={data_time} "
          f"(원본 첫 값 '{ref_df['Time'].iloc[0]}')")
    if pd.isna(start_time):
        raise ValueError(f"기준 시간축({data_time})의 Time 을 해석할 수 없음 — "
                         f"예: '{ref_df['Time'].iloc[0]}', '{ref_df['Time'].iloc[-1]}'")

    # 중복 제거 + 시간 변환
    clean_dfs = []
    for i, df in enumerate(dfs):
        if df.empty or "Time" not in df.columns:
            continue
        df = df.copy()
        df = df.drop_duplicates(subset="Time", keep="first").reset_index(drop=True)
        _align_datetime_to_base(df, start_time)

        # Time을 datetime으로 강제
        if not pd.api.types.is_datetime64_any_dtype(df["Time"]):
            df["Time"] = pd.to_datetime(df["Time"], errors="coerce")

        # NaT/중복 제거
        df = df.dropna(subset=["Time"]).reset_index(drop=True)
        df = df[~df["Time"].duplicated(keep="first")].reset_index(drop=True)

        if len(df) > 0:
            print(f"  [sync] df[{i}]: {len(df)}행 {len(df.columns)}열, Time={df['Time'].iloc[0]}~{df['Time'].iloc[-1]}")
            clean_dfs.append(df)

    if not clean_dfs:
        raise ValueError("시간 정리 후 남은 데이터가 없음 — 모든 소스의 Time 이 비었거나 변환에 실패")

    # end_time: 첫 번째 clean_df에서
    end_time = clean_dfs[0]["Time"].iloc[-1]

    # 1초 간격 시간축
    try:
        time_index = pd.date_range(start=start_time, end=end_time, freq="s", name="Time")
        print(f"  [sync] time_index: {len(time_index)}초 ({time_index[0]} ~ {time_index[-1]})")
        for di, cdf in enumerate(clean_dfs):
            lo, hi = cdf["Time"].iloc[0], cdf["Time"].iloc[-1]
            ov = int(((time_index >= lo) & (time_index <= hi)).sum())
            if ov == 0:
                print(f"  [sync] ⚠️ df[{di}] 시간 범위 {lo} ~ {hi} 가 기준 격자와 겹치지 않음 "
                      f"→ 이 소스는 전부 빈 값 (경과시간 vs 벽시계 시간 혼용 의심)")
            elif ov < len(time_index) * 0.5:
                print(f"  [sync] ⚠️ df[{di}] 기준 격자의 {ov/len(time_index)*100:.0f}%만 겹침")
    except Exception as e:
        print(f"  [sync] date_range 실패: {e}")
        raise ValueError(f"시간 격자 생성 실패 (시작={start_time}, 끝={end_time}): {e}") from e

    # reindex (각 df별 try/except)
    resampled = []
    for di, df in enumerate(clean_dfs):
        try:
            if not df["Time"].is_monotonic_increasing:
                df = df.sort_values("Time").reset_index(drop=True)
            rs = df.set_index("Time").reindex(time_index, method=None)
            rs = rs.ffill(limit=30).bfill(limit=30)
            resampled.append(rs)
        except Exception as e:
            print(f"  [sync] reindex 실패 df[{di}]: {e}")
            try:
                df = df.sort_values("Time")
                dummy = pd.DataFrame({"Time": time_index})
                merged_one = pd.merge_asof(dummy, df, on="Time", direction="nearest", tolerance=pd.Timedelta("30s"))
                resampled.append(merged_one.set_index("Time"))
                print(f"  [sync] df[{di}] merge_asof 폴백 성공")
            except Exception as e2:
                print(f"  [sync] df[{di}] 폴백도 실패: {e2}")

    if not resampled:
        raise ValueError("모든 소스의 재색인(1초 격자 맞춤)이 실패 — 위 [sync] 로그 확인")

    # 병합 (중복 컬럼 제거)
    merged = pd.concat(resampled, axis=1)
    merged = merged.loc[:, ~merged.columns.duplicated()]
    merged = merged.reset_index()

    # 숫자 변환 + 선형 보간 (NaN 있는 컬럼만, 배치 처리)
    num_cols = merged.columns.difference(["Time"])
    non_numeric = [c for c in num_cols if not pd.api.types.is_numeric_dtype(merged[c])]
    if non_numeric:
        merged[non_numeric] = merged[non_numeric].apply(pd.to_numeric, errors="coerce")

    has_nan = merged[num_cols].isnull().any()
    nan_cols = has_nan[has_nan].index.tolist()
    if nan_cols:
        merged[nan_cols] = merged[nan_cols].interpolate(
            method="linear", limit_direction="both")

    # 단조성 검증
    if not merged["Time"].is_monotonic_increasing:
        merged = merged.sort_values("Time").reset_index(drop=True)

    print(f"  [sync] 최종: {len(merged)}행×{len(merged.columns)}열")
    return merged


# ══════════════════════════════════════════════
#  시간 열 부가 (Time_sec, Time_min)
# ══════════════════════════════════════════════
def add_time_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Time 열 기준으로 Time_sec, Time_min 열 추가/재계산.

    Time 이 datetime 이 아닐 수도 있으므로(문자열 HH:MM:SS, 경과초 등)
    형식을 판별해 초 단위로 환산한다.
    """
    if "Time" not in df.columns or len(df) == 0:
        df["Time_sec"] = pd.Series(dtype=float)
        df["Time_min"] = pd.Series(dtype=float)
        return df

    s = df["Time"]

    if pd.api.types.is_datetime64_any_dtype(s):
        time_sec = (s - s.iloc[0]).dt.total_seconds()
    elif pd.api.types.is_timedelta64_dtype(s):
        time_sec = s.dt.total_seconds()
    elif pd.api.types.is_numeric_dtype(s):
        # 이미 경과 시간(초)로 들어온 경우
        time_sec = pd.to_numeric(s, errors="coerce").astype(float)
        print("  [time] Time 이 숫자형 — 경과초로 간주")
    else:
        conv = pd.to_datetime(s, errors="coerce")
        if conv.notna().sum() >= max(1, int(len(conv) * 0.5)):
            time_sec = (conv - conv.dropna().iloc[0]).dt.total_seconds()
            df["Time"] = conv
        else:
            num = pd.to_numeric(s, errors="coerce")
            if num.notna().sum() > 0:
                time_sec = num.astype(float)
                print("  [time] Time 파싱 실패 — 숫자 경과초로 대체")
            else:
                print("  [time] Time 파싱 불가 — 행 인덱스를 초로 사용")
                time_sec = pd.Series(range(len(df)), index=df.index, dtype=float)

    time_sec = time_sec.astype(float)
    first = time_sec.dropna().iloc[0] if time_sec.notna().any() else 0.0
    time_sec = time_sec - first
    time_min = (time_sec / 60).round(2)

    # 이미 있으면 재계산, 없으면 새로 삽입
    if "Time_sec" in df.columns:
        df["Time_sec"] = time_sec
    else:
        df.insert(min(1, len(df.columns)), "Time_sec", time_sec)
    if "Time_min" in df.columns:
        df["Time_min"] = time_min
    else:
        df.insert(min(2, len(df.columns)), "Time_min", time_min)
    return df
