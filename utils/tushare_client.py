# -*- coding: utf-8 -*-
"""
Tushare Pro 客户端封装

从环境变量 TUSHARE_TOKEN 读取 token，提供 pro_api 实例。
使用时：在 GitHub Secrets 中配置 TUSHARE_TOKEN，本地可在 .env 中配置。

用法:
    from utils.tushare_client import get_pro

    pro = get_pro()
    if pro:
        df = pro.daily(ts_code="000001.SZ", start_date="20240101", end_date="20240226")
"""
from __future__ import annotations

import os
import warnings


def get_pro():
    """返回 Tushare Pro API 实例；若未配置 token 则返回 None。"""
    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not token:
        try:
            import streamlit as st
            token = (st.session_state.get("tushare_token") or "").strip()
        except Exception:
            pass
    if not token:
        return None
    try:
        warnings.filterwarnings(
            "ignore",
            message=r".*Series\.fillna with 'method' is deprecated.*",
            category=FutureWarning,
            module=r"tushare\.pro\.data_pro",
        )
        import tushare as ts
        ts.set_token(token)
        return ts.pro_api()
    except ImportError:
        return None
