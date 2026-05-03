"""
AI 研报 prompt 构建 + 报告解析工具。

供 step3_batch_report 和 step4_rebalancer 使用的报告构建、解析与分流逻辑。
"""

from __future__ import annotations

import json
import re

import pandas as pd

# ── 环境变量配置 ──

RECENT_DAYS = 15
HIGHLIGHT_DAYS = 60
HIGHLIGHT_PCT_THRESHOLD = 5.0
HIGHLIGHT_VOL_RATIO = 2.0
SUPPLY_HEAVY_VOL_RATIO = 1.5
SUPPLY_DRY_VOL_RATIO = 0.8
SUPPLY_TEST_MAX_ABS_PCT = 1.0
KEY_LEVEL_WINDOW = 20


# ── 报告解析工具 ──


def _extract_json_block(text: str) -> str:
    """从 markdown code block 或原始文本中提取 JSON 片段。"""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        return raw[start : end + 1]
    return raw


def _normalize_structured_pool(
    payload: dict,
    allowed_codes: set[str],
    code_name: dict[str, str],
) -> dict[str, list[dict[str, str]]]:
    """将结构化 JSON 报告规范化为 watch_pool / operation_pool 两个列表。"""

    def _collect_items(keys: tuple[str, ...]) -> list[dict]:
        out: list[dict] = []
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                out.extend(v for v in value if isinstance(v, dict))
            elif isinstance(value, dict):
                out.append(value)
        return out

    watch_raw = _collect_items(
        (
            "\u903b\u8f91\u7834\u4ea7",
            "\u50a8\u5907\u8425\u5730",
            "invalidated",
            "building_cause",
            "building_camp",
        )
    )
    ops_raw = _collect_items(
        (
            "operation_pool",
            "\u5904\u4e8e\u8d77\u8df3\u677f",
            "on_the_springboard",
            "springboard_pool",
        )
    )

    watch_items: list[dict[str, str]] = []
    op_items: list[dict[str, str]] = []
    seen_watch: set[str] = set()
    seen_ops: set[str] = set()

    if isinstance(watch_raw, list):
        for item in watch_raw:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code", "")).strip()
            if not re.fullmatch(r"\d{6}", code) or code not in allowed_codes:
                continue
            if code in seen_watch:
                continue
            seen_watch.add(code)
            watch_items.append(
                {
                    "code": code,
                    "name": str(item.get("name", "")).strip() or code_name.get(code, code),
                    "reason": str(item.get("reason", "")).strip(),
                    "condition": str(item.get("condition", "")).strip(),
                }
            )

    if isinstance(ops_raw, list):
        for item in ops_raw:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code", "")).strip()
            if not re.fullmatch(r"\d{6}", code) or code not in allowed_codes:
                continue
            if code in seen_ops:
                continue
            seen_ops.add(code)
            op_items.append(
                {
                    "code": code,
                    "name": str(item.get("name", "")).strip() or code_name.get(code, code),
                    "action": str(item.get("action", "")).strip(),
                    "reason": str(item.get("reason", "")).strip(),
                    "entry_condition": str(item.get("entry_condition", "")).strip(),
                }
            )

    return {
        "watch_pool": watch_items,
        "operation_pool": op_items,
    }


def _try_parse_structured_report(
    report: str,
    allowed_codes: set[str],
    code_name: dict[str, str],
) -> dict[str, list[dict[str, str]]] | None:
    """尝试将报告解析为结构化 JSON 格式。"""
    raw = (report or "").strip()
    if not raw:
        return None
    for candidate in [raw, _extract_json_block(raw)]:
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        normalized = _normalize_structured_pool(payload, allowed_codes, code_name)
        if normalized["watch_pool"] or normalized["operation_pool"]:
            return normalized
    return None


def _extract_ops_codes_from_markdown(
    report: str,
    allowed_codes: set[str],
) -> list[str]:
    """从纯 Markdown 文本中提取"处于起跳板"章节里的股票代码。"""
    lines = str(report or "").splitlines()
    in_ops_section = False
    ops_codes: list[str] = []
    stop_tokens = ("\u903b\u8f91\u7834\u4ea7", "\u50a8\u5907\u8425\u5730")
    start_tokens = ("\u5904\u4e8e\u8d77\u8df3\u677f",)

    for raw_line in lines:
        line = str(raw_line or "").strip()
        if not line:
            continue
        if line.startswith("#"):
            if any(token in line for token in start_tokens):
                in_ops_section = True
            elif any(token in line for token in stop_tokens):
                in_ops_section = False
        if not in_ops_section:
            continue
        for code in re.findall(r"\b\d{6}\b", line):
            if code in allowed_codes and code not in ops_codes:
                ops_codes.append(code)
    return ops_codes


