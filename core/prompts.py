# -*- coding: utf-8 -*-
# Copyright (c) 2024 youngcan. All Rights Reserved.
# 本代码仅供个人学习研究使用，未经授权不得用于商业目的。
# 商业授权请联系作者支付授权费用。

"""
AI 分析用系统提示词常量 — 统一存放。

合并原 integrations/ai_prompts.py 与 core/wyckoff_single_prompt.py。
"""

# ── 从 core/ai_prompts.py 迁入 ──────────────────────────

from core.ai_prompts import (  # noqa: F401
    ALPHA_CIO_SYSTEM_PROMPT,
    PRIVATE_PM_DECISION_JSON_PROMPT,
    PRIVATE_PM_SYSTEM_PROMPT,
    WYCKOFF_FUNNEL_SYSTEM_PROMPT,
)

# ── 从旧 core/wyckoff_single_prompt.py 迁入 ────────────────────────

from core.wyckoff_single_prompt import WYCKOFF_SINGLE_SYSTEM_PROMPT  # noqa: F401
