"""
calculator.py — Stage 1: 병합 데이터의 기본 파생 컬럼 계산
- 압력 추정 (온도 → Psat)
- fTemp 건조도 감지
- 발산값 보정
- MX100 누락 컬럼 유도
"""
import numpy as np
import pandas as pd
from properties import get_props


# AMS 표준명 → 앱 표준명(그래프 카테고리·계산에서 쓰는 이름) 별칭
#   앱 이름이 없을 때만 AMS 값을 복사해서 채움 (기존 값은 절대 덮어쓰지 않음)
STD_ALIASES = {
    "T_Air_Eva_In":   ("Heatpump_DuctInTemp",),   # 증발기 공기 입구 (= 덕트 입구)
    "T_Air_Cond_Out": ("Heatpump_DuctOutTemp",),  # 응축기 공기 출구 (= 덕트 출구)
    "T_Eva_In":       ("Heatpump_EvaInTemp",),    # 증발기 냉매 입구
    "T_Eva_Out":      ("Heatpump_EvaOutTemp",),   # 증발기 냉매 출구
    "T_Comp_Body":    ("Heatpump_CompTemp",),     # 압축기 토출(바디)
}


def apply_std_aliases(df: pd.DataFrame, log: bool = True) -> pd.DataFrame:
    """앱 표준명이 없으면 AMS 표준명 값으로 채움. 추가된 별칭은 로그로 남김."""
    added = []
    for std, srcs in STD_ALIASES.items():
        if std in df.columns:
            continue
        for src in srcs:
            if src in df.columns:
                df[std] = df[src].values
                added.append(f"{std}←{src}")
                break
    if added and log:
        print(f"  [alias] 앱 표준명 별칭 {len(added)}개: {', '.join(added)}")
    return df


