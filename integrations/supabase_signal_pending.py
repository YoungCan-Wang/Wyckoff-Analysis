"""Supabase signal_pending 表读写，模式同 supabase_recommendation.py。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pandas as pd

from core.constants import TABLE_SIGNAL_PENDING
from core.signal_confirmation import SIGNAL_TTL_DAYS, build_snap, run_confirmation_cycle
from integrations.supabase_base import create_admin_client as _admin
from integrations.supabase_base import is_admin_configured as _configured


def write_pending_signals(
    signal_date: str,
    triggers: dict[str, list[tuple[str, float]]],
    df_map: dict[str, pd.DataFrame],
    regime: str = "NEUTRAL",
    name_map: dict[str, str] | None = None,
    sector_map: dict[str, str] | None = None,
    cfg: Any = None,
) -> int:
    """将 L4 触发信号写入 signal_pending 表，返回写入行数。"""
    if not _configured():
        return 0

    name_map, sector_map = name_map or {}, sector_map or {}
    now_iso = datetime.now(UTC).isoformat()
    payload: list[dict[str, Any]] = []

    for signal_type, hits in triggers.items():
        ttl = SIGNAL_TTL_DAYS.get(signal_type, 3)
        for code, score in hits:
            df = df_map.get(code)
            if df is None or df.empty:
                continue
            snap = build_snap(signal_type, df, score, cfg)
            payload.append(
                {
                    "code": int(code) if code.isdigit() else 0,
                    "signal_type": signal_type,
                    "signal_date": signal_date,
                    "signal_score": float(score),
                    "status": "pending",
                    "ttl_days": ttl,
                    "days_elapsed": 0,
                    "regime": regime,
                    "name": name_map.get(code, code),
                    "industry": sector_map.get(code, ""),
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    **snap,
                }
            )

    if not payload:
        return 0

    try:
        client = _admin()
        existing = client.table(TABLE_SIGNAL_PENDING).select("code,signal_type").eq("status", "pending").execute()
        existing_keys = {(int(r["code"]), r["signal_type"]) for r in (existing.data or [])}
        to_insert = [p for p in payload if (int(p["code"]), p["signal_type"]) not in existing_keys]
        if not to_insert:
            print(f"[signal_pending] {len(payload)} 条信号已存在 pending，跳过")
            return 0
        client.table(TABLE_SIGNAL_PENDING).insert(to_insert).execute()
        print(f"[signal_pending] 写入 {len(to_insert)} 条（跳过 {len(payload) - len(to_insert)} 条已有）")
        return len(to_insert)
    except Exception as e:
        print(f"[signal_pending] write failed: {e}")
        return 0


def load_pending_signals() -> list[dict[str, Any]]:
    if not _configured():
        return []
    try:
        return _admin().table(TABLE_SIGNAL_PENDING).select("*").eq("status", "pending").execute().data or []
    except Exception as e:
        print(f"[signal_pending] load failed: {e}")
        return []


def batch_update_signals(updates: list[dict[str, Any]]) -> bool:
    if not _configured() or not updates:
        return True
    try:
        client = _admin()
        now_iso = datetime.now(UTC).isoformat()
        for upd in updates:
            row_id = upd.get("id")
            if row_id is None:
                continue
            row: dict[str, Any] = {
                "status": upd["status"],
                "days_elapsed": upd.get("days_elapsed", 0),
                "confirm_reason": upd.get("confirm_reason", ""),
                "updated_at": now_iso,
            }
            if upd.get("confirm_date"):
                row["confirm_date"] = upd["confirm_date"]
            if upd.get("expire_date"):
                row["expire_date"] = upd["expire_date"]
            client.table(TABLE_SIGNAL_PENDING).update(row).eq("id", row_id).execute()
        print(f"[signal_pending] 更新 {len(updates)} 条状态")
        return True
    except Exception as e:
        print(f"[signal_pending] update failed: {e}")
        return False


def run_step2_5(
    signal_date: str,
    triggers: dict[str, list[tuple[str, float]]],
    df_map: dict[str, pd.DataFrame],
    regime: str = "NEUTRAL",
    name_map: dict[str, str] | None = None,
    sector_map: dict[str, str] | None = None,
    cfg: Any = None,
) -> list[dict[str, Any]]:
    """Step2.5：写入新信号 + 确认/过期旧信号，返回确认通过的 symbol_info 列表。"""
    write_pending_signals(signal_date, triggers, df_map, regime, name_map, sector_map, cfg)
    pending = load_pending_signals()
    if not pending:
        return []
    updates, confirmed = run_confirmation_cycle(pending, df_map, signal_date)
    if updates:
        batch_update_signals(updates)
    return confirmed
