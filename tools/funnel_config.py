"""
FunnelConfig 环境变量覆盖工具。

提供 FunnelConfig 的 env-var 覆盖与布尔解析逻辑，
供 agents/ 和 scripts/ 共同复用。
"""

from __future__ import annotations

import os
from dataclasses import fields as dataclass_fields

from core.wyckoff_engine import FunnelConfig


def parse_int_env(name: str, default: int) -> int:
    """安全解析整型环境变量，支持浮点字符串如 '5.0'。"""
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except Exception:
        return default


def parse_bool(raw: str) -> bool:
    """解析布尔字符串（1/true/yes/on → True）。"""
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def apply_funnel_cfg_overrides(cfg: FunnelConfig) -> None:
    """
    将环境变量 FUNNEL_CFG_* 映射到 FunnelConfig 字段。

    示例：FUNNEL_CFG_MIN_MARKET_CAP_YI=35 → cfg.min_market_cap_yi = 35.0

    注意：enable_evr_trigger 仅由 regime 自动决策，不接受环境变量覆盖。
    """
    for f in dataclass_fields(FunnelConfig):
        if f.name == "enable_evr_trigger":
            # EVR 仅由 regime 自动决策，不接受环境变量覆盖。
            continue
        key = f"FUNNEL_CFG_{f.name.upper()}"
        raw = os.getenv(key)
        if raw is None:
            continue
        val = raw.strip()
        if not val:
            continue
        try:
            current = getattr(cfg, f.name, None)
            if isinstance(current, bool):
                parsed = parse_bool(val)
            elif isinstance(current, int) and not isinstance(current, bool):
                parsed = int(float(val))
            elif isinstance(current, float):
                parsed = float(val)
            else:
                parsed = val
            setattr(cfg, f.name, parsed)
        except Exception as e:
            print(f"[funnel] ⚠️ 忽略非法配置 {key}={raw!r}: {e}")
