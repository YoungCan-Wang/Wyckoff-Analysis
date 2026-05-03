"""
Supabase 投资组合读写（脚本侧，无 Streamlit 依赖）
用途：
1) 读取 USER_LIVE 持仓状态给 Step4 使用
2) 记录 AI 订单建议与每日净值快照
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

from supabase import Client

from core.constants import (
    TABLE_DAILY_NAV,
    TABLE_PORTFOLIO_POSITIONS,
    TABLE_PORTFOLIOS,
    TABLE_TRADE_ORDERS,
    TABLE_USER_SETTINGS,
)
from integrations.supabase_base import create_admin_client as _get_supabase_admin_client
from integrations.supabase_base import is_admin_configured as is_supabase_configured


def load_user_settings_admin(user_id: str) -> dict[str, Any] | None:
    user_id = str(user_id or "").strip()
    if not user_id or not is_supabase_configured():
        return None
    try:
        client = _get_supabase_admin_client()
        resp = client.table(TABLE_USER_SETTINGS).select("*").eq("user_id", user_id).limit(1).execute()
        if not resp.data:
            return None
        row = resp.data[0] or {}
        if not isinstance(row, dict):
            return None
        return row
    except Exception:
        logger.debug("[supabase_portfolio] load_user_settings_admin failed: {e}")
        return None


def _normalize_buy_dt_text(raw: Any) -> str:
    text = str(raw or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return text
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text.replace("-", "")
    return text


def compute_portfolio_state_signature(
    free_cash: float | int | None,
    positions: list[dict[str, Any]] | None,
) -> str:
    normalized_positions: list[dict[str, Any]] = []
    for row in positions or []:
        code = str(row.get("code", "") or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            continue
        normalized_positions.append(
            {
                "code": code,
                "shares": int(row.get("shares", 0) or 0),
                "cost_price": round(float(row.get("cost_price", row.get("cost", 0.0)) or 0.0), 4),
                "buy_dt": _normalize_buy_dt_text(row.get("buy_dt")),
            }
        )
    normalized_positions.sort(key=lambda x: x["code"])
    payload = {
        "free_cash": round(float(free_cash or 0.0), 2),
        "positions": normalized_positions,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def extract_state_signature_from_run_id(run_id: Any) -> str:
    text = str(run_id or "").strip()
    m = re.search(r"_sig([0-9a-fA-F]{8,40})$", text)
    return m.group(1).lower() if m else ""


def _is_active_trade_order_status(status: Any) -> bool:
    return str(status or "").strip().upper() not in {"", "CANCELLED", "CANCELED"}


def load_portfolio_state(portfolio_id: str = "USER_LIVE", client: Client | None = None) -> dict[str, Any] | None:
    """
    返回格式：
    {
      "portfolio_id": "...",
      "free_cash": 12345.6,
      "total_equity": 23456.7 | None,
      "positions": [{"code","name","cost","buy_dt","shares"}, ...]
    }
    """
    if client is None and not is_supabase_configured():
        return None
    try:
        client = client or _get_supabase_admin_client()
        p_resp = (
            client.table(TABLE_PORTFOLIOS)
            .select("portfolio_id,free_cash,total_equity,updated_at")
            .eq("portfolio_id", portfolio_id)
            .limit(1)
            .execute()
        )
        if not p_resp.data:
            return None
        p = p_resp.data[0]
        pos_resp = (
            client.table(TABLE_PORTFOLIO_POSITIONS)
            .select("code,name,shares,cost_price,buy_dt,stop_loss,updated_at")
            .eq("portfolio_id", portfolio_id)
            .order("code")
            .execute()
        )
        positions: list[dict[str, Any]] = []
        latest_updates: list[str] = [str(p.get("updated_at", "") or "").strip()]
        for row in pos_resp.data or []:
            row_updated_at = str(row.get("updated_at", "") or "").strip()
            if row_updated_at:
                latest_updates.append(row_updated_at)
            positions.append(
                {
                    "code": str(row.get("code", "")).strip(),
                    "name": str(row.get("name", "")).strip(),
                    "cost": float(row.get("cost_price", 0.0) or 0.0),
                    "buy_dt": str(row.get("buy_dt", "") or "").strip(),
                    "shares": int(row.get("shares", 0) or 0),
                    "stop_loss": (float(row["stop_loss"]) if row.get("stop_loss") is not None else None),
                    "updated_at": row_updated_at,
                }
            )
        state_updated_at = max((x for x in latest_updates if x), default="")
        return {
            "portfolio_id": str(p.get("portfolio_id")),
            "free_cash": float(p.get("free_cash", 0.0) or 0.0),
            "total_equity": (float(p["total_equity"]) if p.get("total_equity") is not None else None),
            "updated_at": str(p.get("updated_at", "") or "").strip(),
            "state_updated_at": state_updated_at,
            "state_signature": compute_portfolio_state_signature(p.get("free_cash", 0.0), positions),
            "positions": positions,
        }
    except Exception as e:
        logger.warning("[supabase_portfolio] load_portfolio_state failed: %s", e)
        return None


def build_user_live_portfolio_id(user_id: str) -> str:
    user_id = str(user_id or "").strip()
    return f"USER_LIVE:{user_id}"


def list_step4_targets(target_user_id: str | None = None) -> list[dict[str, Any]]:
    """
    自动发现可执行 Step4 的用户目标：
    - 来自 user_settings（必须有 user_id / tg_bot_token / tg_chat_id）
    - 自动映射 portfolio_id=USER_LIVE:<user_id>
    - 仅返回 Supabase 中已存在且结构可用的 portfolio
    """
    if not is_supabase_configured():
        return []
    try:
        client = _get_supabase_admin_client()
        query = client.table(TABLE_USER_SETTINGS).select("user_id,tg_bot_token,tg_chat_id,gemini_api_key,gemini_model")
        target_user_id = str(target_user_id or "").strip()
        if target_user_id:
            query = query.eq("user_id", target_user_id).limit(1)
        resp = query.execute()
        targets: list[dict[str, Any]] = []
        for row in resp.data or []:
            user_id = str(row.get("user_id", "") or "").strip()
            if target_user_id and user_id != target_user_id:
                continue
            tg_bot_token = str(row.get("tg_bot_token", "") or "").strip()
            tg_chat_id = str(row.get("tg_chat_id", "") or "").strip()
            if not user_id or not tg_bot_token or not tg_chat_id:
                continue
            portfolio_id = build_user_live_portfolio_id(user_id)
            p = load_portfolio_state(portfolio_id)
            if not isinstance(p, dict):
                continue
            if p.get("free_cash") is None or not isinstance(p.get("positions"), list):
                continue
            targets.append(
                {
                    "user_id": user_id,
                    "portfolio_id": portfolio_id,
                    "tg_bot_token": tg_bot_token,
                    "tg_chat_id": tg_chat_id,
                    "gemini_api_key": str(row.get("gemini_api_key", "") or "").strip(),
                    "gemini_model": str(row.get("gemini_model", "") or "").strip(),
                }
            )
        return targets
    except Exception:
        logger.debug("[supabase_portfolio] list_step4_targets failed: {e}")
        return []


def check_daily_run_exists(
    portfolio_id: str,
    trade_date: str,
    state_signature: str | None = None,
) -> bool:
    """
    检查当日是否已存在同一持仓快照下的有效交易订单（幂等性检查）。
    返回 True 表示当前快照已运行过。
    """
    if not is_supabase_configured():
        return False
    try:
        client = _get_supabase_admin_client()
        resp = (
            client.table(TABLE_TRADE_ORDERS)
            .select("run_id,status,created_at")
            .eq("portfolio_id", portfolio_id)
            .eq("trade_date", trade_date)
            .order("created_at", desc=True)
            .limit(200)
            .execute()
        )
        rows = resp.data or []
        active_rows = [row for row in rows if _is_active_trade_order_status(row.get("status"))]
        if not active_rows:
            return False
        expected_sig = str(state_signature or "").strip().lower()
        if not expected_sig:
            return True
        return any(extract_state_signature_from_run_id(row.get("run_id")) == expected_sig for row in active_rows)
    except Exception:
        logger.debug("[supabase_portfolio] check_daily_run_exists failed: {e}")
        return False


def update_position_stops(portfolio_id: str, updates: list[dict[str, Any]]) -> bool:
    """
    批量更新持仓止损价。
    updates: [{"code": "000001", "stop_loss": 12.34}, ...]
    """
    if not is_supabase_configured() or not updates:
        return False
    try:
        client = _get_supabase_admin_client()
        # Supabase 不支持批量 update 不同值，需逐个 update
        # 若量大可考虑其它方式，目前持仓数不多，循环即可
        for item in updates:
            code = item.get("code")
            stop_loss = item.get("stop_loss")
            if not code or stop_loss is None:
                continue
            (
                client.table(TABLE_PORTFOLIO_POSITIONS)
                .update({"stop_loss": stop_loss})
                .eq("portfolio_id", portfolio_id)
                .eq("code", code)
                .execute()
            )
        return True
    except Exception:
        logger.debug("[supabase_portfolio] update_position_stops failed: {e}")
        return False


def _ensure_portfolio_exists(portfolio_id: str, client: Client) -> None:
    """确保 portfolios 行存在，不存在则创建。"""
    resp = client.table(TABLE_PORTFOLIOS).select("portfolio_id").eq("portfolio_id", portfolio_id).limit(1).execute()
    if not resp.data:
        client.table(TABLE_PORTFOLIOS).upsert(
            {"portfolio_id": portfolio_id, "free_cash": 0, "name": "我的持仓"},
            on_conflict="portfolio_id",
        ).execute()


def upsert_position(portfolio_id: str, position: dict[str, Any], client: Client | None = None) -> tuple[bool, str]:
    """新增或更新单个持仓。

    position 需包含 code，可选 name/shares/cost_price/buy_dt/strategy。
    返回 (成功, 消息)。
    """
    code = str(position.get("code", "")).strip()
    if not code or len(code) != 6:
        return False, f"无效的股票代码: {code}"
    try:
        client = client or _get_supabase_admin_client()
        _ensure_portfolio_exists(portfolio_id, client)
        row = {
            "portfolio_id": portfolio_id,
            "code": code,
            "name": str(position.get("name", "") or "").strip(),
            "shares": int(position.get("shares", 0) or 0),
            "cost_price": float(position.get("cost_price", 0) or 0),
            "buy_dt": str(position.get("buy_dt", "") or "").strip(),
        }
        client.table(TABLE_PORTFOLIO_POSITIONS).upsert(row, on_conflict="portfolio_id,code").execute()
        return True, f"{code} 已更新"
    except Exception as e:
        logger.warning("[supabase_portfolio] upsert_position failed: %s", e)
        return False, str(e)


def delete_position(portfolio_id: str, code: str, client: Client | None = None) -> tuple[bool, str]:
    """删除单个持仓。"""
    code = code.strip()
    try:
        client = client or _get_supabase_admin_client()
        client.table(TABLE_PORTFOLIO_POSITIONS).delete().eq("portfolio_id", portfolio_id).eq("code", code).execute()
        return True, f"{code} 已删除"
    except Exception as e:
        logger.warning("[supabase_portfolio] delete_position failed: %s", e)
        return False, str(e)


def update_free_cash(portfolio_id: str, free_cash: float, client: Client | None = None) -> tuple[bool, str]:
    """更新可用资金。"""
    try:
        client = client or _get_supabase_admin_client()
        _ensure_portfolio_exists(portfolio_id, client)
        client.table(TABLE_PORTFOLIOS).update({"free_cash": free_cash}).eq("portfolio_id", portfolio_id).execute()
        return True, f"可用资金已更新为 {free_cash:,.2f}"
    except Exception as e:
        logger.warning("[supabase_portfolio] update_free_cash failed: %s", e)
        return False, str(e)


def save_ai_trade_orders(
    *,
    run_id: str,
    portfolio_id: str,
    model: str,
    trade_date: str,
    market_view: str,
    orders: list[dict[str, Any]],
) -> bool:
    if not is_supabase_configured():
        return False
    if not orders:
        return True
    try:
        client = _get_supabase_admin_client()
        payload: list[dict[str, Any]] = []
        for o in orders:
            payload.append(
                {
                    "run_id": run_id,
                    "portfolio_id": portfolio_id,
                    "trade_date": trade_date,
                    "model": model,
                    "market_view": market_view or "",
                    "code": str(o.get("code", "")).strip(),
                    "name": str(o.get("name", "")).strip(),
                    "action": str(o.get("action", "")).strip(),
                    "status": str(o.get("status", "")).strip(),
                    "shares": int(o.get("shares", 0) or 0),
                    "price_hint": (float(o["price_hint"]) if o.get("price_hint") is not None else None),
                    "amount": float(o.get("amount", 0.0) or 0.0),
                    "stop_loss": (float(o["stop_loss"]) if o.get("stop_loss") is not None else None),
                    "max_loss": float(o.get("max_loss", 0.0) or 0.0),
                    "drawdown_ratio": float(o.get("drawdown_ratio", 0.0) or 0.0),
                    "reason": str(o.get("reason", "") or ""),
                    "tape_condition": str(o.get("tape_condition", "") or ""),
                    "invalidate_condition": str(o.get("invalidate_condition", "") or ""),
                    "created_at": datetime.now(UTC).isoformat(),
                }
            )
        client.table(TABLE_TRADE_ORDERS).insert(payload).execute()
        return True
    except Exception:
        logger.debug("[supabase_portfolio] save_ai_trade_orders failed: {e}")
        return False


def cancel_trade_orders(
    *,
    portfolio_id: str,
    trade_date: str,
    exclude_run_id: str | None = None,
) -> int:
    if not is_supabase_configured():
        return 0
    try:
        client = _get_supabase_admin_client()
        query = (
            client.table(TABLE_TRADE_ORDERS)
            .select("id,status,run_id")
            .eq("portfolio_id", portfolio_id)
            .eq("trade_date", trade_date)
            .limit(500)
        )
        if exclude_run_id:
            query = query.neq("run_id", exclude_run_id)
        rows = query.execute().data or []
        active_rows = [row for row in rows if _is_active_trade_order_status(row.get("status"))]
        for row in active_rows:
            (client.table(TABLE_TRADE_ORDERS).update({"status": "CANCELLED"}).eq("id", row.get("id")).execute())
        return len(active_rows)
    except Exception:
        logger.debug("[supabase_portfolio] cancel_trade_orders failed: {e}")
        return 0


def upsert_daily_nav(
    *,
    portfolio_id: str,
    trade_date: str,
    free_cash: float,
    total_equity: float,
    positions_value: float,
) -> bool:
    if not is_supabase_configured():
        return False
    try:
        client = _get_supabase_admin_client()
        payload = {
            "portfolio_id": portfolio_id,
            "trade_date": trade_date,
            "free_cash": float(free_cash),
            "positions_value": float(positions_value),
            "total_equity": float(total_equity),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        client.table(TABLE_DAILY_NAV).upsert(
            payload,
            on_conflict="portfolio_id,trade_date",
        ).execute()
        return True
    except Exception:
        logger.debug("[supabase_portfolio] upsert_daily_nav failed: {e}")
        return False
