"""
任务限频 — 基于 Supabase 的 per-user 每日使用次数管控 + 等级体系。
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from core.constants import TABLE_JOB_USAGE

logger = logging.getLogger(__name__)

TABLE_USER_TIER = "user_tier"

# ---------------------------------------------------------------------------
# 等级定义
# ---------------------------------------------------------------------------
# tier_name → {job_kind: daily_limit}
# daily_limit = 0 表示该功能不可用, -1 表示无限制
TIER_LIMITS: dict[str, dict[str, int]] = {
    "free": {
        "full_pipeline": 2,
        "batch_ai_report": 5,
        "funnel_screen": 3,
    },
    "basic": {
        "full_pipeline": 5,
        "batch_ai_report": 15,
        "funnel_screen": 8,
    },
    "pro": {
        "full_pipeline": 20,
        "batch_ai_report": 50,
        "funnel_screen": 30,
    },
    "unlimited": {
        "full_pipeline": -1,
        "batch_ai_report": -1,
        "funnel_screen": -1,
    },
}

TIER_LABELS: dict[str, str] = {
    "free": "免费版",
    "basic": "基础版",
    "pro": "专业版",
    "unlimited": "无限版",
}

DEFAULT_TIER = "free"


# ---------------------------------------------------------------------------
# 等级查询
# ---------------------------------------------------------------------------


def get_user_tier(user_id: str) -> str:
    """
    查询用户等级。

    - user_tier 表有记录且未过期 → 返回对应 tier
    - 无记录或已过期 → 返回 DEFAULT_TIER ("free")
    - Supabase 不可用 → 返回 DEFAULT_TIER（降级放行）
    """
    if not user_id:
        return DEFAULT_TIER
    try:
        from integrations.supabase_client import get_supabase_client

        supabase = get_supabase_client()
        resp = supabase.table(TABLE_USER_TIER).select("tier, expires_at").eq("user_id", user_id).execute()
        if not resp.data:
            return DEFAULT_TIER
        row = resp.data[0]
        tier = str(row.get("tier", DEFAULT_TIER) or DEFAULT_TIER).strip().lower()
        # 检查是否过期
        expires_at = row.get("expires_at")
        if expires_at:
            try:
                exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                if exp < datetime.now(UTC):
                    return DEFAULT_TIER  # 已过期，降级
            except (ValueError, TypeError):
                pass
        return tier if tier in TIER_LIMITS else DEFAULT_TIER
    except Exception as e:
        logger.warning("[job_usage] 查询用户等级失败（降级为 %s）: %s", DEFAULT_TIER, e)
        return DEFAULT_TIER


def get_tier_limit(tier: str, job_kind: str) -> int:
    """获取指定等级、指定任务的每日上限。-1=无限, 0=不可用。"""
    tier_cfg = TIER_LIMITS.get(tier, TIER_LIMITS[DEFAULT_TIER])
    return tier_cfg.get(job_kind, 0)


def get_user_tier_info(user_id: str) -> dict[str, Any]:
    """返回用户等级完整信息，用于页面展示。"""
    tier = get_user_tier(user_id)
    return {
        "tier": tier,
        "label": TIER_LABELS.get(tier, tier),
        "limits": TIER_LIMITS.get(tier, TIER_LIMITS[DEFAULT_TIER]),
    }


# ---------------------------------------------------------------------------
# 用量查询 / 自增
# ---------------------------------------------------------------------------


def get_daily_usage(user_id: str, job_kind: str) -> int:
    """查询用户今日已使用次数。Supabase 不可用时返回 0（不阻塞）。"""
    if not user_id:
        return 0
    try:
        from integrations.supabase_client import get_supabase_client

        supabase = get_supabase_client()
        today = date.today().isoformat()
        resp = (
            supabase.table(TABLE_JOB_USAGE)
            .select("count")
            .eq("user_id", user_id)
            .eq("job_kind", job_kind)
            .eq("usage_date", today)
            .execute()
        )
        if resp.data:
            return int(resp.data[0].get("count", 0))
        return 0
    except Exception as e:
        logger.warning("[job_usage] 查询失败（降级放行）: %s", e)
        return 0


def increment_daily_usage(user_id: str, job_kind: str) -> int:
    """
    将用户今日使用次数 +1 并返回新值。
    Supabase 不可用时返回 -1（不阻塞）。
    """
    if not user_id:
        return -1
    try:
        from integrations.supabase_client import get_supabase_client

        supabase = get_supabase_client()
        today = date.today().isoformat()
        current = get_daily_usage(user_id, job_kind)
        new_count = current + 1
        supabase.table(TABLE_JOB_USAGE).upsert(
            {
                "user_id": user_id,
                "job_kind": job_kind,
                "usage_date": today,
                "count": new_count,
            },
            on_conflict="user_id,job_kind,usage_date",
        ).execute()
        tier = get_user_tier(user_id)
        limit = get_tier_limit(tier, job_kind)
        logger.info(
            "[job_usage] %s/%s [%s]: %d/%s",
            user_id[:8],
            job_kind,
            tier,
            new_count,
            "∞" if limit < 0 else str(limit),
        )
        return new_count
    except Exception as e:
        logger.warning("[job_usage] 写入失败（降级放行）: %s", e)
        return -1


# ---------------------------------------------------------------------------
# 限频校验
# ---------------------------------------------------------------------------


def check_daily_limit(user_id: str, job_kind: str) -> tuple[bool, int, int]:
    """
    检查是否超出每日限额（自动关联用户等级）。

    Returns:
        (allowed, used, limit)
        - allowed: True 表示可以继续执行
        - used: 今日已用次数
        - limit: 每日上限（-1 = 无限制, 0 = 不可用）
    """
    tier = get_user_tier(user_id)
    limit = get_tier_limit(tier, job_kind)
    if limit < 0:
        # 无限制
        return (True, 0, -1)
    if limit == 0:
        # 该等级不可用此功能
        return (False, 0, 0)
    used = get_daily_usage(user_id, job_kind)
    return (used < limit, used, limit)