def run_stage1(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Stage 1 계산 실행.

    개별 계산에 필요한 컬럼이 없으면 그 계산만 건너뛰고 진행한다.
    (일부 소스만 병합된 경우에도 Merge 자체는 성공해야 함)
    """
    setup = cfg["processing"].get("setup_type", "BR")
    env = cfg["environment"]
    props = get_props(env["refrigerant"], env["patm"], env.get("backend", "HEOS"))

    def _try(label, fn):
        try:
            return fn()
        except KeyError as e:
            print(f"  [stage1] {label} 건너뜀 — 필요한 {e}")
        except Exception as e:
            print(f"  [stage1] {label} 실패 — {type(e).__name__}: {e}")
        return None

    # AMS 이름으로 들어온 온도를 앱 표준명으로도 제공 (그래프 카테고리·Calc 가 찾는 이름)
    df = apply_std_aliases(df)

    if setup == "MX100":
        out = _try("MX100 유도", lambda: _derive_missing_mx100(df))
        if out is not None:
            df = out
    elif setup == "BR":
        out = _try("발산값 보정", lambda: _clamp_divergent(df))
        if out is not None:
            df = out

        ft = _try("fTemp 계산", lambda: _calc_ftemp(df, cfg["ftemp"]))
        if ft is not None:
            df["fTemp_calc"] = ft

        out = _try("압력 추정", lambda: _calc_pressures(df, props, env["patm"]))
        if out is not None:
            df = out

        out = _try("토출 과열도", lambda: _calc_discharge_superheat(df, props))
        if out is not None:
            df = out

    return df


def _col(df, *names, required=True, default=None):
    """여러 이름 중 존재하는 첫 컬럼 반환. required=False면 default 반환."""
    for n in names:
        if n in df.columns:
            return df[n].values
    if required:
        raise KeyError(f"컬럼 없음: {names}")
    return default

def _calc_ftemp(df, cfg_ft):
    k1, k2, k3 = cfg_ft["k1"], cfg_ft["k2"], cfg_ft["k3"]
    delta = cfg_ft["auto_delta"]
    win = int(np.round(cfg_ft["moving_avg_window_min"] * 60, 0))

    need = [
        ("응축기 공기 출구", ("Heatpump_DuctOutTemp", "T_Air_Cond_Out")),
        ("증발기 공기 입구", ("Heatpump_DuctInTemp", "T_Air_Eva_In")),
        ("증발기 냉매 입구", ("Heatpump_EvaInTemp", "T_Eva_In")),
    ]
    missing = [f"{lb}({'/'.join(ns)})" for lb, ns in need
               if not any(n in df.columns for n in ns)]
    if missing:
        raise KeyError("컬럼: " + ", ".join(missing))

    raw = 10 * (k1 * _col(df, "Heatpump_DuctOutTemp", "T_Air_Cond_Out")
                - k2 * _col(df, "Heatpump_DuctInTemp", "T_Air_Eva_In")
                + k3 * _col(df, "Heatpump_EvaInTemp", "T_Eva_In")) + delta / 10
    return pd.Series(raw, index=df.index).rolling(window=win).mean().bfill().round(0)


def _calc_pressures(df, props, patm):
    print(f"  [_calc_pressures] 컬럼: P_Comp_Out={'Y' if 'P_Comp_Out' in df.columns else 'N'}, "
          f"T_Cond_M1={'Y' if 'T_Cond_M1' in df.columns else 'N'}")

    # ── 고압 (토출압) ──
    # T_Cond_M1이 있으면 항상 재계산 (가장 정확한 소스)
    if "T_Cond_M1" in df.columns:
        p = props.psat_t_barg(df["T_Cond_M1"].values)
        df["P_Comp_Out"] = p; df["P_Cond_Out"] = p
        print(f"  [P_Comp_Out] ← psat(T_Cond_M1), 범위: {p.min():.2f}~{p.max():.2f} barg")
    elif "P_Comp_Out" not in df.columns:
        # T_Cond_In → 과열이라 psat이 약간 높음 (근사)
        for c in ["T_Cond_In", "T_Comp_Out", "T_Comp_Body"]:
            if c in df.columns:
                p = props.psat_t_barg(df[c].values)
                df["P_Comp_Out"] = p; df["P_Cond_Out"] = p
                print(f"  ⚠️ [P_Comp_Out] ← psat({c}), 과열 근사! 범위: {p.min():.2f}~{p.max():.2f} barg")
                break
        else:
            print(f"  ❌ [P_Comp_Out] 계산 불가")
    else:
        print(f"  [P_Comp_Out] ← 기존값 유지, 범위: {df['P_Comp_Out'].min():.2f}~{df['P_Comp_Out'].max():.2f}")
    if "P_Cond_Out" not in df.columns and "P_Comp_Out" in df.columns:
        df["P_Cond_Out"] = df["P_Comp_Out"]

    # ── 저압 (흡입압) ──
    # T_Eva_In이 있으면 항상 재계산
    for c in ["T_Eva_In", "Heatpump_EvaInTemp"]:
        if c in df.columns:
            p = props.psat_t_barg(df[c].values)
            df["P_Comp_In"] = p; df["P_Eva_In"] = p
            print(f"  [P_Comp_In] ← psat({c}), 범위: {p.min():.2f}~{p.max():.2f} barg")
            break
    else:
        if "P_Comp_In" in df.columns:
            print(f"  [P_Comp_In] ← 기존값 유지, 범위: {df['P_Comp_In'].min():.2f}~{df['P_Comp_In'].max():.2f}")
        else:
            print(f"  ❌ [P_Comp_In] 계산 불가")
    if "P_Eva_In" not in df.columns and "P_Comp_In" in df.columns:
        df["P_Eva_In"] = df["P_Comp_In"]

    if "T_Air_Eva_In" not in df.columns and "Heatpump_DuctInTemp" in df.columns:
        df["T_Air_Eva_In"] = df["Heatpump_DuctInTemp"].values
    if "T_Air_Cond_Out" not in df.columns and "Heatpump_DuctOutTemp" in df.columns:
        df["T_Air_Cond_Out"] = df["Heatpump_DuctOutTemp"].values
    return df


def _calc_discharge_superheat(df, props):
    if "P_Comp_Out" in df.columns:
        t_dew = props.tsat_p(df["P_Comp_Out"].values)
        df["T_SH_CompOut"] = df["Heatpump_CompTemp"].values - t_dew
    else:
        df["T_SH_CompOut"] = 0
    return df


def _clamp_divergent(df):
    if "Heatpump_CompTemp" in df.columns:
        df.loc[df["Heatpump_CompTemp"] >= 200, "Heatpump_CompTemp"] = -1
    if "HP_SuperHeatDegree" in df.columns:
        bad = df["HP_SuperHeatDegree"] >= 1000
        if bad.any() and "Heatpump_EvaOutTemp" in df.columns:
            df.loc[bad, "HP_SuperHeatDegree"] = (
                df.loc[bad, "Heatpump_EvaOutTemp"] - df.loc[bad, "Heatpump_EvaInTemp"])
    return df


def _derive_missing_mx100(df):
    _map = {
        "T_Comp_In": "T_Eva_Out", "T_Comp_Out": "T_Cond_In",
        "Heatpump_CompTemp": "T_Comp_Out",
        "Heatpump_EvaInTemp": "T_Eva_In", "Heatpump_EvaOutTemp": "T_Eva_Out",
        "Heatpump_DuctInTemp": "T_Air_Eva_In", "Heatpump_DuctOutTemp": "T_Air_Cond_Out",
    }
    for tgt, src in _map.items():
        if tgt not in df.columns and src in df.columns:
            df[tgt] = df[src]
    if "HP_SuperHeatDegree" not in df.columns and "T_Eva_Out" in df.columns:
        df["HP_SuperHeatDegree"] = df["T_Eva_Out"] - df["T_Eva_In"]
    if "Heatpump_DryMotionInfo" not in df.columns:
        df["Heatpump_DryMotionInfo"] = 0
    if "Hz_Comp" in df.columns:
        df["HP_CompCurrentHz"] = df["Hz_Comp"] * 120 / 6 / 60
    return df
