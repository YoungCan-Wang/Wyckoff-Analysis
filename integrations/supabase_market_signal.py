"""
Supabase 最新交易日市场信号读写

用途：
1) 定时任务写入 A50 / VIX / 大盘水温
2) Web 端读取最新交易日市场信号并渲染全局提示栏
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from supabase import Client

from core.constants import TABLE_MARKET_SIGNAL_DAILY
from integrations.supabase_base import create_admin_client as _get_supabase_admin_client
from integrations.supabase_base import is_admin_configured as is_supabase_admin_configured


def _normalize_trade_date(raw: Any) -> str:
    if isinstance(raw, date):
        return raw.isoformat()
    text = str(raw or "").strip()
    if len(text) >= 10:
        return text[:10]
    return text


def _safe_float(raw: Any) -> float | None:
    try:
        if raw is None:
            return None
        text = str(raw).strip().replace(",", "")
        if not text:
            return None
        return float(text)
    except Exception:
        return None


def _format_signed_pct(raw: Any) -> str:
    value = _safe_float(raw)
    if value is None:
        return "--"
    return f"{value:+.2f}%"


def _format_plain(raw: Any, digits: int = 2) -> str:
    value = _safe_float(raw)
    if value is None:
        return "--"
    return f"{value:.{digits}f}"


def _benchmark_regime_desc(regime: str) -> str:
    mapping = {
        "RISK_ON": "偏强",
        "NEUTRAL": "中性",
        "RISK_OFF": "偏弱",
        "CRASH": "极弱",
        "BLACK_SWAN": "极端恶劣",
        "UNKNOWN": "待确认",
    }
    return mapping.get(str(regime or "").strip().upper(), "待确认")


def _premarket_regime_desc(regime: str) -> str:
    mapping = {
        "NORMAL": "平稳",
        "CAUTION": "情绪冲击",
        "RISK_OFF": "转冷",
        "BLACK_SWAN": "急剧恶化",
    }
    return mapping.get(str(regime or "").strip().upper(), "待确认")


def _normalize_benchmark_slot(regime: str) -> str:
    normalized = str(regime or "").strip().upper()
    if normalized == "RISK_ON":
        return "RISK_ON"
    if normalized == "NEUTRAL":
        return "NEUTRAL"
    if normalized in {"CRASH", "BLACK_SWAN"}:
        return "CRASH"
    return "RISK_OFF"


def _normalize_premarket_slot(regime: str) -> str:
    normalized = str(regime or "").strip().upper()
    if normalized in {"BLACK_SWAN", "RISK_OFF", "CAUTION", "NORMAL"}:
        return normalized
    return "NORMAL"


def _benchmark_state_sentence(regime: str) -> str:
    mapping = {
        "RISK_ON": "盘后主线仍偏强",
        "NEUTRAL": "盘后市场仍在震荡观察",
        "RISK_OFF": "盘后市场已偏弱",
        "CRASH": "盘后市场已处在明显防守区",
        "BLACK_SWAN": "盘后市场已处在明显防守区",
        "UNKNOWN": "盘后市场状态仍待确认",
    }
    return mapping.get(str(regime or "").strip().upper(), "盘后市场状态仍待确认")


def _premarket_state_sentence(regime: str) -> str:
    mapping = {
        "NORMAL": "隔夜外部冲击相对平稳",
        "CAUTION": "隔夜情绪扰动已经出现",
        "RISK_OFF": "隔夜风险偏好明显转冷",
        "BLACK_SWAN": "隔夜恐慌冲击正在抬升",
    }
    return mapping.get(str(regime or "").strip().upper(), "隔夜外部环境仍待确认")


STRUCTURED_MARKET_SIGNAL_FIELDS = {
    "benchmark_slot",
    "premarket_slot",
    "market_posture_code",
    "market_posture_name",
    "wind_phrase",
    "water_phrase",
    "action_phrase",
}


MARKET_BANNER_MATRIX: dict[str, dict[str, dict[str, str]]] = {
    "BLACK_SWAN": {
        "RISK_ON": {
            "posture_code": "DEFENSIVE",
            "posture_name": "防守优先",
            "tone": "保守",
            "title": "亲爱的投资者，最新交易日大盘偏强，但隔夜恐慌冲击已显著抬升。",
            "wind": "盘面风向正在由进攻转向防守",
            "water": "避险资金正在快速回流",
            "action": "先收缩防线，暂停激进追价，只保留最确定的观察与应对",
        },
        "NEUTRAL": {
            "posture_code": "HARD_DEFENSE",
            "posture_name": "严防死守",
            "tone": "恶劣",
            "title": "亲爱的投资者，最新交易日水温中性，但隔夜恐慌冲击已经压过试探空间。",
            "wind": "市场风向明显偏冷",
            "water": "资金更倾向于防守和撤离",
            "action": "以防守为先，耐心等待风险释放，不要伸手接刀",
        },
        "RISK_OFF": {
            "posture_code": "HARD_DEFENSE",
            "posture_name": "严防死守",
            "tone": "恶劣",
            "title": "亲爱的投资者，最新交易日内外部信号共振转弱，当前先守再说。",
            "wind": "弱势风向正在共振",
            "water": "流动性更偏向撤退而不是进攻",
            "action": "先把风险控制放在首位，尽量减少无谓出手",
        },
        "CRASH": {
            "posture_code": "HARD_DEFENSE",
            "posture_name": "严防死守",
            "tone": "恶劣",
            "title": "亲爱的投资者，最新交易日已处在高压防守区，当前严禁激进出手。",
            "wind": "恐慌风暴仍在场内回荡",
            "water": "避险资金继续占上风",
            "action": "严格防守，等待市场重新给出清晰修复信号",
        },
    },
    "RISK_OFF": {
        "RISK_ON": {
            "posture_code": "DEFENSIVE",
            "posture_name": "收缩防线",
            "tone": "保守",
            "title": "亲爱的投资者，最新交易日大盘偏强，但隔夜风险偏好已经转冷。",
            "wind": "盘面风向仍在上方，但阻力开始变大",
            "water": "资金从全面进攻转向去弱留强",
            "action": "控制节奏和仓位，只跟随最强、最清晰的主线机会",
        },
        "NEUTRAL": {
            "posture_code": "DEFENSIVE",
            "posture_name": "收缩防线",
            "tone": "保守",
            "title": "亲爱的投资者，最新交易日方向尚未完全明朗，隔夜风险偏冷需要优先处理。",
            "wind": "市场风向偏向谨慎",
            "water": "资金更愿意先看清再行动",
            "action": "先稳住节奏，多看少动，等待更高胜率的确认点",
        },
        "RISK_OFF": {
            "posture_code": "DEFENSIVE",
            "posture_name": "收缩防线",
            "tone": "保守",
            "title": "亲爱的投资者，最新交易日市场已偏弱，隔夜风险继续转冷。",
            "wind": "弱势风向仍在延续",
            "water": "资金持续往防守端聚集",
            "action": "以防守仓位为主，避免在弱势环境中频繁试错",
        },
        "CRASH": {
            "posture_code": "HARD_DEFENSE",
            "posture_name": "严防死守",
            "tone": "恶劣",
            "title": "亲爱的投资者，最新交易日市场处在防守区，隔夜风险继续加码。",
            "wind": "下行压力没有解除",
            "water": "场内资金仍以撤退为主",
            "action": "严格收缩战线，等风险释放充分后再讨论进攻",
        },
    },
    "CAUTION": {
        "RISK_ON": {
            "posture_code": "CONTROLLED_ATTACK",
            "posture_name": "控制试探",
            "tone": "谨慎乐观",
            "title": "亲爱的投资者，最新交易日大盘仍偏强，但隔夜情绪出现扰动。",
            "wind": "做多风向还在，但节奏开始放缓",
            "water": "资金仍会流向强者，只是不再全面扩散",
            "action": "可以继续顺势跟随，但要用更轻的仓位去做更高胜率的确认机会",
        },
        "NEUTRAL": {
            "posture_code": "PATIENT_OBSERVE",
            "posture_name": "耐心观察",
            "tone": "谨慎",
            "title": "亲爱的投资者，最新交易日水温中性，隔夜情绪扰动要求先看清方向。",
            "wind": "市场风向仍在摇摆",
            "water": "资金在试探，暂未形成明确合力",
            "action": "盘中沉着应对，先观察，再等待最清晰的结构确认",
        },
        "RISK_OFF": {
            "posture_code": "DEFENSIVE",
            "posture_name": "收缩防线",
            "tone": "保守",
            "title": "亲爱的投资者，最新交易日市场偏弱，隔夜情绪扰动会继续放大压力。",
            "wind": "偏弱风向暂未改变",
            "water": "资金更倾向于收缩而非扩张",
            "action": "保持防守姿态，只做极少量、极高确定性的试探",
        },
        "CRASH": {
            "posture_code": "HARD_DEFENSE",
            "posture_name": "严防死守",
            "tone": "恶劣",
            "title": "亲爱的投资者，最新交易日仍在防守区，隔夜情绪扰动不宜低估。",
            "wind": "弱势风向占主导",
            "water": "资金风险偏好仍在收缩",
            "action": "不急于出手，先把仓位纪律和止损纪律放在第一位",
        },
    },
    "NORMAL": {
        "RISK_ON": {
            "posture_code": "FULL_ATTACK",
            "posture_name": "顺势进攻",
            "tone": "乐观",
            "title": "亲爱的投资者，最新交易日内外部信号共振偏强。",
            "wind": "做多风向仍在发酵",
            "water": "资金仍在向强势主线集中",
            "action": "顺势跟随，但只参与有确认、有纪律的高胜率机会",
        },
        "NEUTRAL": {
            "posture_code": "PATIENT_OBSERVE",
            "posture_name": "耐心观察",
            "tone": "谨慎",
            "title": "亲爱的投资者，最新交易日水温中性，先等待方向自己走出来。",
            "wind": "市场风向仍在试探",
            "water": "资金在轮动中寻找下一步方向",
            "action": "不急着抢跑，耐心等更清晰的盘口与结构确认",
        },
        "RISK_OFF": {
            "posture_code": "DEFENSIVE",
            "posture_name": "收缩防线",
            "tone": "保守",
            "title": "亲爱的投资者，最新交易日市场偏弱，当前仍以防守为先。",
            "wind": "偏弱风向暂未扭转",
            "water": "资金更偏向防守而不是扩张",
            "action": "先保护本金，等待水温真正回暖后再提升进攻强度",
        },
        "CRASH": {
            "posture_code": "HARD_DEFENSE",
            "posture_name": "严防死守",
            "tone": "恶劣",
            "title": "亲爱的投资者，最新交易日市场环境偏冷，当前不要与下行趋势硬碰硬。",
            "wind": "市场风向仍明显偏空",
            "water": "资金仍处在避险模式",
            "action": "继续防守，避免在高波动环境中频繁试错",
        },
    },
}


def compose_market_state(row: dict[str, Any] | None) -> dict[str, str]:
    data = dict(row or {})
    benchmark_regime = str(data.get("benchmark_regime", "") or "").strip().upper()
    premarket_regime = str(data.get("premarket_regime", "") or "").strip().upper()
    benchmark_slot = _normalize_benchmark_slot(benchmark_regime)
    premarket_slot = _normalize_premarket_slot(premarket_regime)
    strategy = (
        MARKET_BANNER_MATRIX.get(premarket_slot, {}).get(benchmark_slot) or MARKET_BANNER_MATRIX["CAUTION"]["NEUTRAL"]
    )

    return {
        "benchmark_slot": benchmark_slot,
        "premarket_slot": premarket_slot,
        "market_posture_code": strategy["posture_code"],
        "market_posture_name": strategy["posture_name"],
        "wind_phrase": strategy["wind"],
        "water_phrase": strategy["water"],
        "action_phrase": strategy["action"],
        "banner_tone": strategy["tone"],
    }


def compose_market_banner(row: dict[str, Any] | None) -> dict[str, str]:
    data = dict(row or {})
    benchmark_regime = str(data.get("benchmark_regime", "") or "").strip().upper()
    premarket_regime = str(data.get("premarket_regime", "") or "").strip().upper()
    state = compose_market_state(data)
    title = (
        MARKET_BANNER_MATRIX.get(state["premarket_slot"], {}).get(state["benchmark_slot"], {}).get("title")
        or "亲爱的投资者，最新交易日请顺势而为，保持节奏。"
    )
    body = (
        "以上指标按各自最新可用时间更新。"
        f"{_benchmark_state_sentence(benchmark_regime)}，{_premarket_state_sentence(premarket_regime)}。"
        f"当前{state['wind_phrase']}，{state['water_phrase']}。"
        f"{state['action_phrase']}。"
        "交易的本质是顺势而为：乘风而上，顺水推舟。"
    )

    return {
        **state,
        "banner_title": title,
        "banner_message": body,
    }


def _deep_merge_source_jobs(base: Any, patch: Any) -> dict[str, Any]:
    left = dict(base or {}) if isinstance(base, dict) else {}
    right = dict(patch or {}) if isinstance(patch, dict) else {}
    merged = dict(left)
    for key, value in right.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _normalize_row_for_upsert(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if "trade_date" in out:
        out["trade_date"] = _normalize_trade_date(out.get("trade_date"))
    for key in [
        "main_index_close",
        "main_index_ma50",
        "main_index_ma200",
        "main_index_recent3_cum_pct",
        "main_index_today_pct",
        "smallcap_close",
        "smallcap_recent3_cum_pct",
        "a50_close",
        "a50_pct_chg",
        "vix_close",
        "vix_pct_chg",
    ]:
        if key in out:
            out[key] = _safe_float(out.get(key))
    for key in ["a50_value_date", "vix_value_date"]:
        if key in out and out.get(key):
            out[key] = _normalize_trade_date(out.get(key))
    if "premarket_reasons" in out and out.get("premarket_reasons") is None:
        out["premarket_reasons"] = []
    if "source_jobs" in out and not isinstance(out.get("source_jobs"), dict):
        out["source_jobs"] = {}
    return out


def _load_market_signal_by_trade_date(client: Client, trade_date: str) -> dict[str, Any] | None:
    resp = client.table(TABLE_MARKET_SIGNAL_DAILY).select("*").eq("trade_date", trade_date).limit(1).execute()
    if not resp.data:
        return None
    return dict(resp.data[0])


def _iter_market_signal_clients(client: Client | None = None) -> list[Client]:
    clients: list[Client] = []
    if client is not None:
        clients.append(client)
        return clients
    if is_supabase_admin_configured():
        try:
            clients.append(_get_supabase_admin_client())
        except Exception:
            pass
    try:
        from integrations.supabase_client import get_supabase_client

        session_client = get_supabase_client()
        if session_client is not None:
            clients.append(session_client)
    except Exception:
        pass
    return clients


def upsert_market_signal_daily(trade_date: date | str, patch: dict[str, Any]) -> bool:
    if not is_supabase_admin_configured():
        return False
    try:
        client = _get_supabase_admin_client()
        trade_date_text = _normalize_trade_date(trade_date)
        existing = _load_market_signal_by_trade_date(client, trade_date_text) or {}
        merged = dict(existing)
        merged.update(_normalize_row_for_upsert(dict(patch or {})))
        merged["trade_date"] = trade_date_text
        merged["source_jobs"] = _deep_merge_source_jobs(
            existing.get("source_jobs"),
            patch.get("source_jobs") if isinstance(patch, dict) else None,
        )
        merged.update(compose_market_banner(merged))
        merged["updated_at"] = datetime.now(UTC).isoformat()
        try:
            client.table(TABLE_MARKET_SIGNAL_DAILY).upsert(
                _normalize_row_for_upsert(merged),
                on_conflict="trade_date",
            ).execute()
        except Exception:
            fallback = {k: v for k, v in merged.items() if k not in STRUCTURED_MARKET_SIGNAL_FIELDS}
            client.table(TABLE_MARKET_SIGNAL_DAILY).upsert(
                _normalize_row_for_upsert(fallback),
                on_conflict="trade_date",
            ).execute()
        return True
    except Exception:
        return False


def load_market_signal_daily(trade_date: date | str, client: Client | None = None) -> dict[str, Any] | None:
    trade_date_text = _normalize_trade_date(trade_date)
    for sb in _iter_market_signal_clients(client):
        try:
            row = _load_market_signal_by_trade_date(sb, trade_date_text)
            if row:
                return row
        except Exception:
            continue
    return None


def load_latest_market_signal_daily(client: Client | None = None) -> dict[str, Any] | None:
    def _is_non_empty(value: Any) -> bool:
        if value is None:
            return False
        text = str(value).strip()
        return text != ""

    def _pick_latest_with_fields(rows: list[dict[str, Any]], required_any: tuple[str, ...]) -> dict[str, Any] | None:
        for r in rows:
            if any(_is_non_empty(r.get(k)) for k in required_any):
                return r
        return None

    for sb in _iter_market_signal_clients(client):
        try:
            if sb is None:
                continue
            resp = (
                sb.table(TABLE_MARKET_SIGNAL_DAILY)
                .select("*")
                .order("trade_date", desc=True)
                .order("updated_at", desc=True)
                .limit(120)
                .execute()
            )
            rows = [dict(x) for x in (resp.data or []) if isinstance(x, dict)]
            if not rows:
                continue

            # 基础记录：整体最新一条，用于兜底字段。
            merged = dict(rows[0])

            benchmark_row = _pick_latest_with_fields(
                rows,
                ("benchmark_regime", "main_index_close", "main_index_ma50", "main_index_ma200"),
            )
            premarket_row = _pick_latest_with_fields(
                rows,
                ("premarket_regime", "premarket_reasons"),
            )
            a50_row = _pick_latest_with_fields(
                rows,
                ("a50_close", "a50_pct_chg", "a50_value_date"),
            )
            vix_row = _pick_latest_with_fields(
                rows,
                ("vix_close", "vix_pct_chg", "vix_value_date"),
            )

            if benchmark_row:
                for key in (
                    "trade_date",
                    "benchmark_regime",
                    "main_index_code",
                    "main_index_close",
                    "main_index_ma50",
                    "main_index_ma200",
                    "main_index_recent3_cum_pct",
                    "main_index_today_pct",
                    "smallcap_index_code",
                    "smallcap_close",
                    "smallcap_recent3_cum_pct",
                ):
                    merged[key] = benchmark_row.get(key)

            if premarket_row:
                for key in ("premarket_regime", "premarket_reasons"):
                    merged[key] = premarket_row.get(key)

            if a50_row:
                for key in ("a50_value_date", "a50_source", "a50_close", "a50_pct_chg"):
                    merged[key] = a50_row.get(key)

            if vix_row:
                for key in ("vix_value_date", "vix_source", "vix_close", "vix_pct_chg"):
                    merged[key] = vix_row.get(key)

            # 用合并后的“最新可用分组数据”重算文案
            merged.update(compose_market_banner(merged))
            return merged
        except Exception:
            continue
    return None
