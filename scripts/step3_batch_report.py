# -*- coding: utf-8 -*-
"""
阶段 3：批量 AI 研报
拉取选中股票的 OHLCV → 第五步特征工程 → AI 分析 → 飞书发送
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime

import pandas as pd


# Ensure project root is on sys.path for direct script invocation
if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.prompts import WYCKOFF_FUNNEL_SYSTEM_PROMPT
from integrations.fetch_a_share_csv import _resolve_trading_window, _fetch_hist
from integrations.llm_client import call_llm
from integrations.rag_veto import (
    get_rag_veto_runtime_status,
    is_rag_veto_enabled,
    run_negative_news_veto,
)
from integrations.data_source import (
    fetch_index_hist,
    fetch_market_cap_map,
    fetch_sector_map,
    fetch_stock_spot_snapshot,
)
from utils.feishu import send_feishu_notification
from utils.notify import send_wecom_notification, send_dingtalk_notification
from utils.trading_clock import CN_TZ, resolve_end_calendar_day
from core.wyckoff_engine import fit_ai_candidate_quotas, normalize_hist_from_fetch
from core.sector_rotation import SECTOR_STATE_LABELS

TRADING_DAYS = 320
GEMINI_MODEL_FALLBACK = ""
STEP3_REPORT_STYLE = (
    os.getenv("STEP3_REPORT_STYLE", "v3_three_camp").strip().lower()
)
_LEGACY_REPORT_STYLES = {
    "legacy",
    "legacy_dual_pool",
    "dual_pool",
    "classic",
    "v1",
}
STEP3_USE_LEGACY_REPORT = STEP3_REPORT_STYLE in _LEGACY_REPORT_STYLES
if STEP3_USE_LEGACY_REPORT:
    raise RuntimeError(
        "STEP3_REPORT_STYLE legacy 口径已禁用。"
        "请改为 v3_three_camp（或任意非 legacy 样式）以启用三阵营输出。"
    )
STEP3_MAX_AI_INPUT = int(
    os.getenv("STEP3_MAX_AI_INPUT", "0")
)
STEP3_DEFAULT_CONTEXT_CAP = max(
    int(os.getenv("STEP3_DEFAULT_CONTEXT_CAP", "8")),
    0,
)
STEP3_MAX_PER_INDUSTRY = int(os.getenv("STEP3_MAX_PER_INDUSTRY", "5"))
STEP3_EMPTY_COMPRESSION_FALLBACK_CAP = max(
    int(os.getenv("STEP3_EMPTY_COMPRESSION_FALLBACK_CAP", "8")),
    0,
)
STEP3_MAX_UPSTREAM_FILL = max(int(os.getenv("STEP3_MAX_UPSTREAM_FILL", "0")), 0)
STEP3_MAX_OUTPUT_TOKENS = 32768
DYNAMIC_MAINLINE_BONUS_RATE = 0.15
DYNAMIC_MAINLINE_TOP_N = 3
DYNAMIC_MAINLINE_MIN_CLUSTER = 2
STEP3_ENABLE_COMPRESSION = os.getenv("STEP3_ENABLE_COMPRESSION", "1").strip().lower() in {
    "1", "true", "yes", "on"
}
STEP3_ENABLE_RAG_VETO = os.getenv(
    "STEP3_ENABLE_RAG_VETO", "1"
).strip().lower() in {
    "1", "true", "yes", "on"
}
STEP3_SKIP_LLM = os.getenv("STEP3_SKIP_LLM", "0").strip().lower() in {
    "1", "true", "yes", "on"
}
STEP3_RESPECT_UPSTREAM_PRIORITY = os.getenv(
    "STEP3_RESPECT_UPSTREAM_PRIORITY", "1"
).strip().lower() in {"1", "true", "yes", "on"}


RECENT_DAYS = 15
HIGHLIGHT_DAYS = 60
HIGHLIGHT_PCT_THRESHOLD = 5.0
HIGHLIGHT_VOL_RATIO = 2.0
DEBUG_MODEL_IO = os.getenv("DEBUG_MODEL_IO", "").strip().lower() in {"1", "true", "yes", "on"}
DEBUG_MODEL_IO_FULL = os.getenv("DEBUG_MODEL_IO_FULL", "").strip().lower() in {"1", "true", "yes", "on"}
# 已按策略要求关闭“目标交易日强校验”，避免数据源时差导致候选被整批跳过。
ENFORCE_TARGET_TRADE_DATE = False
STEP3_ENABLE_SPOT_PATCH = os.getenv("STEP3_ENABLE_SPOT_PATCH", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
STEP3_SPOT_PATCH_RETRIES = int(os.getenv("STEP3_SPOT_PATCH_RETRIES", "2"))
STEP3_SPOT_PATCH_SLEEP = float(os.getenv("STEP3_SPOT_PATCH_SLEEP", "0.2"))
SUPPLY_HEAVY_VOL_RATIO = 1.5
SUPPLY_DRY_VOL_RATIO = 0.8
SUPPLY_TEST_MAX_ABS_PCT = 1.0
KEY_LEVEL_WINDOW = 20
TRACK_LABELS = {
    "Trend": "Trend轨（右侧主升 / 放量点火）",
    "Accum": "Accum轨（左侧潜伏 / Spring / LPS）",
}


def _dump_model_input(
    items: list[dict],
    model: str,
    system_prompt: str,
    user_message: str,
    *,
    name_hint: str = "",
) -> str:
    if not DEBUG_MODEL_IO:
        return ""

    logs_dir = os.getenv("LOGS_DIR", "logs")
    os.makedirs(logs_dir, exist_ok=True)
    hint = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name_hint or "").strip())[:32]
    suffix = f"_{hint}" if hint else ""
    path = os.path.join(
        logs_dir,
        f"step3_model_input_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{suffix}.txt",
    )
    symbols_line = ", ".join(f"{x.get('code', '')}" for x in items)
    body = (
        f"[step3] model={model}\n"
        f"[step3] symbol_count={len(items)}\n"
        f"[step3] symbols={symbols_line}\n"
        f"[step3] system_prompt_len={len(system_prompt)}\n"
        f"[step3] user_message_len={len(user_message)}\n"
    )
    if DEBUG_MODEL_IO_FULL:
        body += (
            "\n===== SYSTEM PROMPT =====\n"
            f"{system_prompt}\n"
            "\n===== USER MESSAGE =====\n"
            f"{user_message}\n"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"[step3] 模型输入已落盘: {path}")
    return path


def _send_input_preview(
    webhook_url: str,
    model: str,
    system_prompt: str,
    previews: list[dict],
    *,
    wecom_webhook: str = "",
    dingtalk_webhook: str = "",
) -> tuple[bool, str]:
    """
    预演模式：不调用模型，仅展示将发送给模型的输入内容。
    """
    total_selected = sum(int(x.get("selected_count", 0) or 0) for x in previews)
    blocks: list[str] = [
        "# 🧪 Step3 模型输入预演（未调用大模型）",
        "",
        f"- 目标模型: `{model}`",
        f"- 输入股票数: `{total_selected}`",
        "- 模式: `STEP3_SKIP_LLM=1`",
        "",
        "## SYSTEM PROMPT",
        "",
        "```text",
        system_prompt,
        "```",
        "",
    ]
    for idx, item in enumerate(previews, start=1):
        blocks.extend(
            [
                f"## USER MESSAGE {idx} / {len(previews)}",
                "",
                f"- 轨道: `{item.get('track', '')}`",
                f"- 股票数: `{item.get('selected_count', 0)}`",
                "",
                "```text",
                str(item.get("user_message", "") or ""),
                "```",
                "",
            ]
        )
    report = (
        "\n".join(blocks).rstrip() + "\n"
    )
    title = f"🧪 模型输入预演 {date.today().strftime('%Y-%m-%d')}"
    sent = send_feishu_notification(webhook_url, title, report) if webhook_url else True
    if wecom_webhook:
        send_wecom_notification(wecom_webhook, title, report)
    if dingtalk_webhook:
        send_dingtalk_notification(dingtalk_webhook, title, report)
    if not sent:
        print("[step3] 预演报告飞书推送失败")
        return (False, report)
    print(f"[step3] 预演报告发送成功，股票数={total_selected}")
    return (True, report)


def _has_required_sections(report: str) -> bool:
    text = (report or "").replace(" ", "")
    has_invalidated = "逻辑破产" in text
    has_building = "储备营地" in text
    has_springboard = "处于起跳板" in text
    return has_invalidated and has_building and has_springboard


def _repair_report_structure(
    report: str,
    model: str,
    api_key: str,
    selected_codes: list[str],
    *,
    provider: str = "gemini",
    llm_base_url: str = "",
) -> str:
    """
    当模型未给出可识别的分层结构时，做一次结构修复重写。
    """
    if not report.strip():
        return report

    repair_system = (
        "你是格式修复器。请将输入研报重排为标准 Markdown，"
        "必须包含三个章节：1) 逻辑破产 2) 储备营地 3) 处于起跳板。"
        "如果输入原本是旧口径的继续观察/立刻建仓，也要将其重排到上述三阵营中。"
        "明显假突破、派发、放量失守归入逻辑破产；其余未到起跳点的非操作标的归入储备营地。"
        "不可新增未在输入中出现的股票代码。"
    )
    repair_user = (
        "允许使用的股票代码："
        + ", ".join(selected_codes)
        + "\n\n以下是待修复文本：\n\n"
        + report
    )
    try:
        fixed = call_llm(
            provider=provider,
            model=model,
            api_key=api_key,
            system_prompt=repair_system,
            user_message=repair_user,
            base_url=llm_base_url or None,
            timeout=180,
            max_output_tokens=STEP3_MAX_OUTPUT_TOKENS,
        )
        return fixed or report
    except Exception as e:
        print(f"[step3] 结构修复失败: {e}")
        return report


def _build_fallback_sections(selected_df: pd.DataFrame) -> str:
    """
    最后兜底：确保飞书一定出现标准三阵营结果块。
    """
    if selected_df is None or selected_df.empty:
        return (
            "## 💀 逻辑破产（系统兜底）\n"
            "- 无（本轮无明确失效标的可判定）。\n\n"
            "## ⏳ 储备营地（系统兜底）\n"
            "- 无（本轮无可用候选）。\n\n"
            "## 🏹 处于起跳板（系统兜底）\n"
            "- 无（本轮无可操作标的）。"
        )

    lines = ["## 💀 逻辑破产（系统兜底）", "- 无（系统未判定明确逻辑破产标的）。", ""]
    lines.append("## ⏳ 储备营地（系统兜底）")
    for _, row in selected_df.iterrows():
        code = str(row.get("code", ""))
        name = str(row.get("name", code))
        tag = str(row.get("tag", ""))
        score = row.get("wyckoff_score")
        score_text = f"{float(score):.3f}" if pd.notna(score) else "-"
        lines.append(
            f"- `{code} {name}` | 标签: {tag or '-'} | 量化分: {score_text} | 仍需条件: 回踩结构战区时需缩量确认。"
        )

    lines.append("")
    lines.append("## 🏹 处于起跳板（系统兜底）")
    lines.append("- 无（模型未输出可操作标的，保持耐心观察）")
    return "\n".join(lines)


def _extract_json_block(text: str) -> str:
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
            "逻辑破产",
            "储备营地",
            "invalidated",
            "building_cause",
            "building_camp",
        )
    )
    ops_raw = _collect_items(
        (
            "operation_pool",
            "处于起跳板",
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
    """从纯 Markdown 文本中提取“处于起跳板”章节里的股票代码。"""
    lines = str(report or "").splitlines()
    in_ops_section = False
    ops_codes: list[str] = []
    stop_tokens = ("逻辑破产", "储备营地")
    start_tokens = ("处于起跳板",)

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
    对外暴露：从 Step3 报告中提取“处于起跳板”代码。
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





def _extract_codes_from_text(
    text: str,
    allowed_codes: set[str],
) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for code in re.findall(r"\b\d{6}\b", text or ""):
        if code not in allowed_codes or code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def _job_end_calendar_day() -> date:
    """
    定时任务统一口径：
    - 北京时间 17:00-23:59 走 T（当天）
    - 北京时间 00:00-16:59 走 T-1（上一自然日）
    """
    return resolve_end_calendar_day()


def _latest_trade_date_from_hist(df: pd.DataFrame) -> date | None:
    if df is None or df.empty or "date" not in df.columns:
        return None
    s = pd.to_datetime(df["date"], errors="coerce").dropna()
    if s.empty:
        return None
    return s.iloc[-1].date()


def _append_spot_bar_if_needed(
    code: str,
    df: pd.DataFrame,
    target_trade_date: date,
) -> tuple[pd.DataFrame, bool]:
    if not STEP3_ENABLE_SPOT_PATCH or df is None or df.empty:
        return (df, False)
    latest_trade_date = _latest_trade_date_from_hist(df)
    if latest_trade_date is None or latest_trade_date >= target_trade_date:
        return (df, False)
    if target_trade_date != datetime.now(CN_TZ).date():
        return (df, False)

    df_s = df.sort_values("date").reset_index(drop=True)
    last_close_series = pd.to_numeric(df_s.get("close"), errors="coerce").dropna()
    prev_close = float(last_close_series.iloc[-1]) if not last_close_series.empty else None

    for attempt in range(max(STEP3_SPOT_PATCH_RETRIES, 1)):
        snap = fetch_stock_spot_snapshot(code, force_refresh=attempt > 0)
        close_v = None if not snap else snap.get("close")
        if close_v is None or float(close_v) <= 0:
            if attempt < max(STEP3_SPOT_PATCH_RETRIES, 1) - 1:
                time.sleep(max(STEP3_SPOT_PATCH_SLEEP, 0.0))
            continue

        close_f = float(close_v)
        open_f = float(snap.get("open")) if snap and snap.get("open") is not None else close_f
        high_raw = float(snap.get("high")) if snap and snap.get("high") is not None else close_f
        low_raw = float(snap.get("low")) if snap and snap.get("low") is not None else close_f
        high_f = max(high_raw, open_f, close_f)
        low_f = min(low_raw, open_f, close_f)
        turnover_ok = bool(float(snap.get("turnover_unit_ok", 0.0))) if snap else False
        if turnover_ok:
            volume_f = float(snap.get("volume")) if snap.get("volume") is not None else 0.0
            amount_f = float(snap.get("amount")) if snap.get("amount") is not None else 0.0
        else:
            volume_f = 0.0
            amount_f = 0.0
        pct_f = float(snap.get("pct_chg")) if snap and snap.get("pct_chg") is not None else None
        if pct_f is None and prev_close and prev_close > 0:
            pct_f = (close_f - prev_close) / prev_close * 100.0

        new_row = {
            "date": target_trade_date.isoformat(),
            "open": open_f,
            "high": high_f,
            "low": low_f,
            "close": close_f,
            "volume": volume_f,
            "amount": amount_f,
            "pct_chg": pct_f if pct_f is not None else 0.0,
        }
        patched = pd.concat([df_s, pd.DataFrame([new_row])], ignore_index=True)
        patched = patched.sort_values("date").reset_index(drop=True)
        return (patched, True)
    return (df, False)


def _safe_return(series: pd.Series, lookback: int = 10) -> float | None:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) <= lookback:
        return None
    start = float(s.iloc[-lookback - 1])
    end = float(s.iloc[-1])
    if start == 0:
        return None
    return (end - start) / start * 100.0


def _resolve_bias_range(regime: str | None) -> tuple[float, float]:
    r = str(regime or "").upper()
    if r == "BLACK_SWAN":
        return (0.0, 15.0)
    if r == "CRASH":
        return (0.0, 20.0)
    if r == "RISK_ON":
        return (-5.0, 45.0)
    if r == "RISK_OFF":
        return (0.0, 25.0)
    return (0.0, 35.0)


def _format_mainline_tag(industry: str | None, is_hot: bool) -> str:
    if not is_hot or not industry:
        return ""
    return f"🔥 [当前资金最强主线: {industry}]"


def ultimate_compressor(
    candidates_df: pd.DataFrame,
    regime: str | None,
    bonus_rate: float = DYNAMIC_MAINLINE_BONUS_RATE,
    max_total: int = STEP3_MAX_AI_INPUT,
    max_per_industry: int = STEP3_MAX_PER_INDUSTRY,
) -> pd.DataFrame:
    """
    Step 4.5 终极压缩：动态乖离过滤 + 因子标准化 + 动态主线识别 + 行业上限。
    """
    if candidates_df is None or candidates_df.empty:
        return pd.DataFrame()

    df = candidates_df.copy()
    if max_total <= 0:
        max_total = len(df)
    df["code"] = df.get("code", "").astype(str).str.strip()
    df["bias_200"] = pd.to_numeric(df.get("bias_200"), errors="coerce")
    df["rs_10"] = pd.to_numeric(df.get("rs_10"), errors="coerce")
    df["min_vol_ratio_5d"] = pd.to_numeric(df.get("min_vol_ratio_5d"), errors="coerce")
    df["industry"] = df.get("industry", "").astype(str).str.strip()
    df.loc[df["industry"] == "", "industry"] = pd.NA

    # 先删脏数据：核心字段缺失直接淘汰
    df = df.dropna(subset=["bias_200", "rs_10", "min_vol_ratio_5d", "industry"])
    if df.empty:
        return pd.DataFrame()

    # 动态水温阈值
    bias_min, bias_max = _resolve_bias_range(regime)
    df = df[(df["bias_200"] >= bias_min) & (df["bias_200"] <= bias_max)]
    if df.empty:
        return pd.DataFrame()

    # 百分位因子分数
    df["rs_score"] = df["rs_10"].rank(pct=True, ascending=True, method="average")
    # 量比越小越好：ascending=False 使小值获得更高分位
    df["dry_score"] = df["min_vol_ratio_5d"].rank(
        pct=True, ascending=False, method="average"
    )
    df["base_wyckoff_score"] = 0.6 * df["rs_score"] + 0.4 * df["dry_score"]

    # 动态主线识别：候选池内“有集群且相对强度高”的行业
    industry_stats = (
        df.groupby("industry", as_index=False)
        .agg(stock_count=("code", "count"), avg_rs=("rs_score", "mean"))
    )
    valid_industry_stats = industry_stats[
        industry_stats["stock_count"] >= DYNAMIC_MAINLINE_MIN_CLUSTER
    ]
    hot_industries: set[str] = set()
    if not valid_industry_stats.empty:
        hot_industries = set(
            valid_industry_stats.nlargest(DYNAMIC_MAINLINE_TOP_N, "avg_rs")["industry"]
            .astype(str)
            .tolist()
        )
    df["is_hot_mainline"] = df["industry"].astype(str).isin(hot_industries)
    df["policy_tag"] = df.apply(
        lambda r: _format_mainline_tag(str(r.get("industry", "")), bool(r.get("is_hot_mainline"))),
        axis=1,
    )
    df["dynamic_bonus"] = df["is_hot_mainline"].map(
        lambda v: float(bonus_rate) if bool(v) else 0.0
    )
    df["wyckoff_score"] = df["base_wyckoff_score"] * (1.0 + df["dynamic_bonus"])

    # 先全局排序，再做行业拥挤度限制
    df = df.sort_values("wyckoff_score", ascending=False).copy()
    df["industry_rank"] = (
        df.groupby("industry")["wyckoff_score"]
        .rank(ascending=False, method="first")
        .astype(int)
    )
    df = df.groupby("industry", group_keys=False).head(max_per_industry)
    df = df.head(max_total).reset_index(drop=True)
    if hot_industries:
        print(f"[step3] 动态主线行业: {', '.join(sorted(hot_industries))}")
    else:
        print("[step3] 动态主线行业: 无（未形成有效行业集群）")
    return df


def _fallback_candidates_when_compression_empty(
    candidates_df: pd.DataFrame,
) -> pd.DataFrame:
    if candidates_df is None or candidates_df.empty:
        return pd.DataFrame()

    df = candidates_df.copy()
    df["wyckoff_score"] = pd.to_numeric(df.get("funnel_score"), errors="coerce")
    df = df.sort_values(
        by=["wyckoff_score", "rs_10", "min_vol_ratio_5d"],
        ascending=[False, False, True],
        na_position="last",
    ).reset_index(drop=True)
    if STEP3_EMPTY_COMPRESSION_FALLBACK_CAP > 0:
        df = df.head(STEP3_EMPTY_COMPRESSION_FALLBACK_CAP).reset_index(drop=True)
    return df


def _coerce_bool_like(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _resolve_step3_context_cap(raw_count: int) -> int:
    raw_n = max(int(raw_count), 0)
    if raw_n <= 0:
        return 0
    if STEP3_MAX_AI_INPUT > 0:
        return min(STEP3_MAX_AI_INPUT, raw_n)
    if STEP3_DEFAULT_CONTEXT_CAP > 0:
        return min(STEP3_DEFAULT_CONTEXT_CAP, raw_n)
    return raw_n


def _has_upstream_priority_context(candidates_df: pd.DataFrame) -> bool:
    if not STEP3_RESPECT_UPSTREAM_PRIORITY or candidates_df is None or candidates_df.empty:
        return False
    if "priority_score" in candidates_df.columns:
        if pd.to_numeric(candidates_df["priority_score"], errors="coerce").notna().any():
            return True
    if "selection_source" in candidates_df.columns:
        if candidates_df["selection_source"].astype(str).str.strip().ne("").any():
            return True
    return False


def _select_upstream_priority_candidates(
    candidates_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Respect the Funnel's upstream ordering and only do a conservative tail cut here.
    Step3 should not re-rank already-selected AI candidates into a different order.
    """
    if candidates_df is None or candidates_df.empty:
        return pd.DataFrame()

    df = candidates_df.copy()
    df["input_order"] = pd.to_numeric(df.get("input_order"), errors="coerce")
    df["input_order"] = df["input_order"].fillna(pd.Series(range(len(df)), index=df.index)).astype(int)
    if "selection_is_fill" in df.columns:
        df["selection_is_fill"] = df["selection_is_fill"].apply(_coerce_bool_like)
    else:
        df["selection_is_fill"] = False
    df["priority_score"] = pd.to_numeric(df.get("priority_score"), errors="coerce")
    df = df.sort_values(
        by=["selection_is_fill", "input_order"],
        ascending=[True, True],
        kind="stable",
    ).reset_index(drop=True)

    context_cap = _resolve_step3_context_cap(len(df))
    if context_cap <= 0 or len(df) <= context_cap:
        return df

    trend_total = int((df["track"] == "Trend").sum())
    accum_total = int((df["track"] == "Accum").sum())
    trend_cap, accum_cap = fit_ai_candidate_quotas(context_cap, trend_total, accum_total)

    core_df = df[~df["selection_is_fill"]].copy()
    fill_df = df[df["selection_is_fill"]].copy()

    selected_parts: list[pd.DataFrame] = []
    if trend_cap > 0:
        selected_parts.append(core_df[core_df["track"] == "Trend"].head(trend_cap))
    if accum_cap > 0:
        selected_parts.append(core_df[core_df["track"] == "Accum"].head(accum_cap))

    selected_df = (
        pd.concat(selected_parts, ignore_index=False)
        if selected_parts
        else df.iloc[0:0].copy()
    )
    selected_codes = set(selected_df["code"].astype(str).tolist())

    remaining_slots = max(context_cap - len(selected_df), 0)
    if remaining_slots > 0:
        core_remainder = core_df[~core_df["code"].astype(str).isin(selected_codes)]
        if not core_remainder.empty:
            extra_core = core_remainder.head(remaining_slots)
            selected_df = pd.concat([selected_df, extra_core], ignore_index=False)
            selected_codes.update(extra_core["code"].astype(str).tolist())
            remaining_slots = max(context_cap - len(selected_df), 0)

    if remaining_slots > 0 and STEP3_MAX_UPSTREAM_FILL > 0:
        fill_remainder = fill_df[~fill_df["code"].astype(str).isin(selected_codes)]
        if not fill_remainder.empty:
            fill_take = fill_remainder.head(min(remaining_slots, STEP3_MAX_UPSTREAM_FILL))
            selected_df = pd.concat([selected_df, fill_take], ignore_index=False)

    selected_df = selected_df.sort_values("input_order", kind="stable").reset_index(drop=True)
    return selected_df


