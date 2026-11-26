"""
postprocessor.py — 노이즈 제거, 스파이크 제거, 건조 구간 필터링

[v2 개선]
1. 노이즈: Savitzky-Golay 필터 (미분 보존, 지연 없음) + 기존 sliding window 병행
2. 스파이크: Hampel 필터 (MAD 기반, 이상치에 강건) + rolling 폴백
3. AI 옵션: Isolation Forest 이상치 탐지 (sklearn 설치 시)
4. EMA LPF는 기존 유지 (pandas ewm이 이미 최적)
"""
import numpy as np
import pandas as pd

try:
    from numba import njit
    HAS_NUMBA = True
except ImportError:
    HAS_NUMBA = False

try:
    from scipy.signal import savgol_filter
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

try:
    from sklearn.ensemble import IsolationForest
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False


# ══════════════════════════════════════════════
#  노이즈 제거 — Savitzky-Golay (비인과적, 지연 없음)
# ══════════════════════════════════════════════
def apply_noise_removal(
    df: pd.DataFrame,
    columns: list[str],
    window_size: int = 61,
    threshold: float = 5,
    method: str = "savgol",
) -> pd.DataFrame:
    """
    method:
      "savgol"  — Savitzky-Golay (기본, 미분 보존, 위상 지연 0)
      "sliding" — 기존 슬라이딩 윈도우 (하위 호환)
    """
    for col in columns:
        if not col or col not in df.columns:
            continue
        arr = df[col].values.astype(np.float64)

        if method == "savgol" and HAS_SCIPY and len(arr) > window_size:
            # window는 홀수여야 함
            win = window_size if window_size % 2 == 1 else window_size + 1
            win = min(win, len(arr) - 1)
            if win % 2 == 0:
                win += 1
            df[col] = savgol_filter(arr, window_length=win, polyorder=3)
        else:
            df[col] = _remove_noise_sliding(arr, window_size, threshold)
    return df


def _remove_noise_sliding(data, window_size, threshold):
    """기존 슬라이딩 윈도우 (Numba JIT 가속)."""
    if HAS_NUMBA:
        return _remove_noise_jit(data, window_size, threshold)
    n = len(data)
    clean = data.copy()
    if n < window_size:
        return clean
    current_sum = np.sum(clean[:window_size])
    for i in range(window_size, n):
        mean = current_sum / window_size
        new_val = mean if abs(data[i] - mean) > threshold else data[i]
        clean[i] = new_val
        current_sum += new_val - clean[i - window_size]
    return clean


if HAS_NUMBA:
    @njit(cache=True)
    def _remove_noise_jit(data, window_size, threshold):
        n = len(data)
        clean = data.copy()
        if n < window_size:
            return clean
        current_sum = 0.0
        for j in range(window_size):
            current_sum += clean[j]
        for i in range(window_size, n):
            mean = current_sum / window_size
            if abs(data[i] - mean) > threshold:
                new_val = mean
            else:
                new_val = data[i]
            clean[i] = new_val
            current_sum += new_val - clean[i - window_size]
        return clean


# ══════════════════════════════════════════════
#  스파이크 제거 — Hampel 필터 (MAD 기반, 강건)
# ══════════════════════════════════════════════
def smooth_spikes(
    df: pd.DataFrame,
    target_columns: list[str],
    window: int = 5,
    sigma: float = 3.0,
    method: str = "hampel",
) -> pd.DataFrame:
    """
    method:
      "hampel"  — Hampel 필터 (MAD 기반, 스파이크에 오염되지 않음)
      "rolling"  — 기존 rolling mean/std
      "iforest" — Isolation Forest (AI, sklearn 필요)
    """
    modified = []
    for col in target_columns:
        if col not in df.columns:
            continue

        if method == "hampel":
            outlier = _hampel_detect(df[col].values, window, sigma)
        elif method == "iforest" and HAS_SKLEARN:
            outlier = _iforest_detect(df[col].values)
        else:
            outlier = _rolling_detect(df[col], window, sigma)

        n = outlier.sum()
        if n > 0:
            print(f"  [{col}] 스파이크 {n}개 제거 ({method})")
            df.loc[outlier, col] = np.nan
            modified.append(col)

    if modified:
        df[modified] = df[modified].interpolate(method="linear", limit_direction="both")
    return df


