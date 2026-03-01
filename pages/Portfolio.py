import os
import re
import sys
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
import streamlit as st
from postgrest.exceptions import APIError

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.layout import setup_page
from app.navigation import show_right_nav
from app.ui_helpers import show_page_loading
from integrations.supabase_client import get_supabase_client

PORTFOLIO_ID = "USER_LIVE"
TABLE_PORTFOLIOS = "portfolios"
TABLE_POSITIONS = "portfolio_positions"


def _to_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(str(v).strip())
    except Exception:
        return default


def _parse_buy_dt(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{8}", s):
        try:
            return datetime.strptime(s, "%Y%m%d").date()
        except Exception:
            return None
    try:
        return datetime.fromisoformat(s[:10]).date()
    except Exception:
        return None


def _format_buy_dt(v: Any) -> str:
    d = _parse_buy_dt(v)
    if not d:
        return ""
    return d.strftime("%Y%m%d")


def _format_money(v: float) -> str:
    return f"{float(v):,.2f}"


def _parse_money_input(raw: Any, field_name: str) -> float:
    s = str(raw or "").strip().replace(",", "")
    if not s:
        raise ValueError(f"{field_name} 不能为空")
    try:
        val = float(s)
    except Exception as e:
        raise ValueError(f"{field_name} 必须是数字") from e
    if val < 0:
        raise ValueError(f"{field_name} 不能为负数")
    return val


def _estimate_positions_value(rows: list[dict[str, Any]]) -> float:
    total = 0.0
    for r in rows:
        shares = int(_to_float(r.get("shares", 0), 0))
        cost = _to_float(r.get("cost_price", 0.0), 0.0)
        if shares > 0 and cost >= 0:
            total += shares * cost
    return float(total)


def _load_user_live() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    supabase = get_supabase_client()

    p_resp = (
        supabase.table(TABLE_PORTFOLIOS)
        .select("portfolio_id,name,free_cash,total_equity")
        .eq("portfolio_id", PORTFOLIO_ID)
        .limit(1)
        .execute()
    )
    if not p_resp.data:
        supabase.table(TABLE_PORTFOLIOS).upsert(
            {
                "portfolio_id": PORTFOLIO_ID,
                "name": "Real Portfolio",
                "free_cash": 0.0,
                "total_equity": None,
                "updated_at": datetime.utcnow().isoformat(),
            },
            on_conflict="portfolio_id",
        ).execute()
        portfolio = {
            "portfolio_id": PORTFOLIO_ID,
            "name": "Real Portfolio",
            "free_cash": 0.0,
            "total_equity": None,
        }
    else:
        portfolio = p_resp.data[0]

    pos_resp = (
        supabase.table(TABLE_POSITIONS)
        .select("code,name,shares,cost_price,buy_dt,strategy")
        .eq("portfolio_id", PORTFOLIO_ID)
        .order("code")
        .execute()
    )
    positions = pos_resp.data or []
    return portfolio, positions


def _to_editor_df(rows: list[dict[str, Any]]) -> pd.DataFrame:
    data: list[dict[str, Any]] = []
    for row in rows:
        data.append(
            {
                "代码": str(row.get("code", "")).strip(),
                "名称": str(row.get("name", "")).strip(),
                "成本": _to_float(row.get("cost_price", 0.0)),
                "数量": int(_to_float(row.get("shares", 0), 0)),
                "建仓时间": _parse_buy_dt(row.get("buy_dt")),
                "策略": str(row.get("strategy", "")).strip(),
                "删除": False,
            }
        )
    if not data:
        data.append(
            {
                "代码": "",
                "名称": "",
                "成本": 0.0,
                "数量": 0,
                "建仓时间": None,
                "策略": "",
                "删除": False,
            }
        )
    return pd.DataFrame(data)


def _save_user_live(
    *,
    free_cash: float,
    total_equity: float | None,
    editor_df: pd.DataFrame,
    existing_codes: set[str],
) -> tuple[bool, str]:
    supabase = get_supabase_client()

    payload_by_code: dict[str, dict[str, Any]] = {}
    deleted_codes: set[str] = set()
    errors: list[str] = []

    for idx, row in enumerate(editor_df.to_dict("records"), start=1):
        code = str(row.get("代码", "")).strip()
        if not code:
            continue
        if not re.fullmatch(r"\d{6}", code):
            errors.append(f"第 {idx} 行代码非法（必须6位数字）")
            continue
        if code in payload_by_code:
            errors.append(f"代码重复：{code}")
            continue

        mark_delete = bool(row.get("删除", False))
        shares = int(_to_float(row.get("数量", 0), 0))
        cost_price = _to_float(row.get("成本", 0.0), 0.0)
        name = str(row.get("名称", "")).strip() or code
        strategy = str(row.get("策略", "")).strip()
        buy_dt = _format_buy_dt(row.get("建仓时间"))

        if cost_price < 0:
            errors.append(f"第 {idx} 行成本不能为负")
            continue

        # 删除勾选或数量<=0 都视为清仓
        if mark_delete or shares <= 0:
            deleted_codes.add(code)
            continue

        payload_by_code[code] = {
            "portfolio_id": PORTFOLIO_ID,
            "code": code,
            "name": name,
            "shares": shares,
            "cost_price": cost_price,
            "buy_dt": buy_dt,
            "strategy": strategy,
            "updated_at": datetime.utcnow().isoformat(),
        }

    if errors:
        return False, "；".join(errors)

    keep_codes = set(payload_by_code.keys())
    delete_codes = (existing_codes - keep_codes) | deleted_codes

    try:
        supabase.table(TABLE_PORTFOLIOS).upsert(
            {
                "portfolio_id": PORTFOLIO_ID,
                "name": "Real Portfolio",
                "free_cash": float(free_cash),
                "total_equity": (None if total_equity is None else float(total_equity)),
                "updated_at": datetime.utcnow().isoformat(),
            },
            on_conflict="portfolio_id",
        ).execute()

        for code in sorted(delete_codes):
            (
                supabase.table(TABLE_POSITIONS)
                .delete()
                .eq("portfolio_id", PORTFOLIO_ID)
                .eq("code", code)
                .execute()
            )

        if payload_by_code:
            supabase.table(TABLE_POSITIONS).upsert(
                list(payload_by_code.values()),
                on_conflict="portfolio_id,code",
            ).execute()
        return True, f"保存成功：持仓 {len(payload_by_code)} 只，删除 {len(delete_codes)} 只"
    except APIError as e:
        return False, f"Supabase API 异常: {e.code} - {e.message}"
    except Exception as e:
        return False, f"保存失败: {e}"


setup_page(page_title="持仓管理", page_icon="💼")
content_col = show_right_nav()

with content_col:
    st.markdown(
        """
<style>
.portfolio-summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}
.portfolio-card {
  border: 1px solid #e9ebef;
  border-radius: 12px;
  padding: 14px 16px;
  background: #ffffff;
}
.portfolio-card .label {
  color: #7a808c;
  font-size: 14px;
  line-height: 1.2;
  margin-bottom: 6px;
}
.portfolio-card .value {
  color: #1f2430;
  font-size: 40px;
  font-weight: 650;
  letter-spacing: 0.2px;
  line-height: 1.1;
}
@media (max-width: 960px) {
  .portfolio-summary {
    grid-template-columns: 1fr;
  }
}
</style>
        """,
        unsafe_allow_html=True,
    )

    st.title("💼 持仓管理")
    st.caption("管理 Step4 的 USER_LIVE 账本。删除行即清仓；Step4 将优先读取这里。")

    loading = show_page_loading(title="加载持仓中...", subtitle="正在读取 USER_LIVE")
    try:
        portfolio, positions = _load_user_live()
    finally:
        loading.empty()

    existing_codes = {str(x.get("code", "")).strip() for x in positions}
    free_cash_initial = _to_float(portfolio.get("free_cash", 0.0), 0.0)
    total_equity_raw = portfolio.get("total_equity")
    manual_total_equity = (
        _to_float(total_equity_raw, 0.0) if total_equity_raw is not None else None
    )
    positions_value_est = _estimate_positions_value(positions)
    display_total_equity = (
        manual_total_equity
        if manual_total_equity is not None
        else (free_cash_initial + positions_value_est)
    )
    holding_count = len([p for p in positions if int(_to_float(p.get("shares", 0), 0)) > 0])

    st.markdown(
        f"""
<div class="portfolio-summary">
  <div class="portfolio-card">
    <div class="label">总市值</div>
    <div class="value">{_format_money(display_total_equity)}</div>
  </div>
  <div class="portfolio-card">
    <div class="label">现金</div>
    <div class="value">{_format_money(free_cash_initial)}</div>
  </div>
  <div class="portfolio-card">
    <div class="label">持仓股数</div>
    <div class="value">{holding_count}</div>
  </div>
</div>
        """,
        unsafe_allow_html=True,
    )

    auto_equity = total_equity_raw is None
    input_default_equity = (
        manual_total_equity if manual_total_equity is not None else display_total_equity
    )
    c1, c2, c3 = st.columns([1, 1, 1])
    with c1:
        free_cash_input = st.text_input(
            "现金",
            value=f"{free_cash_initial:.2f}",
            help="用于 Step4 的可用现金",
        )
    with c2:
        total_equity_input = st.text_input(
            "总市值",
            value=f"{input_default_equity:.2f}",
            disabled=auto_equity,
            help="关闭自动推导后可手动指定",
        )
    with c3:
        auto_equity = st.toggle(
            "总资产自动推导（推荐）",
            value=auto_equity,
            help="开启后 total_equity 保存为 NULL，Step4 自动按 现金+最新持仓市值推导。",
        )

    st.markdown("### 持仓股")
    st.caption("每行一只股票。勾选“删除”或把数量改为 0，保存后会清仓。可直接新增行。")

    editor_df = st.data_editor(
        _to_editor_df(positions),
        use_container_width=True,
        hide_index=True,
        num_rows="dynamic",
        column_config={
            "代码": st.column_config.TextColumn(
                "代码",
                help="A股6位代码，如 002273",
                max_chars=6,
                required=True,
            ),
            "名称": st.column_config.TextColumn("名称", max_chars=20),
            "成本": st.column_config.NumberColumn(
                "成本",
                min_value=0.0,
                step=0.001,
                format="%.3f",
                required=True,
            ),
            "数量": st.column_config.NumberColumn(
                "数量",
                min_value=0,
                step=100,
                format="%d",
                required=True,
            ),
            "建仓时间": st.column_config.DateColumn(
                "建仓时间",
                format="YYYY-MM-DD",
            ),
            "策略": st.column_config.TextColumn("策略", max_chars=50),
            "删除": st.column_config.CheckboxColumn("删除", default=False),
        },
        key="portfolio_editor",
    )

    col_save, col_reload = st.columns([1, 1])
    with col_save:
        if st.button("💾 保存 USER_LIVE", use_container_width=True):
            try:
                free_cash_value = _parse_money_input(free_cash_input, "现金")
                total_equity_value = (
                    None
                    if auto_equity
                    else _parse_money_input(total_equity_input, "总市值")
                )
            except ValueError as e:
                st.error(str(e))
                st.stop()

            loader = show_page_loading(title="保存中...", subtitle="正在写入 Supabase")
            try:
                ok, msg = _save_user_live(
                    free_cash=free_cash_value,
                    total_equity=total_equity_value,
                    editor_df=editor_df,
                    existing_codes=existing_codes,
                )
            finally:
                loader.empty()
            if ok:
                st.toast(msg, icon="✅")
                st.rerun()
            else:
                st.error(msg)
    with col_reload:
        if st.button("🔄 重新加载", use_container_width=True):
            st.rerun()
