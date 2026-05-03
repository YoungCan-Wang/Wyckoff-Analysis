"""
持仓健康诊断模块

复用 wyckoff_engine 已有的 L2 通道分类、L4 触发检测、L5 退出信号、
吸筹阶段分析、派发识别等能力，对任意持仓个股做结构化健康诊断。

用法:
    from core.holding_diagnostic import diagnose_holdings, format_diagnostic_text

    diagnostics = diagnose_holdings(
        holdings=[(code, name, cost), ...],
        df_map=df_map,
        bench_df=bench_df,
    )
    for d in diagnostics:
        print(format_diagnostic_text(d))
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import pandas as pd

from core.wyckoff_engine import (
    FunnelConfig,
    _analyze_accum_stage,
    _detect_evr,
    _detect_lps,
    _detect_sos,
    _detect_spring,
    _sorted_if_needed,
    layer2_strength_detailed,
    layer5_exit_signals,
)


@dataclass
class HoldingDiagnostic:
    """单只持仓的结构化健康诊断结果"""

    code: str
    name: str
    cost: float
    latest_close: float
    pnl_pct: float  # 浮盈亏 %

    # 均线结构
    ma5: float | None = None
    ma20: float | None = None
    ma50: float | None = None
    ma200: float | None = None
    ma_pattern: str = "数据不足"  # 多头排列 / 空头排列 / MA50>MA200 / MA50<MA200
    ma200_bias_pct: float | None = None

    # Wyckoff 定位
    l2_channel: str = "未入选"  # 主升通道 / 潜伏通道 / 吸筹通道 / ...
    accum_stage: str | None = None  # Accum_A / Accum_B / Accum_C
    track: str = "Unknown"  # Trend / Accum / Unknown
    l4_triggers: list[str] = field(default_factory=list)  # ["SOS", "Spring", ...]

    # 退出信号 (来自 layer5_exit_signals)
    exit_signal: str | None = None  # stop_loss / distribution_warning
    exit_price: float | None = None
    exit_reason: str = ""

    # 止损参考
    stop_loss_7pct: float = 0.0  # 成本 × 0.93
    stop_loss_status: str = "安全"  # 已穿止损 / 逼近止损 / 安全

    # 量能与振幅
    vol_ratio_20_60: float = 0.0
    range_60d_pct: float = 0.0
    ret_10d_pct: float = 0.0
    ret_20d_pct: float = 0.0
    from_year_high_pct: float = 0.0
    from_year_low_pct: float = 0.0

    # 综合评级
    health: str = "🟢健康"
    health_reasons: list[str] = field(default_factory=list)


# ── 通道 → 轨道映射 ──

_TREND_CHANNELS = {"主升通道", "点火破局"}
_ACCUM_CHANNELS = {"潜伏通道", "吸筹通道", "地量蓄势", "暗中护盘"}


def _classify_track(channel: str) -> str:
    """
    通道 → 轨道映射。
    引擎可能输出多标签（如 "主升通道+点火破局"），用子串匹配而非精确匹配。
    Trend 优先：只要包含任一趋势通道就归为 Trend。
    """
    for t in _TREND_CHANNELS:
        if t in channel:
            return "Trend"
    for a in _ACCUM_CHANNELS:
        if a in channel:
            return "Accum"
    return "Unknown"


def _calc_ma_pattern(
    close_val: float,
    ma50: float | None,
    ma200: float | None,
) -> str:
    if ma50 is None or ma200 is None:
        return "数据不足"
    if close_val > ma50 > ma200:
        return "多头排列"
    if close_val < ma50 < ma200:
        return "空头排列"
    if ma50 > ma200:
        return "MA50>MA200(偏强)"
    return "MA50<MA200(偏弱)"


def diagnose_one_stock(
    code: str,
    name: str,
    cost: float,
    df: pd.DataFrame,
    bench_df: pd.DataFrame | None = None,
    cfg: FunnelConfig | None = None,
) -> HoldingDiagnostic:
    """
    对单只股票执行全面 Wyckoff 健康诊断。

    Parameters
    ----------
    code : 6位股票代码
    name : 股票名称
    cost : 持仓成本价
    df   : 该股 320 日 OHLCV（需包含 date/open/high/low/close/volume 列）
    bench_df : 大盘基准 OHLCV（用于 L2 通道 RS 计算，可选）
    cfg  : FunnelConfig，默认使用全局默认值
    """
    if cfg is None:
        cfg = FunnelConfig()

    df_s = _sorted_if_needed(df).copy()
    close = pd.to_numeric(df_s["close"], errors="coerce")
    high = pd.to_numeric(df_s["high"], errors="coerce")
    low = pd.to_numeric(df_s["low"], errors="coerce")
    volume = pd.to_numeric(df_s["volume"], errors="coerce")

    latest_close = float(close.iloc[-1]) if not close.empty else 0.0
    pnl_pct = (latest_close - cost) / cost * 100.0 if cost > 0 else 0.0

    # ── 均线 ──
    ma5 = float(close.rolling(5).mean().iloc[-1]) if len(close) >= 5 else None
    ma20 = float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else None
    ma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
    ma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
    ma_pattern = _calc_ma_pattern(latest_close, ma50, ma200)
    ma200_bias = (latest_close - ma200) / ma200 * 100 if ma200 and ma200 > 0 else None

    # ── L2 通道分类（复用引擎函数）──
    # 诊断模式下关闭 RPS 过滤：单股 universe 做 RPS 排名无意义（百分位天然 100）。
    # 保留均线结构、RS 相对强度、量能形态等绝对指标做通道判断。
    l2_channel = "未入选"
    try:
        diag_cfg = replace(cfg, enable_rps_filter=False)
        passed, channel_map = layer2_strength_detailed([code], {code: df_s}, bench_df, diag_cfg)
        if code in channel_map:
            l2_channel = channel_map[code]
    except Exception:
        pass  # 数据不足时降级为"未入选"

    track = _classify_track(l2_channel)

    # ── 吸筹阶段分析 ──
    accum_stage = None
    try:
        accum_stage = _analyze_accum_stage(df_s, cfg)
    except Exception:
        pass

    # ── L4 触发检测 ──
    l4_triggers: list[str] = []
    try:
        if _detect_sos(df_s, cfg) is not None:
            l4_triggers.append("SOS")
        if _detect_spring(df_s, cfg) is not None:
            l4_triggers.append("Spring")
        if _detect_lps(df_s, cfg) is not None:
            l4_triggers.append("LPS")
        if _detect_evr(df_s, cfg) is not None:
            l4_triggers.append("EVR")
    except Exception:
        pass

    # ── L5 退出信号（复用引擎函数）──
    exit_signal = None
    exit_price = None
    exit_reason = ""
    try:
        accum_map = {code: accum_stage} if accum_stage else {}
        exit_signals = layer5_exit_signals([code], {code: df_s}, accum_map, cfg)
        sig = exit_signals.get(code, {})
        if sig:
            exit_signal = sig.get("signal")
            exit_price = sig.get("price")
            exit_reason = sig.get("reason", "")
    except Exception:
        pass

    # ── 止损参考 ──
    stop_loss_7pct = cost * 0.93
    if stop_loss_7pct <= 0:
        stop_status = "无成本价"
    elif latest_close <= stop_loss_7pct:
        stop_status = "已穿止损"
    elif (latest_close - stop_loss_7pct) / stop_loss_7pct < 0.02:
        stop_status = "逼近止损(<2%)"
    else:
        stop_status = "安全"

    # ── 量能与振幅 ──
    vol_20 = float(volume.tail(20).mean()) if len(volume) >= 20 else 0
    vol_60 = float(volume.tail(60).mean()) if len(volume) >= 60 else 0
    vol_ratio = vol_20 / vol_60 if vol_60 > 0 else 0

    h60 = float(high.tail(60).max()) if len(high) >= 60 else float(high.max())
    l60 = float(low.tail(60).min()) if len(low) >= 60 else float(low.min())
    range_60d = (h60 - l60) / l60 * 100 if l60 > 0 else 0

    ret_10d = (latest_close / float(close.iloc[-11]) - 1) * 100 if len(close) >= 11 else 0
    ret_20d = (latest_close / float(close.iloc[-21]) - 1) * 100 if len(close) >= 21 else 0

    lookback_250 = min(len(high), 250)
    h_year = float(high.tail(lookback_250).max())
    l_year = float(low.tail(lookback_250).min())
    from_year_high = (latest_close - h_year) / h_year * 100 if h_year > 0 else 0
    from_year_low = (latest_close - l_year) / l_year * 100 if l_year > 0 else 0

    # ── 综合评级 ──
    reasons: list[str] = []

    # 危险信号
    if stop_status == "已穿止损":
        reasons.append("已穿止损线(-7%)")
    if exit_signal == "stop_loss":
        reasons.append("结构止损（从高点回撤>10%）")
    if ma_pattern == "空头排列":
        reasons.append("均线空头排列")
    if range_60d > 50:
        reasons.append(f"60日振幅过大(>{range_60d:.0f}%)")
    if ret_10d < -15:
        reasons.append(f"近10日暴跌({ret_10d:+.1f}%)")

    # 警戒信号
    if exit_signal == "distribution_warning":
        reasons.append("高位派发预警")
    if stop_status == "逼近止损(<2%)":
        reasons.append("逼近止损线")
    if pnl_pct < -5:
        reasons.append("浮亏超过5%")
    if ma_pattern == "MA50<MA200(偏弱)" and pnl_pct < 0:
        reasons.append("均线偏弱且浮亏")
    if vol_ratio < 0.5:
        reasons.append("量能严重萎缩")

    # 正面信号
    positive = []
    if ma_pattern == "多头排列":
        positive.append("多头排列")
    if any(t in l2_channel for t in _TREND_CHANNELS):
        positive.append(f"L2通道:{l2_channel}")
    if l4_triggers:
        positive.append(f"L4信号:{'+'.join(l4_triggers)}")

    # 打分
    danger_count = sum(1 for r in reasons if any(k in r for k in ["已穿", "暴跌", "空头排列", "结构止损"]))
    warn_count = len(reasons) - danger_count

    if danger_count >= 1:
        health = "🔴危险"
    elif warn_count >= 2 or warn_count == 1 and not positive:
        health = "🟡警戒"
    else:
        health = "🟢健康"

    if positive and not reasons:
        reasons = positive  # 健康时展示正面因素

    return HoldingDiagnostic(
        code=code,
        name=name,
        cost=cost,
        latest_close=latest_close,
        pnl_pct=pnl_pct,
        ma5=ma5,
        ma20=ma20,
        ma50=ma50,
        ma200=ma200,
        ma_pattern=ma_pattern,
        ma200_bias_pct=ma200_bias,
        l2_channel=l2_channel,
        accum_stage=accum_stage,
        track=track,
        l4_triggers=l4_triggers,
        exit_signal=exit_signal,
        exit_price=exit_price,
        exit_reason=exit_reason,
        stop_loss_7pct=stop_loss_7pct,
        stop_loss_status=stop_status,
        vol_ratio_20_60=vol_ratio,
        range_60d_pct=range_60d,
        ret_10d_pct=ret_10d,
        ret_20d_pct=ret_20d,
        from_year_high_pct=from_year_high,
        from_year_low_pct=from_year_low,
        health=health,
        health_reasons=reasons,
    )


def diagnose_holdings(
    holdings: list[tuple[str, str, float]],
    df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame | None = None,
    cfg: FunnelConfig | None = None,
) -> list[HoldingDiagnostic]:
    """
    批量诊断持仓。

    Parameters
    ----------
    holdings : [(code, name, cost), ...]
    df_map   : {code: DataFrame} 每只股票的 OHLCV 数据
    bench_df : 大盘基准 OHLCV
    cfg      : FunnelConfig
    """
    results = []
    for code, name, cost in holdings:
        df = df_map.get(code)
        if df is None or df.empty:
            # 无数据时返回最小诊断
            results.append(
                HoldingDiagnostic(
                    code=code,
                    name=name,
                    cost=cost,
                    latest_close=0.0,
                    pnl_pct=0.0,
                    health="🔴危险",
                    health_reasons=["无法获取行情数据"],
                )
            )
            continue
        results.append(diagnose_one_stock(code, name, cost, df, bench_df, cfg))
    return results


def format_diagnostic_text(d: HoldingDiagnostic) -> str:
    """将诊断结果格式化为结构化文本，可注入 LLM prompt 或终端显示。"""
    lines = [
        f"{d.health} {d.code} {d.name} | 盈亏: {d.pnl_pct:+.2f}%",
        f"  成本: {d.cost:.2f} | 现价: {d.latest_close:.2f}",
    ]

    # 均线
    ma_parts = [f"均线: {d.ma_pattern}"]
    if d.ma200_bias_pct is not None:
        ma_parts.append(f"MA200乖离: {d.ma200_bias_pct:+.1f}%")
    lines.append("  " + " | ".join(ma_parts))

    # Wyckoff 定位
    wy_parts = [f"通道: {d.l2_channel}", f"轨道: {d.track}"]
    if d.accum_stage:
        wy_parts.append(f"阶段: {d.accum_stage}")
    if d.l4_triggers:
        wy_parts.append(f"L4: {'+'.join(d.l4_triggers)}")
    lines.append("  " + " | ".join(wy_parts))

    # 退出信号
    if d.exit_signal:
        exit_parts = [f"退出信号: {d.exit_signal}"]
        if d.exit_price is not None:
            exit_parts.append(f"触发价: {d.exit_price:.2f}")
        if d.exit_reason:
            exit_parts.append(d.exit_reason)
        lines.append("  " + " | ".join(exit_parts))

    # 止损
    lines.append(f"  止损(-7%): {d.stop_loss_7pct:.2f} → {d.stop_loss_status}")

    # 量能
    lines.append(
        f"  量比(20/60): {d.vol_ratio_20_60:.2f} | 60日振幅: {d.range_60d_pct:.1f}% | "
        f"近10日: {d.ret_10d_pct:+.1f}% | 近20日: {d.ret_20d_pct:+.1f}%"
    )

    # 评级理由
    if d.health_reasons:
        lines.append(f"  理由: {', '.join(d.health_reasons)}")

    return "\n".join(lines)


def format_diagnostic_for_llm(d: HoldingDiagnostic) -> str:
    """生成简洁版诊断文本，适合注入 Step4 LLM prompt 中。"""
    parts = [
        f"[持仓诊断] {d.health}",
        f"通道:{d.l2_channel} 轨道:{d.track}",
        f"均线:{d.ma_pattern}",
    ]
    if d.ma200_bias_pct is not None:
        parts.append(f"MA200乖离:{d.ma200_bias_pct:+.1f}%")
    if d.accum_stage:
        parts.append(f"阶段:{d.accum_stage}")
    if d.l4_triggers:
        parts.append(f"信号:{'+'.join(d.l4_triggers)}")
    if d.exit_signal:
        parts.append(f"退出:{d.exit_signal}")
        if d.exit_price is not None:
            parts.append(f"触发价:{d.exit_price:.2f}")
    parts.append(f"止损状态:{d.stop_loss_status}")
    parts.append(f"量比:{d.vol_ratio_20_60:.2f} 振幅:{d.range_60d_pct:.0f}%")
    if d.health_reasons:
        parts.append(f"原因:{','.join(d.health_reasons[:3])}")
    return " | ".join(parts)