def _hampel_detect(data: np.ndarray, window: int, sigma: float) -> np.ndarray:
    """
    Hampel 필터: 중앙값 + MAD(Median Absolute Deviation) 기반.
    rolling mean/std와 달리 이상치 자체에 오염되지 않음.
    """
    n = len(data)
    outlier = np.zeros(n, dtype=bool)
    k = 1.4826  # MAD → σ 변환 상수 (정규분포 가정)
    half = window // 2

    for i in range(half, n - half):
        win_data = data[i - half: i + half + 1]
        median = np.median(win_data)
        mad = k * np.median(np.abs(win_data - median))
        if mad < 1e-9:
            continue
        if abs(data[i] - median) > sigma * mad:
            outlier[i] = True
    return outlier


def _rolling_detect(series: pd.Series, window: int, sigma: float) -> np.ndarray:
    """기존 rolling mean/std 방식."""
    roll = series.rolling(window=window, center=True)
    rmean = roll.mean()
    rstd = roll.std()
    return ((series - rmean).abs() > (sigma * rstd)).values


def _iforest_detect(data: np.ndarray, contamination: float = 0.02) -> np.ndarray:
    """Isolation Forest 이상치 탐지 (AI 기반)."""
    X = data.reshape(-1, 1)
    mask_valid = ~np.isnan(X.ravel())
    result = np.zeros(len(data), dtype=bool)
    if mask_valid.sum() < 10:
        return result
    clf = IsolationForest(contamination=contamination, random_state=42, n_jobs=-1)
    X_valid = X[mask_valid]
    labels = clf.fit_predict(X_valid)
    result[mask_valid] = (labels == -1)
    return result


# ══════════════════════════════════════════════
#  접두사 기반 이상치 마스킹 + 선형 보간
# ══════════════════════════════════════════════
def smooth_by_column_prefix(df: pd.DataFrame, thresholds: dict) -> pd.DataFrame:
    sorted_prefixes = sorted(thresholds.keys(), key=len, reverse=True)
    col_bounds = {}
    for col in df.columns:
        for prefix in sorted_prefixes:
            if col.startswith(prefix):
                col_bounds[col] = tuple(thresholds[prefix])
                break

    for col, (lo, hi) in col_bounds.items():
        vals = df[col].values
        mask = (vals < lo) | (vals > hi)
        if mask.any():
            df.loc[mask, col] = np.nan

    if col_bounds:
        cols = list(col_bounds.keys())
        df[cols] = df[cols].interpolate(method="linear", limit_direction="both")
    return df


# ══════════════════════════════════════════════
#  EMA 로우패스 필터
# ══════════════════════════════════════════════
def apply_low_pass_filter(
    df: pd.DataFrame,
    columns: list[str],
    tau: float = 15,
    t_interval: float = 1,
) -> pd.DataFrame:
    alpha = t_interval / (tau + t_interval)
    for col in columns:
        if col and col in df.columns:
            df[col] = df[col].ewm(alpha=alpha, adjust=False).mean().round(2)
    return df


# ══════════════════════════════════════════════
#  건조 구간 필터링 (기존과 동일)
# ══════════════════════════════════════════════
def filter_time_interval(df, interval):
    return df[df["Time_sec"] % interval == 0].copy()

def dry_start_min(F, min_hz):
    """건조 시작 시각 [min] — 압축기 목표 주파수가 처음 min_hz 이상이 된 때 (예전 'Dry_Only' 의 잘라내기 기준).
    알 수 없으면 None. Visualization 의 '건조만' 정렬도 같은 기준."""
    if "HP_CompTargetHz" not in F.columns or "Time_min" not in F.columns:
        return None
    run = pd.to_numeric(F["HP_CompTargetHz"], errors="coerce") >= min_hz
    return float(F.loc[run, "Time_min"].iloc[0]) if run.any() else None