def _format_slice_date(value: object) -> str:
    s = str(value or "")
    return s[5:10] if len(s) >= 10 else s


def _build_supply_demand_summary(df: pd.DataFrame) -> str:
    df_s = df.copy().sort_values("date").reset_index(drop=True)
    if df_s.empty:
        return ""

    close = pd.to_numeric(df_s.get("close"), errors="coerce")
    volume = pd.to_numeric(df_s.get("volume"), errors="coerce")
    vol_ma20 = volume.rolling(20).mean()
    # 先在完整 df 上计算指标，再切片——避免 pandas 索引对齐导致全 NaN
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


def _build_track_user_message(
    track: str,
    benchmark_lines: list[str],
    payloads: list[str],
    *,
    compressed: bool,
    raw_count: int,
    selected_count: int,
) -> str:
    track_key = "Accum" if str(track).strip() == "Accum" else "Trend"
    if track_key == "Trend":
        scope = (
            "[本轮分析范围]\n"
            "本轮仅分析 Trend轨（右侧主升 / 放量点火 / 突破组）。\n"
            "请重点审查是否存在高潮诱多、深水区反抽、爆量次日承接不足，以及看似突破实为派发等问题。"
        )
    else:
        scope = (
            "[本轮分析范围]\n"
            "本轮仅分析 Accum轨（左侧潜伏 / Spring / LPS / Accum_C 组）。\n"
            "请重点审查供应是否真正枯竭；若下跌放量或支撑反复失守，应归入逻辑破产或储备营地。若出现长下影、高收位、放量拉回，不得机械判死刑，必须分辨是真Spring还是失败反抽。"
        )

    message = (
        ("{}\n\n".format("\n".join(benchmark_lines)) if benchmark_lines else "")
        + f"{scope}\n\n"
        + (
            (
                f"[候选说明] 本轮候选已从 {raw_count} 只压缩到 {selected_count} 只。\n\n"
            )
            if compressed and raw_count > selected_count
            else ""
        )
        + "以下是本轮候选名单。\n"
        + "请做三阵营分流：1) 逻辑破产 2) 储备营地 3) 处于起跳板。\n"
        + "其中前两类属于非操作区，第三类才是可执行区。\n"
        + "输出必须包含这三个部分，且只能使用输入列表中的股票代码，不得遗漏或新增。\n\n"
        + "交易执行硬约束：\n"
        + "1) 禁止单点价格指令，必须给“结构战区(Action Zone) + 盘面确认条件(Tape Condition)”。\n"
        + "2) 战区需围绕每只股票的“价格锚点（最新收盘价）”描述，但不得刻舟求剑。\n"
        + "3) 买入触发必须包含量价确认条件（如缩量回踩/拒绝下破）；若放量下破，必须取消买入。\n"
        + "4) 强势突破标的必须给“防踏空策略”：开盘强势确认后可先用计划仓位1/3试单，其余等待二次确认。\n"
        + "5) 输入中的“量化初筛假设/阶段假设”只是程序的一阶假设，不是结论；若15日切片证据冲突，你必须直接推翻它。\n"
        + "6) 盘面解剖必须结合振幅、收位与量比，明确说明盘中洗盘、承接、冲高回落或拒绝下跌的博弈痕迹。\n"
        + "7) 次日计划优先用 Plan A / Plan B 条件树表达，不要写机械目标价。\n"
        + "8) 若输入中出现【板块状态】与【板块证据】，请将其视为行业层风向与资金节奏的参考，不要机械套用结论，最终仍以个股量价结构定生死。\n\n"
        + "\n".join(payloads)
    )
    return message


