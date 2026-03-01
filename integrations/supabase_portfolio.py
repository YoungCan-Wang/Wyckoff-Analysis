# -*- coding: utf-8 -*-
"""
Supabase 投资组合读写（脚本侧，无 Streamlit 依赖）
用途：
1) 读取 USER_LIVE 持仓状态给 Step4 使用
2) 记录 AI 订单建议与每日净值快照
"""
from __future__ import annotations

from datetime import datetime
import os
from typing import Any

from supabase import Client, create_client

TABLE_PORTFOLIOS = "portfolios"
TABLE_PORTFOLIO_POSITIONS = "portfolio_positions"
TABLE_TRADE_ORDERS = "trade_orders"
TABLE_DAILY_NAV = "daily_nav"


def _get_supabase_admin_client() -> Client:
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (
        (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        or (os.getenv("SUPABASE_KEY") or "").strip()
    )
    if not url or not key:
        raise ValueError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY 未配置")
    return create_client(url, key)


def is_supabase_configured() -> bool:
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (
        (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        or (os.getenv("SUPABASE_KEY") or "").strip()
    )
    return bool(url and key)


def load_portfolio_state(portfolio_id: str = "USER_LIVE") -> dict[str, Any] | None:
    """
    返回格式：
    {
      "portfolio_id": "...",
      "free_cash": 12345.6,
      "total_equity": 23456.7 | None,
      "positions": [{"code","name","cost","buy_dt","shares","strategy"}, ...]
    }
    """
    if not is_supabase_configured():
        return None
    try:
        client = _get_supabase_admin_client()
        p_resp = (
            client.table(TABLE_PORTFOLIOS)
            .select("portfolio_id,free_cash,total_equity")
            .eq("portfolio_id", portfolio_id)
            .limit(1)
            .execute()
        )
        if not p_resp.data:
            return None
        p = p_resp.data[0]
        pos_resp = (
            client.table(TABLE_PORTFOLIO_POSITIONS)
            .select("code,name,shares,cost_price,buy_dt,strategy,stop_loss")
            .eq("portfolio_id", portfolio_id)
            .order("code")
            .execute()
        )
        positions: list[dict[str, Any]] = []
        for row in pos_resp.data or []:
            positions.append(
                {
                    "code": str(row.get("code", "")).strip(),
                    "name": str(row.get("name", "")).strip(),
                    "cost": float(row.get("cost_price", 0.0) or 0.0),
                    "buy_dt": str(row.get("buy_dt", "") or "").strip(),
                    "shares": int(row.get("shares", 0) or 0),
                    "strategy": str(row.get("strategy", "") or "").strip(),
                    "stop_loss": (
                        float(row["stop_loss"]) if row.get("stop_loss") is not None else None
                    ),
                }
            )
        return {
            "portfolio_id": str(p.get("portfolio_id")),
            "free_cash": float(p.get("free_cash", 0.0) or 0.0),
            "total_equity": (
                float(p["total_equity"]) if p.get("total_equity") is not None else None
            ),
            "positions": positions,
        }
    except Exception as e:
        print(f"[supabase_portfolio] load_portfolio_state failed: {e}")
        return None


def check_daily_run_exists(portfolio_id: str, trade_date: str) -> bool:
    """
    检查当日是否已存在交易订单（幂等性检查）。
    返回 True 表示已运行过。
    """
    if not is_supabase_configured():
        return False
    try:
        client = _get_supabase_admin_client()
        # 只要查到一条记录，就说明跑过了
        resp = (
            client.table(TABLE_TRADE_ORDERS)
            .select("id")
            .eq("portfolio_id", portfolio_id)
            .eq("trade_date", trade_date)
            .limit(1)
            .execute()
        )
        return bool(resp.data)
    except Exception as e:
        print(f"[supabase_portfolio] check_daily_run_exists failed: {e}")
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
    except Exception as e:
        print(f"[supabase_portfolio] update_position_stops failed: {e}")
        return False


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
                    "price_hint": (
                        float(o["price_hint"]) if o.get("price_hint") is not None else None
                    ),
                    "amount": float(o.get("amount", 0.0) or 0.0),
                    "stop_loss": (
                        float(o["stop_loss"]) if o.get("stop_loss") is not None else None
                    ),
                    "max_loss": float(o.get("max_loss", 0.0) or 0.0),
                    "drawdown_ratio": float(o.get("drawdown_ratio", 0.0) or 0.0),
                    "reason": str(o.get("reason", "") or ""),
                    "tape_condition": str(o.get("tape_condition", "") or ""),
                    "invalidate_condition": str(o.get("invalidate_condition", "") or ""),
                    "created_at": datetime.utcnow().isoformat(),
                }
            )
        client.table(TABLE_TRADE_ORDERS).insert(payload).execute()
        return True
    except Exception as e:
        print(f"[supabase_portfolio] save_ai_trade_orders failed: {e}")
        return False


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
            "updated_at": datetime.utcnow().isoformat(),
        }
        client.table(TABLE_DAILY_NAV).upsert(
            payload,
            on_conflict="portfolio_id,trade_date",
        ).execute()
        return True
    except Exception as e:
        print(f"[supabase_portfolio] upsert_daily_nav failed: {e}")
        return False