def extract_operation_pool_codes(
    report: str,
    allowed_codes: list[str] | set[str] | tuple[str, ...],
) -> list[str]:
    """
    对外暴露：从 Step3 报告中提取"处于起跳板"代码。
    优先解析 Markdown 章节，若无则回退结构化 JSON 解析。
    """
    ordered_allowed = [str(c).strip() for c in allowed_codes if re.fullmatch(r"\d{6}", str(c).strip())]
    allowed_set = set(ordered_allowed)
    if not allowed_set:
        return []

    ops_codes = _extract_ops_codes_from_markdown(report, allowed_set)
    if not ops_codes:
        code_name = {c: c for c in allowed_set}
        structured = _try_parse_structured_report(
            report=report,
            allowed_codes=allowed_set,
            code_name=code_name,
        )
        if structured and structured.get("operation_pool"):
            for item in structured["operation_pool"]:
                code = str(item.get("code", "")).strip()
                if code in allowed_set and code not in ops_codes:
                    ops_codes.append(code)

    # 防御性去重，保持报告中的出现顺序
    deduped: list[str] = []
    seen: set[str] = set()
    for code in ops_codes:
        if code in allowed_set and code not in seen:
            seen.add(code)
            deduped.append(code)
    return deduped


# ── Payload 构建工具 ──


def _format_slice_date(value: object) -> str:
    s = str(value or "")
    return s[5:10] if len(s) >= 10 else s


def _build_supply_demand_summary(df: pd.DataFrame) -> str:
    """构建供求摘要文本。"""
    df_s = df.copy().sort_values("date").reset_index(drop=True)
    if df_s.empty:
        return ""

    close = pd.to_numeric(df_s.get("close"), errors="coerce")
    volume = pd.to_numeric(df_s.get("volume"), errors="coerce")
    vol_ma20 = volume.rolling(20).mean()
    df_s["pct_chg_calc"] = close.pct_change() * 100
    df_s["vol_ratio"] = volume / vol_ma20.replace(0, pd.NA)
    recent = df_s.tail(RECENT_DAYS).copy()

    pct = pd.to_numeric(recent.get("pct_chg_calc"), errors="coerce")
    vol_ratio = pd.to_numeric(recent.get("vol_ratio"), errors="coerce")
    down_heavy = recent[(pct < 0) & (vol_ratio >= SUPPLY_HEAVY_VOL_RATIO)]
    dry_pullback = recent[(pct < 0) & (vol_ratio <= SUPPLY_DRY_VOL_RATIO)]
    quiet_tests = recent[(pct.abs() <= SUPPLY_TEST_MAX_ABS_PCT) & (vol_ratio <= SUPPLY_DRY_VOL_RATIO)]
    breakout_days = recent[(pct >= HIGHLIGHT_PCT_THRESHOLD) & (vol_ratio >= HIGHLIGHT_VOL_RATIO)]

    key_window = min(max(KEY_LEVEL_WINDOW, 5), len(df_s))
    key_zone = df_s.tail(key_window)
    key_high = pd.to_numeric(key_zone.get("high"), errors="coerce").dropna()
    key_low = pd.to_numeric(key_zone.get("low"), errors="coerce").dropna()
    zone_text = ""
    if not key_high.empty and not key_low.empty:
        zone_text = f"，近{key_window}日区间=[{float(key_low.min()):.2f}, {float(key_high.max()):.2f}]"

    extra_tags: list[str] = []
    if not breakout_days.empty:
        extra_tags.append(f"最近爆量上攻={_format_slice_date(breakout_days.iloc[-1].get('date'))}")
    if not down_heavy.empty:
        extra_tags.append(f"最近供应放大={_format_slice_date(down_heavy.iloc[-1].get('date'))}")
    if not quiet_tests.empty:
        extra_tags.append(f"最近低量测试={_format_slice_date(quiet_tests.iloc[-1].get('date'))}")

    summary = (
        f"  [供求摘要] 近{RECENT_DAYS}日下跌放量{len(down_heavy)}次，"
        f"缩量回踩{len(dry_pullback)}次，低量测试{len(quiet_tests)}次"
        f"{zone_text}"
    )
    if extra_tags:
        summary += "，" + "，".join(extra_tags)
    return summary + "\n"