def _strip_report_title(text: str) -> str:
    lines = str(text or "").strip().splitlines()
    if lines and lines[0].lstrip().startswith("# "):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def _call_track_report(
    *,
    track: str,
    system_prompt: str,
    user_message: str,
    model: str,
    api_key: str,
    selected_codes: list[str],
    selected_df: pd.DataFrame,
    provider: str = "gemini",
    llm_base_url: str = "",
) -> tuple[bool, str, str]:
    report = ""
    used_model = ""
    models_to_try = [model]
    if GEMINI_MODEL_FALLBACK and GEMINI_MODEL_FALLBACK != model:
        models_to_try.append(GEMINI_MODEL_FALLBACK)

    for m in models_to_try:
        try:
            report = call_llm(
                provider=provider,
                model=m,
                api_key=api_key,
                system_prompt=system_prompt,
                user_message=user_message,
                base_url=llm_base_url or None,
                timeout=300,
                max_output_tokens=STEP3_MAX_OUTPUT_TOKENS,
            )
            used_model = m
            break
        except Exception as e:
            print(f"[step3] {track} 轨模型 {m} 失败: {e}")
            if m == models_to_try[-1]:
                return (False, "", "")

    if not _has_required_sections(report):
        print(f"[step3] {track} 轨首版研报缺少可识别分层章节，执行一次结构修复")
        report = _repair_report_structure(
            report=report,
            model=used_model or model,
            api_key=api_key,
            selected_codes=selected_codes,
            provider=provider,
            llm_base_url=llm_base_url,
        )
    if not _has_required_sections(report):
        print(f"[step3] {track} 轨结构修复后仍缺少关键章节，追加系统兜底分层")
        report = report.rstrip() + "\n\n" + _build_fallback_sections(selected_df)
    return (True, report, used_model or model)


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
) -> str:
    """
    第五步：将 320 个交易日 OHLCV 浓缩为发给 AI 的高密度文本。
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
        # Convert internal trigger tags to neutral fact-based labels
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
        f"• {stock_code} {stock_name}{policy_prefix}{tag_text}\n"
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


def run(
    symbols_info: list[dict] | list[str],
    webhook_url: str,
    api_key: str,
    model: str,
    benchmark_context: dict | None = None,
    *,
    notify: bool = True,
    provider: str = "gemini",
    llm_base_url: str = "",
    wecom_webhook: str = "",
    dingtalk_webhook: str = "",
) -> tuple[bool, str, str]:
    """
    拉取 OHLCV → 第五步特征工程 → AI 研报 → 飞书/企微/钉钉发送。
    symbols_info: list[{"code", "name", "tag"}] 或 list[str]（向后兼容）。
    """
    if not symbols_info:
        print("[step3] 无输入股票，跳过")
        return (True, "skipped_no_symbols", "")

    # 兼容旧调用（纯 str 列表）
    items: list[dict] = []
    for s in symbols_info:
        if isinstance(s, str):
            items.append({"code": s, "name": s, "tag": ""})
        else:
            items.append(s)

    print(f"[step3] AI 输入股票数={len(items)}（全量命中输入）")

    end_day = _job_end_calendar_day()
    window = _resolve_trading_window(end_calendar_day=end_day, trading_days=TRADING_DAYS)

    regime = (benchmark_context or {}).get("regime", "NEUTRAL")
    sector_rotation_ctx = (benchmark_context or {}).get("sector_rotation", {}) or {}
    sector_rotation_map = sector_rotation_ctx.get("state_map", {}) or {}
    sector_map = fetch_sector_map()
    market_cap_map = fetch_market_cap_map()
    benchmark_ret_10: float | None = None
    try:
        bench_df = fetch_index_hist("000001", window.start_trade_date, window.end_trade_date)
        benchmark_ret_10 = _safe_return(bench_df["close"], lookback=10)
    except Exception:
        benchmark_ret_10 = None

    failed: list[tuple[str, str]] = []
    candidate_rows: list[dict] = []
    code_to_df: dict[str, pd.DataFrame] = {}
    for item_order, item in enumerate(items):
        code = item["code"]
        name = item.get("name", code)
        tag = item.get("tag", "")
        industry = str(item.get("industry") or sector_map.get(code, "未知行业") or "未知行业").strip()
        rotation_info = sector_rotation_map.get(industry, {}) or {}
        sector_state_code = str(
            item.get("sector_state_code")
            or rotation_info.get("state", "")
            or "NEUTRAL_MIXED"
        ).strip()
        sector_state = str(
            item.get("sector_state")
            or rotation_info.get("label", "")
            or SECTOR_STATE_LABELS.get("NEUTRAL_MIXED", "中性混沌")
        ).strip()
        sector_note = str(item.get("sector_note") or rotation_info.get("note", "") or "").strip()
        try:
            df_raw = _fetch_hist(code, window, "qfq")
            df = normalize_hist_from_fetch(df_raw)
            if ENFORCE_TARGET_TRADE_DATE:
                latest_trade_date = _latest_trade_date_from_hist(df)
                if latest_trade_date != window.end_trade_date:
                    df, patched = _append_spot_bar_if_needed(
                        code,
                        df,
                        window.end_trade_date,
                    )
                    if patched:
                        latest_trade_date = _latest_trade_date_from_hist(df)
                        print(f"[step3] {code} 实时快照补偿成功")
                if latest_trade_date != window.end_trade_date:
                    failed.append(
                        (
                            code,
                            f"latest_trade_date={latest_trade_date}, target_trade_date={window.end_trade_date}",
                        )
                    )
                    continue
            code_to_df[code] = df

            close = pd.to_numeric(df["close"], errors="coerce")
            volume = pd.to_numeric(df["volume"], errors="coerce")
            amount = (
                pd.to_numeric(df["amount"], errors="coerce")
                if "amount" in df.columns
                else pd.Series(close * volume, index=df.index, dtype=float)
            )
            if amount.isna().all():
                amount = pd.Series(close * volume, index=df.index, dtype=float)
            ma200 = close.rolling(200).mean()
            latest_close = close.iloc[-1] if len(close) else pd.NA
            latest_ma200 = ma200.iloc[-1] if len(ma200) else pd.NA
            bias_200 = pd.NA
            if pd.notna(latest_close) and pd.notna(latest_ma200) and float(latest_ma200) != 0:
                bias_200 = (float(latest_close) - float(latest_ma200)) / float(latest_ma200) * 100.0

            stock_ret_10 = _safe_return(close, lookback=10)
            rs_10 = stock_ret_10
            if stock_ret_10 is not None and benchmark_ret_10 is not None:
                rs_10 = stock_ret_10 - benchmark_ret_10

            vol_ma20 = volume.rolling(20).mean()
            amount_ma20 = amount.rolling(20).mean()
            vol_ratio = volume / vol_ma20.replace(0, pd.NA)
            min_vol_ratio_5d = pd.to_numeric(vol_ratio.tail(5), errors="coerce").min()
            avg_amount_20_yi = (
                float(amount_ma20.iloc[-1]) / 1e8
                if len(amount_ma20) and pd.notna(amount_ma20.iloc[-1])
                else pd.NA
            )

            candidate_rows.append(
                {
                    "code": code,
                    "name": name,
                    "input_order": item_order,
                    "tag": tag,
                    "track": str(item.get("track", "")).strip(),
                    "stage": str(item.get("stage", "")).strip(),
                    "funnel_score": pd.to_numeric(item.get("score"), errors="coerce"),
                    "priority_score": pd.to_numeric(item.get("priority_score"), errors="coerce"),
                    "priority_rank": pd.to_numeric(item.get("priority_rank"), errors="coerce"),
                    "selection_source": str(item.get("selection_source", "") or "").strip(),
                    "selection_is_fill": _coerce_bool_like(item.get("selection_is_fill")),
                    "exit_signal": str(item.get("exit_signal", "")).strip(),
                    "exit_price": pd.to_numeric(item.get("exit_price"), errors="coerce"),
                    "exit_reason": str(item.get("exit_reason", "")).strip(),
                    "industry": industry,
                    "sector_state": sector_state,
                    "sector_state_code": sector_state_code,
                    "sector_note": sector_note,
                    "market_cap_yi": pd.to_numeric(market_cap_map.get(code), errors="coerce"),
                    "avg_amount_20_yi": avg_amount_20_yi,
                    "bias_200": bias_200,
                    "rs_10": rs_10,
                    "min_vol_ratio_5d": min_vol_ratio_5d,
                }
            )
        except Exception as e:
            failed.append((code, str(e)))

    if not candidate_rows:
        if failed:
            detail = ", ".join(f"{s}({e})" for s, e in failed)
            print(f"[step3] OHLCV 全部拉取失败: {detail}")
            return (False, "data_all_failed", "")
        return (True, "no_data_but_no_error", "")

    candidates_df = pd.DataFrame(candidate_rows)
    candidates_df["code"] = candidates_df["code"].astype(str).str.strip()
    candidates_df["input_order"] = pd.to_numeric(candidates_df.get("input_order"), errors="coerce")
    candidates_df["input_order"] = candidates_df["input_order"].fillna(
        pd.Series(range(len(candidates_df)), index=candidates_df.index)
    ).astype(int)
    candidates_df["track"] = candidates_df.get("track", "").astype(str).str.strip()
    candidates_df.loc[~candidates_df["track"].isin(["Trend", "Accum"]), "track"] = "Trend"
    candidates_df["policy_tag"] = ""
    selected_df = candidates_df.copy()
    selected_df["wyckoff_score"] = pd.to_numeric(
        selected_df.get("priority_score"),
        errors="coerce",
    )
    selected_df["wyckoff_score"] = selected_df["wyckoff_score"].where(
        selected_df["wyckoff_score"].notna(),
        pd.to_numeric(selected_df.get("funnel_score"), errors="coerce"),
    )
    selected_df["industry_rank"] = pd.NA
    effective_context_cap = _resolve_step3_context_cap(len(candidates_df))

    if _has_upstream_priority_context(candidates_df):
        selected_df = _select_upstream_priority_candidates(candidates_df)
        fill_count = int(selected_df.get("selection_is_fill", pd.Series(dtype=bool)).sum())
        track_counts = (
            selected_df["track"].value_counts().to_dict()
            if "track" in selected_df.columns
            else {}
        )
        print(
            f"[step3] 尊重上游优先级收口: raw={len(candidates_df)} -> selected={len(selected_df)} "
            f"(cap={effective_context_cap}, Trend={int(track_counts.get('Trend', 0))}, "
            f"Accum={int(track_counts.get('Accum', 0))}, fills={fill_count})"
        )
    elif STEP3_ENABLE_COMPRESSION:
        compressed_df = ultimate_compressor(
            candidates_df,
            regime=regime,
            bonus_rate=DYNAMIC_MAINLINE_BONUS_RATE,
            max_total=effective_context_cap,
            max_per_industry=STEP3_MAX_PER_INDUSTRY,
        )
        if compressed_df.empty:
            selected_df = _fallback_candidates_when_compression_empty(candidates_df)
            print(
                "[step3] 压缩器结果为空，回退为受控候选列表 "
                f"(fallback_cap={STEP3_EMPTY_COMPRESSION_FALLBACK_CAP}, selected={len(selected_df)})"
            )
        else:
            selected_df = compressed_df
        print(
            f"[step3] 候选压缩已启用: raw={len(candidates_df)} -> selected={len(selected_df)} "
            f"(regime={regime}, max_total={effective_context_cap}, max_per_industry={STEP3_MAX_PER_INDUSTRY})"
        )
    else:
        print(f"[step3] 候选压缩未启用: selected=全量{len(selected_df)}")

    if effective_context_cap > 0 and len(selected_df) > effective_context_cap:
        before_n = len(selected_df)
        selected_df = selected_df.head(effective_context_cap).reset_index(drop=True)
        print(
            f"[step3] 上下文硬上限生效: selected {before_n} -> {len(selected_df)} "
            f"(effective_context_cap={effective_context_cap}, env_STEP3_MAX_AI_INPUT={STEP3_MAX_AI_INPUT}, "
            f"default_cap={STEP3_DEFAULT_CONTEXT_CAP})"
        )

    selected_df["wyckoff_score"] = pd.to_numeric(
        selected_df.get("priority_score"),
        errors="coerce",
    )
    selected_df["wyckoff_score"] = selected_df["wyckoff_score"].where(
        selected_df["wyckoff_score"].notna(),
        pd.to_numeric(selected_df.get("funnel_score"), errors="coerce"),
    )
    if "industry_rank" not in selected_df.columns:
        selected_df["industry_rank"] = pd.NA

    # P2: RAG 防雷（负面新闻关键词 veto）
    # 注意：RAG 永远在压缩/硬上限之后执行，确保筛查集合已被有效上下文 cap 收口。
    rag_veto_lines: list[str] = []
    rag_veto_preview = ""
    rag_skip_reason = ""
    if STEP3_ENABLE_RAG_VETO and not selected_df.empty:
        rag_status = get_rag_veto_runtime_status()
        if not bool(rag_status.get("enabled")):
            print("[step3][rag] 已关闭（RAG_VETO_ENABLED=0）")
            rag_skip_reason = "RAG_VETO_ENABLED=0"
        elif not bool(rag_status.get("has_provider")):
            print("[step3][rag] 跳过：未配置 TAVILY_API_KEY/SERPAPI_API_KEY")
            rag_skip_reason = "未配置 TAVILY_API_KEY/SERPAPI_API_KEY"
        else:
            rag_inputs = [
                {"code": str(r.get("code", "")).strip(), "name": str(r.get("name", ""))}
                for _, r in selected_df.iterrows()
            ]
            provider_text = (
                f"tavily={bool(rag_status.get('tavily_configured'))}, "
                f"serpapi={bool(rag_status.get('serpapi_configured'))}"
            )
            print(
                "[step3][rag] 启动："
                f"candidates={len(rag_inputs)}, providers=({provider_text}), "
                f"lookback_days={rag_status.get('lookback_days')}, "
                f"max_results={rag_status.get('max_results')}, "
                f"workers={rag_status.get('max_workers')}"
            )

            veto_map = run_negative_news_veto(rag_inputs)
            vetoed_codes: list[str] = []
            scanned_n = len(veto_map)
            external_ok_n = 0
            relevant_n = 0
            semantic_checked_n = 0
            error_n = 0
            source_counts = {"tavily": 0, "serpapi": 0, "none": 0}

            for code, result in veto_map.items():
                src = str(result.search_source or "").strip().lower()
                if src == "tavily":
                    source_counts["tavily"] += 1
                elif src == "serpapi":
                    source_counts["serpapi"] += 1
                else:
                    source_counts["none"] += 1

                if int(result.raw_result_count or 0) > 0:
                    external_ok_n += 1
                if int(result.relevant_result_count or 0) > 0:
                    relevant_n += 1
                if bool(result.semantic_checked):
                    semantic_checked_n += 1
                if result.error:
                    error_n += 1

                hit_text = "、".join(result.hits[:5]) if result.hits else "-"
                print(
                    "[step3][rag] "
                    f"{code} source={result.search_source or '-'} "
                    f"raw={int(result.raw_result_count or 0)} "
                    f"relevant={int(result.relevant_result_count or 0)} "
                    f"hits={hit_text} "
                    f"veto={bool(result.veto)} "
                    f"semantic_checked={bool(result.semantic_checked)} "
                    f"elapsed_ms={int(result.elapsed_ms or 0)}"
                    + (f" err={result.error}" if result.error else "")
                )

                if result.veto:
                    vetoed_codes.append(code)
                    ev_text = f" | 证据: {result.evidence[0]}" if result.evidence else ""
                    semantic_text = ""
                    if result.semantic_checked:
                        semantic_text = (
                            f" | 语义判定: 极端负面={result.semantic_negative}"
                            + (f"({result.semantic_reason})" if result.semantic_reason else "")
                        )
                    rag_veto_lines.append(
                        f"- {code} {result.name}: 命中 {hit_text if hit_text != '-' else '负面关键词'}{semantic_text}{ev_text}"
                    )

            rag_summary_lines = [
                f"- 扫描股票: {scanned_n}",
                f"- 外部检索成功: {external_ok_n}/{scanned_n}" if scanned_n else "- 外部检索成功: 0/0",
                f"- 有效相关新闻: {relevant_n}/{scanned_n}" if scanned_n else "- 有效相关新闻: 0/0",
                (
                    f"- 来源分布: tavily={source_counts['tavily']}, "
                    f"serpapi={source_counts['serpapi']}, none={source_counts['none']}"
                ),
                f"- 语义二判执行: {semantic_checked_n}",
                f"- 检索异常: {error_n}",
                f"- veto 剔除: {len(vetoed_codes)}",
            ]

            if vetoed_codes:
                before_n = len(selected_df)
                selected_df = selected_df[
                    ~selected_df["code"].astype(str).isin(set(vetoed_codes))
                ].reset_index(drop=True)
                print(
                    f"[step3][rag] 负面新闻 veto: {before_n} -> {len(selected_df)}（剔除{len(vetoed_codes)}）"
                )
                rag_veto_preview = (
                    "## 🛡️ RAG 防雷执行摘要（前置）\n"
                    + "\n".join(rag_summary_lines)
                    + "\n\n## 🛑 RAG 防雷已剔除（前置）\n"
                    + "\n".join(rag_veto_lines)
                    + "\n\n---\n"
                )
            else:
                print("[step3][rag] 未命中负面关键词，保持候选不变")
                rag_veto_preview = (
                    "## 🛡️ RAG 防雷执行摘要（前置）\n"
                    + "\n".join(rag_summary_lines)
                    + "\n\n---\n"
                )
    else:
        if STEP3_ENABLE_RAG_VETO:
            if selected_df.empty:
                print("[step3][rag] 跳过：候选为空")
                rag_skip_reason = "候选为空"
            elif not is_rag_veto_enabled():
                print("[step3][rag] 跳过：RAG_VETO_ENABLED=0")
                rag_skip_reason = "RAG_VETO_ENABLED=0"
            else:
                print("[step3][rag] 跳过：未满足运行条件")
                rag_skip_reason = "未满足运行条件"
    if STEP3_ENABLE_RAG_VETO and not rag_veto_preview and rag_skip_reason:
        rag_veto_preview = (
            "## 🛡️ RAG 防雷执行摘要（前置）\n"
            "- 执行状态: 跳过\n"
            f"- 原因: {rag_skip_reason}\n\n---\n"
        )

    selected_codes = [str(x) for x in selected_df["code"].tolist()]
    if not selected_codes:
        report = (
            "# 🏛️ Alpha 投委会机密电报：威科夫盘面审判\n\n"
            "## 💀 逻辑破产\n"
            "- 无（本轮无明确失效标的可判定）\n\n"
            "## ⏳ 储备营地\n"
            "- 无（候选均被 RAG 防雷 veto 或数据不足）\n\n"
            "## 🏹 处于起跳板\n"
            "- 无（风险过高，今日保持观望）"
        )
        if rag_veto_lines:
            report = rag_veto_preview + report + "\n\n## 🛑 RAG 防雷剔除清单\n" + "\n".join(rag_veto_lines)
        if notify:
            model_banner = f"🤖 模型: {model}"
            content = f"{model_banner}\n\n{report}"
            title = f"📄 批量研报 {date.today().strftime('%Y-%m-%d')}"
            sent = send_feishu_notification(webhook_url, title, content) if webhook_url else True
            if wecom_webhook:
                send_wecom_notification(wecom_webhook, title, content)
            if dingtalk_webhook:
                send_dingtalk_notification(dingtalk_webhook, title, content)
            if not sent:
                return (False, "feishu_failed", report)
        return (True, "ok", report)

    payloads_by_track: dict[str, list[str]] = {"Trend": [], "Accum": []}
    df_by_track: dict[str, pd.DataFrame] = {
        "Trend": selected_df.iloc[0:0].copy(),
        "Accum": selected_df.iloc[0:0].copy(),
    }
    selected_codes_by_track: dict[str, list[str]] = {"Trend": [], "Accum": []}
    items_by_track: dict[str, list[dict]] = {"Trend": [], "Accum": []}

    for _, row in selected_df.iterrows():
        code = str(row["code"])
        df = code_to_df.get(code)
        if df is None:
            continue
        track_key = str(row.get("track", "")).strip()
        if track_key not in {"Trend", "Accum"}:
            track_key = "Trend"
        policy_val = row.get("policy_tag")
        policy_text = (
            str(policy_val).strip()
            if isinstance(policy_val, str) and str(policy_val).strip()
            else None
        )
        _exit_sig = str(row.get("exit_signal", "")).strip() or None
        _exit_price_raw = pd.to_numeric(row.get("exit_price"), errors="coerce")
        _exit_price = float(_exit_price_raw) if pd.notna(_exit_price_raw) else None
        _exit_reason = str(row.get("exit_reason", "")).strip() or None
        payload = generate_stock_payload(
            stock_code=code,
            stock_name=str(row.get("name", code)),
            wyckoff_tag=str(row.get("tag", "")),
            df=df,
            industry=str(row.get("industry", "")),
            market_cap_yi=pd.to_numeric(row.get("market_cap_yi"), errors="coerce"),
            avg_amount_20_yi=pd.to_numeric(row.get("avg_amount_20_yi"), errors="coerce"),
            policy_tag=policy_text,
            track=track_key,
            stage=str(row.get("stage", "")).strip() or None,
            sector_state=str(row.get("sector_state", "")).strip() or None,
            sector_state_code=str(row.get("sector_state_code", "")).strip() or None,
            sector_note=str(row.get("sector_note", "")).strip() or None,
            exit_signal=_exit_sig,
            exit_price=_exit_price,
            exit_reason=_exit_reason,
        )
        payloads_by_track.setdefault(track_key, []).append(payload)
        df_by_track[track_key] = pd.concat(
            [df_by_track[track_key], row.to_frame().T],
            ignore_index=True,
        )
        selected_codes_by_track[track_key].append(code)
        item = next((x for x in items if str(x.get("code")) == code), None)
        if item:
            items_by_track[track_key].append(item)

    benchmark_lines = []
    if benchmark_context:
        breadth_ctx = benchmark_context.get("breadth", {}) or {}
        benchmark_lines.append("[宏观水温 / Benchmark Context]")
        benchmark_lines.append(
            f"regime={benchmark_context.get('regime')}, "
            f"close={benchmark_context.get('close')}, "
            f"ma50={benchmark_context.get('ma50')}, "
            f"ma200={benchmark_context.get('ma200')}, "
            f"ma50_slope_5d={benchmark_context.get('ma50_slope_5d')}"
        )
        benchmark_lines.append(
            f"recent3_cum_pct={benchmark_context.get('recent3_cum_pct')}"
        )
        if benchmark_context.get("main_vol_ratio_5_20") is not None:
            benchmark_lines.append(
                f"main_vol_ratio_5_20={benchmark_context.get('main_vol_ratio_5_20'):.3f}, "
                f"main_volume_state={benchmark_context.get('main_volume_state')}"
            )
        market_pv_summary = str(benchmark_context.get("market_pv_summary", "") or "").strip()
        market_pv_outlook = str(benchmark_context.get("market_pv_outlook", "") or "").strip()
        if market_pv_summary or market_pv_outlook:
            benchmark_lines.append("[大盘量价推演 / Price-Volume Outlook]")
            if market_pv_summary:
                benchmark_lines.append(market_pv_summary)
            if market_pv_outlook:
                benchmark_lines.append(market_pv_outlook)
        if breadth_ctx:
            benchmark_lines.append(
                f"breadth_pct={breadth_ctx.get('ratio_pct')}, "
                f"breadth_delta_pct={breadth_ctx.get('delta_pct')}"
            )
        rotation_headline = str(sector_rotation_ctx.get("headline", "")).strip()
        rotation_lines = sector_rotation_ctx.get("overview_lines", []) or []
        if rotation_headline or rotation_lines:
            benchmark_lines.append("[板块轮动 / Sector Rotation]")
            if rotation_headline:
                benchmark_lines.append(rotation_headline)
            benchmark_lines.extend(rotation_lines[:4])

    active_tracks = [
        track for track in ["Trend", "Accum"] if payloads_by_track.get(track)
    ]
    if not active_tracks:
        detail = ", ".join(f"{s}({e})" for s, e in failed) if failed else "无可用 payload"
        print(f"[step3] 候选存在，但未能生成可用模型输入: {detail}")
        return (False, "payload_build_failed", "")
    candidate_track_counts = (
        candidates_df["track"].value_counts().to_dict()
        if "track" in candidates_df.columns
        else {}
    )
    track_requests: list[dict] = []
    for track in active_tracks:
        user_message = _build_track_user_message(
            track=track,
            benchmark_lines=benchmark_lines,
            payloads=payloads_by_track.get(track, []),
            compressed=STEP3_ENABLE_COMPRESSION,
            raw_count=int(candidate_track_counts.get(track, len(payloads_by_track.get(track, [])))),
            selected_count=len(payloads_by_track.get(track, [])),
        )
        track_requests.append(
            {
                "track": track,
                "user_message": user_message,
                "selected_count": len(payloads_by_track.get(track, [])),
            }
        )
        _dump_model_input(
            items=items_by_track.get(track, []),
            model=model,
            system_prompt=WYCKOFF_FUNNEL_SYSTEM_PROMPT,
            user_message=user_message,
            name_hint=track.lower(),
        )

    if STEP3_SKIP_LLM:
        if notify:
            ok, preview_report = _send_input_preview(
                webhook_url=webhook_url,
                model=model,
                system_prompt=WYCKOFF_FUNNEL_SYSTEM_PROMPT,
                previews=track_requests,
            )
            if not ok:
                return (False, "feishu_failed", preview_report)
        else:
            preview_blocks: list[str] = [
                "# 🧪 Step3 模型输入预演（未调用大模型）",
                "",
                f"- 目标模型: `{model}`",
                f"- 输入股票数: `{sum(int(x.get('selected_count', 0) or 0) for x in track_requests)}`",
                "- 模式: `STEP3_SKIP_LLM=1`",
                "",
            ]
            for req in track_requests:
                preview_blocks.extend(
                    [
                        f"## {TRACK_LABELS.get(str(req.get('track', '')), str(req.get('track', '')))}",
                        "",
                        str(req.get("user_message", "") or ""),
                        "",
                    ]
                )
            preview_report = "\n".join(preview_blocks).strip()
        return (True, "ok_preview", preview_report)

    track_reports: list[tuple[str, str]] = []
    used_models: dict[str, str] = {}
    for request in track_requests:
        track = str(request.get("track", "Trend"))
        ok, track_report, used_model = _call_track_report(
            track=track,
            system_prompt=WYCKOFF_FUNNEL_SYSTEM_PROMPT,
            user_message=str(request.get("user_message", "")),
            model=model,
            api_key=api_key,
            selected_codes=selected_codes_by_track.get(track, []),
            selected_df=df_by_track.get(track, selected_df.iloc[0:0].copy()),
            provider=provider,
            llm_base_url=llm_base_url,
        )
        if not ok:
            return (False, "llm_failed", "")
        used_models[track] = used_model
        track_title = TRACK_LABELS.get(track, track)
        track_reports.append(
            (
                track,
                f"## {track_title}\n\n{_strip_report_title(track_report)}".strip(),
            )
        )

    report = "\n\n---\n\n".join(section for _, section in track_reports).strip()

    unique_used_models = list(dict.fromkeys(used_models.values()))
    if len(unique_used_models) == 1:
        model_banner = f"🤖 模型: {unique_used_models[0]}（分轨调用）"
    else:
        model_banner = "🤖 模型: " + " | ".join(
            f"{TRACK_LABELS.get(track, track)}={used_models.get(track, model)}"
            for track in active_tracks
        )
    code_name = {
        str(row.get("code")): str(row.get("name", row.get("code")))
        for _, row in selected_df.iterrows()
    }
    selected_set = set(selected_codes)
    # 优先从 Markdown 操作区提取；若未来回退为结构化 JSON，也保持兼容。
    ops_codes = _extract_ops_codes_from_markdown(report, selected_set)
    structured = _try_parse_structured_report(
        report=report,
        allowed_codes=selected_set,
        code_name=code_name,
    )
    if not ops_codes and structured and structured.get("operation_pool"):
        for item in structured["operation_pool"]:
            code = str(item.get("code", "")).strip()
            if code and code not in ops_codes:
                ops_codes.append(code)
    ops_lines = [f"- {c} {code_name.get(c, c)}" for c in ops_codes]
    ops_preview = (
        "## 🏹 处于起跳板速览（前置）\n"
        + ("\n".join(ops_lines) if ops_lines else "- 无")
        + "\n\n---\n"
    )

    content = f"{model_banner}\n\n{rag_veto_preview}{ops_preview}\n{report}"
    if rag_veto_lines:
        content += "\n\n## 🛑 RAG 防雷剔除清单\n" + "\n".join(rag_veto_lines)
    print(f"[step3] 飞书发送原文长度={len(content)}（不压缩，交由飞书分片）")
    print(
        "[step3] 研报实际使用模型="
        + " | ".join(f"{track}:{used_models.get(track, model)}" for track in active_tracks)
    )
    if failed:
        content += f"\n\n**获取失败**: {', '.join(f'{s}({e})' for s, e in failed)}"

    title = f"📄 批量研报 {date.today().strftime('%Y-%m-%d')}"
    if notify:
        sent = send_feishu_notification(webhook_url, title, content) if webhook_url else True
        if wecom_webhook:
            send_wecom_notification(wecom_webhook, title, content)
        if dingtalk_webhook:
            send_dingtalk_notification(dingtalk_webhook, title, content)
        if not sent:
            print("[step3] 飞书推送失败")
            return (False, "feishu_failed", report)
    print(
        f"[step3] 研报发送成功，股票数={sum(len(payloads_by_track.get(t, [])) for t in active_tracks)}，"
        f"拉取失败数={len(failed)}"
    )
    return (True, "ok", report)