def dry_end_values(v) -> list:
    """설정 filtering.dry_end_subprocess → 숫자 목록 ([6], 6, "6", "6, 64" 모두 받음). 비우면 []."""
    if v is None:
        return []
    items = v if isinstance(v, (list, tuple)) else str(v).replace(";", ",").split(",")
    out = []
    for x in items:
        try:
            out.append(float(str(x).strip()))
        except ValueError:
            pass
    return out


def dry_end_min(F, start_min, end_vals, hold=3):
    """건조 끝 [min] — 건조 시작 뒤 SubProcess 가 end_vals 중 하나가 된 첫 시각 (냉각 단계 등). 못 찾으면 (None, None).
    hold 행 연속(데이터 끝이면 그만큼만) 그 값이어야 인정 — Merge 의 선형 보간이 코드 사이를 지나며
    한 행만 6 이 되는 경우(예: 3 → 64 사이 빈 구간)는 건너뜀."""
    if not end_vals or "SubProcess" not in F.columns or "Time_min" not in F.columns:
        return None, None
    sp = pd.to_numeric(F["SubProcess"], errors="coerce").to_numpy(float)
    hit = np.isin(np.round(sp, 6), np.round(np.asarray(end_vals, float), 6))
    if start_min is not None:
        hit &= F["Time_min"].to_numpy(float) > start_min
    n = len(hit)
    for i in np.flatnonzero(hit):
        j = min(n, i + hold)
        if hit[i:j].all():
            return float(F["Time_min"].iloc[i]), sp[i]
    return None, None


def subprocess_steps(F, max_steps=12, hold=3):
    """SubProcess 가 바뀐 순서 — '2(20.0분) → 3(25.1분) → 6(55.3분)' (Merge 로그에서 건조 끝 값 확인용).
    hold 행 넘게 이어진 정수 값만 셈 (보간으로 코드 사이를 지나간 값은 건너뜀)."""
    if "SubProcess" not in F.columns or "Time_min" not in F.columns:
        return ""
    sp = pd.to_numeric(F["SubProcess"], errors="coerce").to_numpy(float)
    tm = F["Time_min"].to_numpy(float)
    runs, i, n = [], 0, len(sp)
    while i < n:
        j = i
        while j + 1 < n and sp[j + 1] == sp[i]:
            j += 1
        v = sp[i]
        if np.isfinite(v) and abs(v - round(v)) < 1e-6 and (j - i + 1 >= hold or j == n - 1):
            if not runs or runs[-1][0] != v:
                runs.append((v, tm[i]))
        i = j + 1
    if not runs:
        return ""
    steps = [f"{v:g}({t:.1f}분)" for v, t in runs]
    more = len(steps) - max_steps
    return " → ".join(steps[:max_steps]) + (f" → … ({more}번 더 바뀜)" if more > 0 else "")


