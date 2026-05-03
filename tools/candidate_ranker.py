"""
L3 候选排名工具 + 触发器标签常量。

综合动量、缩量、触发信号、板块共振等维度对候选股打分排名。
"""

from __future__ import annotations

import pandas as pd

from core.sector_rotation import SECTOR_STATE_SCORE_BONUS

# ── 全局常量 ──

TRIGGER_LABELS = {
    "sos": "SOS（量价点火）",
    "spring": "Spring（终极震仓）",
    "lps": "LPS（缩量回踩）",
    "evr": "Effort vs Result（放量不跌）",
}

TRIGGER_SHORT_LABELS = {
    "sos": "SOS",
    "spring": "Spring",
    "lps": "LPS",
    "evr": "EVR",
}

# 分组展示优先级：SOS > EVR > Spring > LPS
TRIGGER_GROUP_ORDER = ["sos", "evr", "spring", "lps"]
TRIGGER_GROUP_TITLES = {
    "sos": "⚡ SOS 量价点火",
    "evr": "📊 EVR 放量不跌",
    "spring": "🌀 Spring 终极震仓",
    "lps": "🔄 LPS 缩量回踩",
}


def calc_close_return_pct(close_series: pd.Series, lookback: int) -> float | None:
    """计算 close 序列的 N 日收益率（%）。"""
    s = pd.to_numeric(close_series, errors="coerce").dropna()
    lb = max(int(lookback), 1)
    if len(s) <= lb:
        return None
    start = float(s.iloc[-lb - 1])
    end = float(s.iloc[-1])
    if start <= 0:
        return None
    return (end - start) / start * 100.0


def rank_l3_candidates(
    l3_symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    sector_map: dict[str, str],
    triggers: dict[str, list[tuple[str, float]]],
    top_sectors: list[str],
    l2_channel_map: dict[str, str] | None = None,
    sector_rotation_map: dict[str, dict] | None = None,
) -> tuple[list[str], dict[str, float]]:
    """
    对 L3 股票做统一优先级排序，仅用于 AI 输入队列。

    打分权重：
      0.25 * q20 (20日动量) + 0.20 * q5 (5日) + 0.05 * q3 (3日)
      + 0.20 * dry_q (缩量程度) + 0.30 * trigger_q (Wyckoff 触发强度)
      + hot_bonus (热门板块) + sector_bonus (板块轮动状态)
    """
    if not l3_symbols:
        return ([], {})

    trigger_score_map: dict[str, float] = {}
    for key in TRIGGER_LABELS:
        for code, score in triggers.get(key, []):
            trigger_score_map[code] = max(trigger_score_map.get(code, 0.0), float(score))

    rows: list[dict] = []
    channel_map = l2_channel_map or {}
    rotation_map = sector_rotation_map or {}
    for code in l3_symbols:
        df = df_map.get(code)
        industry = str(sector_map.get(code, "") or "未知行业")
        l2_channel = str(channel_map.get(code, "") or "未标注通道")
        sector_state = str((rotation_map.get(industry, {}) or {}).get("state", "") or "")
        ret20 = None
        ret5 = None
        ret3 = None
        min_vol_ratio_5d = None
        if df is not None and not df.empty:
            s = df.sort_values("date")
            close = pd.to_numeric(s.get("close"), errors="coerce")
            volume = pd.to_numeric(s.get("volume"), errors="coerce")
            ret20 = calc_close_return_pct(close, 20)
            ret5 = calc_close_return_pct(close, 5)
            ret3 = calc_close_return_pct(close, 3)
            vol_ma20 = volume.rolling(20).mean()
            vol_ratio = volume / vol_ma20.replace(0, pd.NA)
            min_vol_ratio_5d = pd.to_numeric(vol_ratio.tail(5), errors="coerce").min()

        rows.append(
            {
                "code": code,
                "industry": industry,
                "ret20": ret20,
                "ret5": ret5,
                "ret3": ret3,
                "min_vol_ratio_5d": min_vol_ratio_5d,
                "trigger_score": float(trigger_score_map.get(code, 0.0)),
                "l2_channel": l2_channel,
                "sector_state": sector_state,
            }
        )

    rank_df = pd.DataFrame(rows)
    for col, fill_default in (("ret20", 0.0), ("ret5", 0.0), ("ret3", 0.0), ("min_vol_ratio_5d", 1.0)):
        rank_df[col] = pd.to_numeric(rank_df[col], errors="coerce")
        if rank_df[col].notna().any():
            rank_df[col] = rank_df[col].fillna(float(rank_df[col].median()))
        else:
            rank_df[col] = rank_df[col].fillna(fill_default)

    rank_df["q20"] = rank_df["ret20"].rank(pct=True, ascending=True, method="average")
    rank_df["q5"] = rank_df["ret5"].rank(pct=True, ascending=True, method="average")
    rank_df["q3"] = rank_df["ret3"].rank(pct=True, ascending=True, method="average")
    rank_df["dry_q"] = rank_df["min_vol_ratio_5d"].rank(pct=True, ascending=False, method="average")
    if rank_df["trigger_score"].nunique(dropna=False) > 1:
        rank_df["trigger_q"] = rank_df["trigger_score"].rank(pct=True, ascending=True, method="average")
    else:
        rank_df["trigger_q"] = rank_df["trigger_score"].apply(lambda x: 1.0 if float(x) > 0 else 0.0)

    hot_sector_set = set(top_sectors or [])
    # 板块快速轮动期 hot_bonus 降低：Top3 板块次日有 49% 概率反转
    rank_df["hot_bonus"] = rank_df["industry"].isin(hot_sector_set).astype(float) * 0.02
    rank_df["sector_bonus"] = rank_df["sector_state"].map(lambda x: float(SECTOR_STATE_SCORE_BONUS.get(str(x), 0.0)))
    # 权重重新分配：降低滞后动量(q20)权重，提升 Wyckoff 触发(trigger_q)权重，
    # 加入 3 日短期动量(q3) 适配板块快速轮动。
    rank_df["watch_score"] = (
        0.25 * rank_df["q20"]
        + 0.20 * rank_df["q5"]
        + 0.05 * rank_df["q3"]
        + 0.20 * rank_df["dry_q"]
        + 0.30 * rank_df["trigger_q"]
        + rank_df["hot_bonus"]
        + rank_df["sector_bonus"]
    )

    rank_df = rank_df.sort_values("watch_score", ascending=False).reset_index(drop=True)
    ranked_symbols = rank_df["code"].astype(str).tolist()
    score_map = {str(r["code"]): float(r["watch_score"]) for _, r in rank_df.iterrows()}
    return (ranked_symbols, score_map)