def generate_stock_payload(
    stock_code: str,
    stock_name: str,
    wyckoff_tag: str,
    df: pd.DataFrame,
    *,
    industry: str | None = None,
    market_cap_yi: float | None = None,
    avg_amount_20_yi: float | None = None,
    quant_score: float | None = None,
    industry_rank: int | None = None,
    policy_tag: str | None = None,
    sector_state: str | None = None,
    sector_state_code: str | None = None,
    sector_note: str | None = None,
    track: str | None = None,
    stage: str | None = None,
    funnel_score: float | None = None,
    exit_signal: str | None = None,
    exit_price: float | None = None,
    exit_reason: str | None = None,
    financial_metrics: dict | None = None,
) -> str:
    """
    将 320 个交易日 OHLCV 浓缩为发给 AI 的高密度文本。
    1. 大背景（MA50 / MA200 / 乖离率 / 市值 / 成交额）
    1.5 板块状态（轮动水温 + 证据）
    2. 近 15 日量价切片（放量比 + 涨跌幅 + 振幅 + 收盘位置）
    3. 近 60 日异动高光时刻
    """
    df = df.copy().sort_values("date").reset_index(drop=True)
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)
    amount = (
        pd.to_numeric(df["amount"], errors="coerce")
        if "amount" in df.columns
        else pd.Series(close * volume, index=df.index, dtype=float)
    )
    if amount.isna().all():
        amount = pd.Series(close * volume, index=df.index, dtype=float)
    df["ma50"] = close.rolling(50).mean()
    df["ma200"] = close.rolling(200).mean()
    df["vol_ma20"] = volume.rolling(20).mean()
    df["amount_ma20"] = amount.rolling(20).mean()
    df["pct_chg_calc"] = close.pct_change() * 100
    prev_close = close.shift(1)
    amplitude_base = prev_close.where(prev_close > 0, close.where(close > 0, pd.NA))
    df["amplitude_pct"] = ((high - low) / amplitude_base.replace(0, pd.NA) * 100).astype(float)
    span = (high - low).replace(0, pd.NA)
    df["close_pos_pct"] = ((close - low) / span * 100).clip(lower=0, upper=100).fillna(50.0)

    latest = df.iloc[-1]
    ma50_val = latest["ma50"]
    ma200_val = latest["ma200"]
    close_val = latest["close"]
    amount_ma20_val = latest.get("amount_ma20", pd.NA)
    market_cap_val = pd.to_numeric(market_cap_yi, errors="coerce")
    avg_amount_val = pd.to_numeric(avg_amount_20_yi, errors="coerce")
    if pd.isna(avg_amount_val):
        avg_amount_val = amount_ma20_val / 1e8 if pd.notna(amount_ma20_val) else pd.NA

    extra_parts: list[str] = []
    if pd.notna(ma50_val):
        extra_parts.append(f"MA50:{ma50_val:.2f}")
    if pd.notna(ma200_val):
        extra_parts.append(f"MA200:{ma200_val:.2f}")
    if pd.notna(ma200_val) and ma200_val > 0:
        bias_200 = (close_val - ma200_val) / ma200_val * 100
        extra_parts.append(f"年线乖离:{bias_200:.1f}%")
    if pd.notna(market_cap_val):
        extra_parts.append(f"市值:{float(market_cap_val):.0f}亿")
    if pd.notna(avg_amount_val):
        extra_parts.append(f"20日均成交:{float(avg_amount_val):.2f}亿")
    extra_text = ", ".join(extra_parts)
    if extra_text:
        background = f"  [结构背景] 现价:{close_val:.2f}, {extra_text}"
    else:
        background = f"  [结构背景] 现价:{close_val:.2f}"

    policy_prefix = f" {policy_tag}" if policy_tag else ""
    tag_text = ""
    raw_tag = str(wyckoff_tag or "").strip()
    if raw_tag:
        facts = []
        lowered = raw_tag.lower()
        neutral_map = [
            ("sos", "向上突破异动"),
            ("spring", "假跌破回收异动"),
            ("lps", "缩量回踩企稳异动"),
            ("evr", "放量滞涨背离异动"),
        ]
        for token, label in neutral_map:
            if token in lowered:
                facts.append(label)
        if facts:
            tag_text = f" | 量化初筛假设：{'/'.join(facts)}"
        else:
            tag_text = f" | 量化初筛假设：{raw_tag}"

    header = (
        f"\u2022 {stock_code} {stock_name}{policy_prefix}{tag_text}\n"
        f"  [价格锚点] 最新收盘价:{close_val:.2f}\n"
        f"{background}\n"
    )
    if stage:
        header += f"  [阶段假设] {stage}\n"
    if industry:
        header += f"  [行业/主营] {industry}\n"
    if sector_state:
        state_text = str(sector_state).strip()
        state_code_text = str(sector_state_code or "").strip()
        if state_code_text:
            state_text = f"{state_text} ({state_code_text})"
        header += f"  [板块状态] {state_text}\n"
    if sector_note:
        header += f"  [板块证据] {str(sector_note).strip()}\n"
    if exit_signal:
        exit_parts = [f"信号: {exit_signal}"]
        if exit_price is not None:
            exit_parts.append(f"触发价: {exit_price:.2f}")
        if exit_reason:
            exit_parts.append(f"原因: {exit_reason}")
        header += f"  [退出预警] {', '.join(exit_parts)}\n"

    if financial_metrics:
        fm = financial_metrics
        fm_parts = []
        _pct_keys = {"roe", "net_income_yoy", "gross_margin", "debt_to_asset_ratio"}
        for key, label in [
            ("eps_basic", "EPS"),
            ("roe", "ROE"),
            ("net_income_yoy", "净利润同比"),
            ("gross_margin", "毛利率"),
            ("debt_to_asset_ratio", "资产负债率"),
        ]:
            v = fm.get(key)
            if v is not None:
                try:
                    fv = float(v)
                    fm_parts.append(f"{label}: {fv:.1f}%" if key in _pct_keys else f"{label}: {fv:.2f}")
                except (ValueError, TypeError):
                    pass
        if fm_parts:
            header += f"  [基本面快照] {' | '.join(fm_parts)}\n"

    supply_summary = _build_supply_demand_summary(df)

    # 近 15 日量价切片
    recent = df.tail(RECENT_DAYS)
    recent_lines = ["  [近15日量价切片]:"]
    for _, row in recent.iterrows():
        vol_ratio = row["volume"] / row["vol_ma20"] if pd.notna(row["vol_ma20"]) and row["vol_ma20"] > 0 else 0
        pct = row["pct_chg_calc"] if pd.notna(row["pct_chg_calc"]) else 0
        amplitude_pct = row.get("amplitude_pct", pd.NA)
        close_pos_pct = row.get("close_pos_pct", pd.NA)
        date_str = str(row["date"])[5:10]
        amp_text = f"{float(amplitude_pct):.1f}%" if pd.notna(amplitude_pct) else "NA"
        close_pos_text = f"{float(close_pos_pct):.0f}%" if pd.notna(close_pos_pct) else "NA"
        recent_lines.append(
            f"    {date_str}: 收{row['close']:.2f} ({pct:+.1f}%), 振幅:{amp_text}, 收位:{close_pos_text}, 量比:{vol_ratio:.1f}x"
        )

    # 近 60 日异动高光
    tail60 = df.tail(HIGHLIGHT_DAYS)
    highlights = []
    for _, row in tail60.iterrows():
        pct = row["pct_chg_calc"] if pd.notna(row["pct_chg_calc"]) else 0
        vol_ratio = row["volume"] / row["vol_ma20"] if pd.notna(row["vol_ma20"]) and row["vol_ma20"] > 0 else 0
        if abs(pct) >= HIGHLIGHT_PCT_THRESHOLD or vol_ratio >= HIGHLIGHT_VOL_RATIO:
            date_str = str(row["date"])[5:10]
            tag_parts = []
            if abs(pct) >= HIGHLIGHT_PCT_THRESHOLD:
                tag_parts.append(f"涨跌{pct:+.1f}%")
            if vol_ratio >= HIGHLIGHT_VOL_RATIO:
                tag_parts.append(f"量比{vol_ratio:.1f}x")
            highlights.append(f"    {date_str}: 收{row['close']:.2f} ({', '.join(tag_parts)})")

    highlight_section = ""
    if highlights:
        highlight_section = "\n  [近60日异动高光]:\n" + "\n".join(highlights) + "\n"

    return header + "\n".join(recent_lines) + "\n" + supply_summary + highlight_section + "\n"