def filter_dry_cycle_br(df, filter_mode, min_hz, last_min, time_interval, end_vals=None):
    """세탁+건조를 남기고 건조가 끝난 뒤는 자름.
    · 건조 끝 = 건조 시작 뒤 SubProcess 가 end_vals(기본 6 = HPDRY_COOLING_STEP 냉각)가 된 때
    · SubProcess 로 못 찾으면 예전처럼 마지막 last_min 분 안에서 히트펌프가 멈춘 때
    filter_mode 는 예전 설정 호환용 — 'Dry_Only' 를 줘도 앞(세탁)은 자르지 않음
    (세탁/건조 구간은 Visualization 에서 골라 봄 — 건조만 보면 케이스마다 건조 시작 = 0분)."""
    F = filter_time_interval(df, time_interval)
    if "Time_min" not in F.columns or F.empty:
        return F
    end_min = F["Time_min"].iloc[-1]
    ds = dry_start_min(F, min_hz)
    de, de_val = dry_end_min(F, ds, end_vals or [])
    if de is not None:
        cycle_end, how_end = de, f"SubProcess = {de_val:g} 이 된 때"
    elif "HP_CompTargetHz" not in F.columns:
        print(f"  [filter] 건조 끝 자르기 건너뜀 — SubProcess {', '.join(f'{v:g}' for v in end_vals or []) or '-'} 도, "
              f"HP_CompTargetHz 도 없음 (전체 그대로)")
        return F
    else:
        # 예전 방식: 마지막 last_min 분 안에서 히트펌프가 멈춘 첫 시점
        need = ["HP_CompTargetHz", "HP_FanSpeed", "HP_EEV_Position"]
        missing = [c for c in need if c not in F.columns]
        if not missing:
            cond_stop = ((F["HP_CompTargetHz"] == 0)
                         & (F["HP_FanSpeed"] == 0)
                         & (F["HP_EEV_Position"] == 500))
        else:
            print(f"  [filter] 정지 판정 축소 — 없는 컬럼: {missing}")
            cond_stop = (F["HP_CompTargetHz"] == 0)
        tail = F[F["Time_min"] >= (end_min - last_min)]
        cond_stop_tail = cond_stop.reindex(tail.index, fill_value=False)
        cycle_end = tail.loc[cond_stop_tail, "Time_min"].iloc[0] if cond_stop_tail.any() else end_min
        if ds is not None and cycle_end < ds:
            cycle_end = end_min
        why = ("SubProcess 열 없음" if "SubProcess" not in F.columns
               else "설정 '건조 끝 SubProcess' 비어 있음" if not end_vals
               else f"건조 시작 뒤 SubProcess 가 {', '.join(f'{v:g}' for v in end_vals)} 이 된 적 없음")
        how_end = f"마지막 {last_min:g}분 안 히트펌프 정지 · {why}"
    out = F[F["Time_min"] < cycle_end].copy() if cycle_end < end_min else F.copy()
    steps = subprocess_steps(F)
    if steps:
        print(f"  [filter] SubProcess 순서: {steps}")
    print(f"  [filter] 세탁+건조 {len(out):,}행 · "
          + (f"건조 시작 {ds:.1f}분 (압축기 목표 주파수 ≥ {min_hz:g} Hz) · " if ds is not None
             else "건조 시작을 알 수 없음 (압축기가 안 돈 데이터) · ")
          + (f"건조 끝 {cycle_end:.1f}분 ({how_end}) → 그 뒤 {end_min - cycle_end:.1f}분 버림" if cycle_end < end_min
             else f"끝까지 ({end_min:.1f}분, {how_end})")
          + " — 세탁/건조는 Visualization 에서 골라 봄")
    return out

def filter_dry_cycle_mx100(df, time_interval):
    if "V1_Comp" in df.columns:
        F = df[df["V1_Comp"] > 0].copy()
        F["Time_min"] = F["Time_min"] - F["Time_min"].iloc[0]
        F["Time_sec"] = F["Time_sec"] - F["Time_sec"].iloc[0]
    else:
        F = df
    return F[F["Time_sec"] % time_interval == 0].copy()


# ══════════════════════════════════════════════
#  후처리 오케스트레이션
# ══════════════════════════════════════════════
def run_postprocessing(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    filt = cfg["filtering"]
    spike = cfg["spike_removal"]
    setup = cfg["processing"]["setup_type"]
    mode  = "Wash_and_Dry"            # 필터 모드 설정은 없앰 — 항상 세탁+건조 (구간은 Visualization 에서 선택)
    interval = cfg["processing"]["time_interval"]

    df = apply_noise_removal(
        df, filt["noise_columns"],
        window_size=filt.get("noise_window", 61),
        threshold=filt.get("noise_threshold", 5),
        method="savgol",
    )
    df = apply_low_pass_filter(df, filt["lpf_columns"], tau=filt.get("lpf_tau", 15))

    if setup == "BR":
        df = filter_dry_cycle_br(df, mode, filt["min_hz"], filt["last_min"], interval,
                                 dry_end_values(filt.get("dry_end_subprocess", [6])))
    else:
        df = filter_dry_cycle_mx100(df, interval)

    thresholds = cfg.get("outlier_thresholds", {})
    if thresholds:
        df = smooth_by_column_prefix(df, thresholds)

    df = smooth_spikes(
        df, spike["target_columns"],
        window=spike.get("window", 5), sigma=spike.get("sigma", 3.0),
        method="hampel",
    )
    return df