def build_track_user_message(
    track: str,
    benchmark_lines: list[str],
    payloads: list[str],
    *,
    compressed: bool,
    raw_count: int,
    selected_count: int,
    regime: str = "",
) -> str:
    """构建发送给 LLM 的轨道级用户消息。"""
    track_key = "Accum" if str(track).strip() == "Accum" else "Trend"
    regime_upper = str(regime or "").strip().upper() or "NEUTRAL"

    if track_key == "Trend":
        scope = (
            "[本轮分析范围]\n"
            "本轮仅分析 Trend轨（右侧主升 / 放量点火 / 突破组）。\n"
            "请重点审查是否存在高潮诱多、深水区反抽、爆量次日承接不足，以及看似突破实为派发等问题。"
        )
        if regime_upper == "CRASH":
            scope += (
                "\n\u26a0\ufe0f 当前 CRASH 环境，右侧突破全部视为诱多。\n"
                "Trend 轨所有标的一律归入逻辑破产或储备营地，不得放入起跳板。"
            )
        elif regime_upper == "RISK_OFF":
            scope += (
                "\n\u26a0\ufe0f 当前大盘处于弱势环境，右侧假突破概率极高。\n"
                "Trend 信号必须有突破日量比 >= 1.5x 且次日承接不回落，否则视为诱多归入逻辑破产。"
            )
    else:
        scope = (
            "[本轮分析范围]\n"
            "本轮仅分析 Accum轨（左侧潜伏 / Spring / LPS / Accum_C 组）。\n"
            "请重点审查供应是否真正枯竭；若下跌放量或支撑反复失守，应归入逻辑破产或储备营地。\n"
            "若出现长下影、高收位、放量拉回，不得机械判死刑，必须分辨是真Spring还是失败反抽。"
        )
        if regime_upper in ("RISK_OFF", "CRASH"):
            scope += (
                "\n\u26a0\ufe0f 当前大盘处于弱势环境，左侧抄底风险极高。\n"
                "Accum 信号必须同时满足：1) 缩量测试量比 < 0.6x 2) 支撑位至少 2 次测试未破。\n"
                "不满足的一律归入储备营地，不得放入起跳板。"
            )

    regime_hint = ""
    if regime_upper == "CRASH":
        regime_hint = "[仓位约束] 当前 CRASH 环境，禁止推荐起跳板，全部归入储备营地或逻辑破产。\n\n"
    elif regime_upper == "RISK_OFF":
        regime_hint = "[仓位约束] 当前 RISK_OFF 弱势环境，起跳板最多 1-2 只，必须有极强的量价确认。\n\n"
    elif regime_upper == "RISK_ON":
        regime_hint = "[仓位约束] 当前 RISK_ON 追涨期，反转率高，起跳板最多 2 只，必须有缩量回踩确认。\n\n"

    message = (
        ("{}\n\n".format("\n".join(benchmark_lines)) if benchmark_lines else "")
        + regime_hint
        + f"{scope}\n\n"
        + (
            (f"[候选说明] 本轮候选已从 {raw_count} 只压缩到 {selected_count} 只。\n\n")
            if compressed and raw_count > selected_count
            else ""
        )
        + "以下是本轮候选名单。\n"
        + "请做三阵营分流：1) 逻辑破产 2) 储备营地 3) 处于起跳板。\n"
        + "其中前两类属于非操作区，第三类才是可执行区。\n"
        + "输出必须包含这三个部分，且只能使用输入列表中的股票代码，不得遗漏或新增。\n\n"
        + "补充执行要求：\n"
        + "1) 买入触发必须包含量价确认条件（缩量回踩/拒绝下破）；放量下破必须取消买入。\n"
        + "2) 盘面解剖须结合振幅、收位与量比，说明洗盘/承接/冲高回落的博弈痕迹。\n"
        + "3) 【板块状态/证据】仅作行业参考，最终以个股量价结构定生死。\n\n"
        + "\n".join(payloads)
    )
    return message
