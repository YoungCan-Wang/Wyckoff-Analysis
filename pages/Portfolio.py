import re
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
import streamlit as st
from postgrest.exceptions import APIError

from app.layout import setup_page
from app.navigation import show_right_nav
from app.ui_helpers import show_page_loading
from core.constants import TABLE_PORTFOLIO_POSITIONS, TABLE_PORTFOLIOS, TABLE_TRADE_ORDERS
from integrations.supabase_client import get_supabase_client
from integrations.supabase_portfolio import (
    compute_portfolio_state_signature,
    extract_state_signature_from_run_id,
)
from utils.trading_clock import CN_TZ, resolve_end_calendar_day

PORTFOLIO_SCOPE = "USER_LIVE"
TABLE_POSITIONS = TABLE_PORTFOLIO_POSITIONS
EDIT_BLACKOUT_WINDOWS = (
    ((7, 50), (8, 0), "盘前风控窗口"),
    ((18, 20), (18, 30), "晚间再平衡窗口"),
)


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


def _current_portfolio_id() -> str | None:
    """
    按登录用户隔离持仓：
    USER_LIVE:<user_id>
    """
    user = st.session_state.get("user")
    if isinstance(user, dict):
        user_id = str(user.get("id") or "").strip()
        if user_id:
            return f"{PORTFOLIO_SCOPE}:{user_id}"
    return None


def _parse_iso_ts(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except Exception:
        return None


def _calc_state_updated_at(
    portfolio: dict[str, Any],
    positions: list[dict[str, Any]],
) -> datetime | None:
    values: list[datetime] = []
    p_dt = _parse_iso_ts(portfolio.get("updated_at"))
    if p_dt is not None:
        values.append(p_dt)
    for row in positions:
        r_dt = _parse_iso_ts(row.get("updated_at"))
        if r_dt is not None:
            values.append(r_dt)
    return max(values) if values else None


def _signature_positions_from_editor(editor_df: pd.DataFrame) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in editor_df.to_dict("records"):
        code = str(row.get("代码", "")).strip()
        if not re.fullmatch(r"\d{6}", code):
            continue
        if bool(row.get("删除", False)):
            continue
        shares = int(_to_float(row.get("数量", 0), 0))
        if shares <= 0:
            continue
        items.append(
            {
                "code": code,
                "shares": shares,
                "cost_price": _to_float(row.get("成本", 0.0), 0.0),
                "buy_dt": _format_buy_dt(row.get("建仓时间")),
            }
        )
    return items


def _is_edit_blackout_now(now_dt: datetime | None = None) -> tuple[bool, str]:
    now_dt = now_dt or datetime.now(CN_TZ)
    if now_dt.weekday() >= 5:
        return False, ""
    current_minutes = now_dt.hour * 60 + now_dt.minute
    for (start_h, start_m), (end_h, end_m), label in EDIT_BLACKOUT_WINDOWS:
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m
        if start_minutes <= current_minutes <= end_minutes:
            return True, f"{label}（北京时间 {start_h:02d}:{start_m:02d}-{end_h:02d}:{end_m:02d}）"
    return False, ""


def _load_recent_orders(portfolio_id: str, limit: int = 200) -> list[dict[str, Any]]:
    supabase = get_supabase_client()
    resp = (
        supabase.table(TABLE_TRADE_ORDERS)
        .select(
            "id,run_id,trade_date,model,market_view,code,name,action,status,shares,"
            "price_hint,amount,stop_loss,reason,tape_condition,invalidate_condition,created_at"
        )
        .eq("portfolio_id", portfolio_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data or []


def _summarize_order_runs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    runs: dict[str, dict[str, Any]] = {}
    for row in rows:
        run_id = str(row.get("run_id", "")).strip() or "UNKNOWN"
        created_at = _parse_iso_ts(row.get("created_at"))
        item = runs.get(run_id)
        if item is None:
            item = {
                "run_id": run_id,
                "trade_date": str(row.get("trade_date", "") or "").strip(),
                "model": str(row.get("model", "") or "").strip(),
                "market_view": str(row.get("market_view", "") or "").strip(),
                "created_at": created_at,
                "rows": [],
                "active_count": 0,
                "cancelled_count": 0,
                "state_signature": extract_state_signature_from_run_id(run_id),
            }
            runs[run_id] = item
        item["rows"].append(row)
        status = str(row.get("status", "") or "").strip().upper()
        if status in {"CANCELLED", "CANCELED"}:
            item["cancelled_count"] += 1
        else:
            item["active_count"] += 1
        if created_at is not None and (item["created_at"] is None or created_at > item["created_at"]):
            item["created_at"] = created_at
    return sorted(
        runs.values(),
        key=lambda x: x.get("created_at").timestamp() if isinstance(x.get("created_at"), datetime) else float("-inf"),
        reverse=True,
    )


def _cancel_todays_orders(portfolio_id: str, trade_date: str) -> tuple[int, str]:
    supabase = get_supabase_client()
    try:
        rows = (
            supabase.table(TABLE_TRADE_ORDERS)
            .select("id,status")
            .eq("portfolio_id", portfolio_id)
            .eq("trade_date", trade_date)
            .limit(500)
            .execute()
        ).data or []
        active_ids = [
            row.get("id")
            for row in rows
            if str(row.get("status", "") or "").strip().upper() not in {"CANCELLED", "CANCELED"} and row.get("id")
        ]
        if not active_ids:
            return 0, ""
        # 批量更新：用 in_ 一次更新所有，避免 N+1
        (
            supabase.table(TABLE_TRADE_ORDERS)
            .update({"status": "CANCELLED"})
            .eq("portfolio_id", portfolio_id)
            .eq("trade_date", trade_date)
            .in_("id", active_ids)
            .execute()
        )
        return len(active_ids), ""
    except APIError as e:
        return 0, f"AI 建议作废失败: {e.code} - {e.message}"
    except Exception as e:
        return 0, f"AI 建议作废失败: {e}"


def _fmt_cn_dt_short(dt: datetime | None) -> str:
    if not isinstance(dt, datetime):
        return "-"
    return dt.astimezone(CN_TZ).strftime("%Y%m%d %H:%M")


def _render_notice(kind: str, text: str) -> None:
    tone = str(kind or "").strip().lower()
    if tone not in {"info", "success", "warning", "danger"}:
        tone = "info"
    import html as _html

    safe_text = _html.escape(str(text))
    st.markdown(
        f"""
<div class="portfolio-notice portfolio-notice-{tone}">
  <div class="notice-text">{safe_text}</div>
</div>
        """,
        unsafe_allow_html=True,
    )


def _load_user_live(portfolio_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    supabase = get_supabase_client()

    p_resp = (
        supabase.table(TABLE_PORTFOLIOS)
        .select("portfolio_id,name,free_cash,total_equity,updated_at")
        .eq("portfolio_id", portfolio_id)
        .limit(1)
        .execute()
    )
    if not p_resp.data:
        supabase.table(TABLE_PORTFOLIOS).upsert(
            {
                "portfolio_id": portfolio_id,
                "name": "Real Portfolio",
                "free_cash": 0.0,
                "total_equity": None,
                "updated_at": datetime.now(UTC).isoformat(),
            },
            on_conflict="portfolio_id",
        ).execute()
        portfolio = {
            "portfolio_id": portfolio_id,
            "name": "Real Portfolio",
            "free_cash": 0.0,
            "total_equity": None,
            "updated_at": "",
        }
    else:
        portfolio = p_resp.data[0]

    pos_resp = (
        supabase.table(TABLE_POSITIONS)
        .select("code,name,shares,cost_price,buy_dt,updated_at")
        .eq("portfolio_id", portfolio_id)
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
                "删除": False,
            }
        )
    return pd.DataFrame(data)


def _save_user_live(
    *,
    portfolio_id: str,
    free_cash: float,
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
        buy_dt = _format_buy_dt(row.get("建仓时间"))

        if cost_price < 0:
            errors.append(f"第 {idx} 行成本不能为负")
            continue

        # 删除勾选或数量<=0 都视为清仓
        if mark_delete or shares <= 0:
            deleted_codes.add(code)
            continue

        payload_by_code[code] = {
            "portfolio_id": portfolio_id,
            "code": code,
            "name": name,
            "shares": shares,
            "cost_price": cost_price,
            "buy_dt": buy_dt,
            "updated_at": datetime.now(UTC).isoformat(),
        }

    if errors:
        return False, "；".join(errors)

    keep_codes = set(payload_by_code.keys())
    delete_codes = (existing_codes - keep_codes) | deleted_codes
    positions_cost_value = sum(
        float(v.get("cost_price", 0.0) or 0.0) * int(v.get("shares", 0) or 0) for v in payload_by_code.values()
    )
    computed_total_equity = float(free_cash) + float(positions_cost_value)

    try:
        supabase.table(TABLE_PORTFOLIOS).upsert(
            {
                "portfolio_id": portfolio_id,
                "name": "Real Portfolio",
                "free_cash": float(free_cash),
                "total_equity": computed_total_equity,
                "updated_at": datetime.now(UTC).isoformat(),
            },
            on_conflict="portfolio_id",
        ).execute()

        for code in sorted(delete_codes):
            (supabase.table(TABLE_POSITIONS).delete().eq("portfolio_id", portfolio_id).eq("code", code).execute())

        if payload_by_code:
            supabase.table(TABLE_POSITIONS).upsert(
                list(payload_by_code.values()),
                on_conflict="portfolio_id,code",
            ).execute()
        return (
            True,
            f"保存成功：持仓 {len(payload_by_code)} 只，删除 {len(delete_codes)} 只，总资产={computed_total_equity:.2f}",
        )
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
.portfolio-notice {
  border-radius: 16px;
  padding: 14px 16px;
  border: 1px solid #d9dee8;
  background: linear-gradient(135deg, #f7f9fc 0%, #eef3fb 100%);
  margin-bottom: 14px;
}
.portfolio-notice .notice-text {
  color: #1e2b45;
  font-size: 15px;
  line-height: 1.45;
  font-weight: 520;
}
.portfolio-notice-info {
  border-color: #cfd9ea;
  background: linear-gradient(135deg, #f6f8fc 0%, #ecf2fb 100%);
}
.portfolio-notice-success {
  border-color: #cfe6d5;
  background: linear-gradient(135deg, #f6fbf7 0%, #eaf6ee 100%);
}
.portfolio-notice-warning {
  border-color: #ecd7b0;
  background: linear-gradient(135deg, #fffaf0 0%, #fff2d9 100%);
}
.portfolio-notice-danger {
  border-color: #ebc9c9;
  background: linear-gradient(135deg, #fff6f6 0%, #fdeaea 100%);
}
.portfolio-run-summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin: 10px 0 14px;
}
.portfolio-run-card {
  border: 1px solid #e6e9f0;
  border-radius: 16px;
  background: #fbfcfe;
  padding: 14px 16px;
}
.portfolio-run-card .label {
  color: #7a808c;
  font-size: 13px;
  line-height: 1.3;
  margin-bottom: 8px;
}
.portfolio-run-card .value {
  color: #1f2430;
  font-size: 26px;
  line-height: 1.15;
  font-weight: 650;
  word-break: break-word;
}
.portfolio-section-title {
  color: #1f2430;
  font-size: 18px;
  font-weight: 650;
  margin: 18px 0 6px;
}
@media (max-width: 960px) {
  .portfolio-summary {
    grid-template-columns: 1fr;
  }
  .portfolio-run-summary {
    grid-template-columns: 1fr;
  }
}
</style>
        """,
        unsafe_allow_html=True,
    )

    st.title("💼 持仓管理")
    portfolio_id = _current_portfolio_id()
    if not portfolio_id:
        st.error("无法识别当前用户，已拒绝加载持仓信息。请重新登录。")
        st.stop()

    loading = show_page_loading(title="思考中...", subtitle="正在读取当前账号持仓")
    try:
        portfolio, positions = _load_user_live(portfolio_id)
    finally:
        loading.empty()

    existing_codes = {str(x.get("code", "")).strip() for x in positions}
    free_cash_initial = _to_float(portfolio.get("free_cash", 0.0), 0.0)
    positions_value_est = _estimate_positions_value(positions)
    display_total_equity = free_cash_initial + positions_value_est
    holding_count = len([p for p in positions if int(_to_float(p.get("shares", 0), 0)) > 0])

    st.markdown(
        f"""
<div class="portfolio-summary">
  <div class="portfolio-card">
    <div class="label">总资产（成本估算）</div>
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

    current_signature = compute_portfolio_state_signature(free_cash_initial, positions)
    state_updated_at = _calc_state_updated_at(portfolio, positions)
    current_trade_date = resolve_end_calendar_day().strftime("%Y-%m-%d")
    edit_locked, edit_locked_reason = _is_edit_blackout_now()

    order_rows: list[dict[str, Any]] = []
    order_error = ""
    try:
        order_rows = _load_recent_orders(portfolio_id)
    except APIError as e:
        order_error = f"AI 建议读取失败: {e.code} - {e.message}"
    except Exception as e:
        order_error = f"AI 建议读取失败: {e}"

    order_runs = _summarize_order_runs(order_rows)
    latest_run = order_runs[0] if order_runs else None
    latest_active_run = next((run for run in order_runs if int(run.get("active_count", 0)) > 0), None)
    latest_active_sig = (
        str(latest_active_run.get("state_signature", "") or "").strip().lower() if latest_active_run else ""
    )
    latest_active_created = (
        latest_active_run.get("created_at")
        if latest_active_run and isinstance(latest_active_run.get("created_at"), datetime)
        else None
    )
    active_order_stale = False
    if latest_active_run:
        if latest_active_sig:
            active_order_stale = latest_active_sig != current_signature
        elif state_updated_at is not None and latest_active_created is not None:
            active_order_stale = state_updated_at > latest_active_created

    flash_notice = str(st.session_state.pop("portfolio_flash_notice", "") or "").strip()
    flash_warning = str(st.session_state.pop("portfolio_flash_warning", "") or "").strip()
    if flash_notice:
        _render_notice("success", flash_notice)
    if flash_warning:
        _render_notice("warning", flash_warning)

    tab_edit, tab_orders = st.tabs(["📝 持仓编辑", "📋 AI 建议"])

    with tab_edit:
        st.caption("当前页仅显示当前登录账号持仓。编辑过程中不会自动刷新，点击保存后才会提交并重载。")
        if edit_locked:
            _render_notice(
                "warning", f"当前处于编辑禁区：{edit_locked_reason}。为避免与定时任务冲突，暂时禁止修改持仓。"
            )
        elif latest_active_run and active_order_stale:
            _render_notice("warning", "检测到当前持仓已与最新 AI 建议脱节。保存持仓后，系统会自动作废当日旧建议。")
        elif latest_active_run:
            _render_notice("success", "当前 AI 建议与最新持仓状态一致，可在下方继续编辑。")

        with st.form("portfolio_edit_form", clear_on_submit=False):
            free_cash_input = st.text_input(
                "现金",
                value=f"{free_cash_initial:.2f}",
                help="用于 Step4 的可用现金",
                disabled=edit_locked,
            )

            st.markdown('<div class="portfolio-section-title">持仓股</div>', unsafe_allow_html=True)
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
                    "删除": st.column_config.CheckboxColumn("删除", default=False),
                },
                key="portfolio_editor",
                disabled=edit_locked,
            )

            submitted = st.form_submit_button(
                "💾 保存当前账号持仓",
                use_container_width=True,
                disabled=edit_locked,
            )
            if submitted:
                try:
                    free_cash_value = _parse_money_input(free_cash_input, "现金")
                except ValueError as e:
                    st.error(str(e))
                    free_cash_value = None

                if free_cash_value is not None:
                    next_signature = compute_portfolio_state_signature(
                        free_cash_value,
                        _signature_positions_from_editor(editor_df),
                    )
                    signature_changed = next_signature != current_signature
                    should_cancel_orders = signature_changed or active_order_stale

                    loader = show_page_loading(title="保存中...", subtitle="正在写入 Supabase")
                    try:
                        ok, msg = _save_user_live(
                            portfolio_id=portfolio_id,
                            free_cash=free_cash_value,
                            editor_df=editor_df,
                            existing_codes=existing_codes,
                        )
                    finally:
                        loader.empty()
                    if ok:
                        notice = msg
                        warning_msg = ""
                        if should_cancel_orders:
                            cancelled_count, cancel_err = _cancel_todays_orders(
                                portfolio_id,
                                current_trade_date,
                            )
                            if cancelled_count:
                                notice += f"；已作废当日 {cancelled_count} 条旧 AI 建议"
                            if cancel_err:
                                warning_msg = cancel_err
                        st.session_state["portfolio_flash_notice"] = notice
                        if warning_msg:
                            st.session_state["portfolio_flash_warning"] = warning_msg
                        st.rerun()
                    else:
                        st.error(msg)

    with tab_orders:
        st.caption("展示当前账号最近的 AI 订单建议。若持仓已变更而建议未刷新，这里会直接提示。")
        if order_error:
            _render_notice("warning", order_error)
        elif not order_rows:
            _render_notice("info", "暂无 AI 建议记录。")
        else:
            ref_run = latest_active_run or latest_run
            ref_created = ref_run.get("created_at") if ref_run else None
            ref_created_text = _fmt_cn_dt_short(ref_created)
            st.markdown(
                f"""
<div class="portfolio-run-summary">
  <div class="portfolio-run-card">
    <div class="label">最近运行时间</div>
    <div class="value">{ref_created_text}</div>
  </div>
  <div class="portfolio-run-card">
    <div class="label">当前有效建议</div>
    <div class="value">{int(latest_active_run.get("active_count", 0)) if latest_active_run else 0}</div>
  </div>
  <div class="portfolio-run-card">
    <div class="label">最近运行作废数</div>
    <div class="value">{int(ref_run.get("cancelled_count", 0)) if ref_run else 0}</div>
  </div>
</div>
                """,
                unsafe_allow_html=True,
            )

            if latest_active_run and active_order_stale:
                _render_notice("warning", "最新有效 AI 建议基于旧持仓生成，当前已过时。保存持仓后旧建议会自动作废。")
            elif latest_active_run:
                _render_notice("success", "最新有效 AI 建议与当前持仓一致。")
            else:
                _render_notice("info", "当前没有有效 AI 建议，最近记录可能都已被作废。")

            run_label = "最新有效 run" if latest_active_run else "最近 run"
            st.markdown(
                f"**{run_label}**: `{str(ref_run.get('run_id', '') or '-')}`  |  "
                f"trade_date=`{str(ref_run.get('trade_date', '') or '-')}`  |  "
                f"model=`{str(ref_run.get('model', '') or '-')}`"
            )
            market_view = str(ref_run.get("market_view", "") or "").strip()
            if market_view:
                st.caption(f"市场判断：{market_view}")

            ref_rows = list(ref_run.get("rows", [])) if ref_run else list(order_rows)
            ref_df = pd.DataFrame(ref_rows).copy()
            if not ref_df.empty:
                ref_df["持仓关联"] = (
                    ref_df["code"].astype(str).apply(lambda x: "当前持仓" if x in existing_codes else "已不在持仓")
                )
                ref_df["生成时间"] = ref_df["created_at"].apply(lambda x: _fmt_cn_dt_short(_parse_iso_ts(x)))
                display_cols = [
                    "code",
                    "name",
                    "action",
                    "status",
                    "持仓关联",
                    "shares",
                    "price_hint",
                    "amount",
                    "stop_loss",
                    "reason",
                    "tape_condition",
                    "invalidate_condition",
                    "生成时间",
                ]
                rename_map = {
                    "code": "代码",
                    "name": "名称",
                    "action": "动作",
                    "status": "状态",
                    "shares": "数量",
                    "price_hint": "参考价",
                    "amount": "金额",
                    "stop_loss": "止损",
                    "reason": "理由",
                    "tape_condition": "触发条件",
                    "invalidate_condition": "证伪条件",
                }
                st.dataframe(
                    ref_df[display_cols].rename(columns=rename_map),
                    use_container_width=True,
                    hide_index=True,
                )

            if len(order_runs) > 1:
                with st.expander("查看更多历史 run"):
                    history_df = pd.DataFrame(
                        [
                            {
                                "run_id": run.get("run_id"),
                                "trade_date": run.get("trade_date"),
                                "model": run.get("model"),
                                "active_count": run.get("active_count"),
                                "cancelled_count": run.get("cancelled_count"),
                                "created_at": _fmt_cn_dt_short(run.get("created_at")),
                                "state_sig": run.get("state_signature") or "-",
                            }
                            for run in order_runs
                        ]
                    )
                    st.dataframe(
                        history_df.rename(
                            columns={
                                "run_id": "Run ID",
                                "trade_date": "交易日",
                                "model": "模型",
                                "active_count": "有效建议数",
                                "cancelled_count": "作废数",
                                "created_at": "生成时间",
                                "state_sig": "持仓签名",
                            }
                        ),
                        use_container_width=True,
                        hide_index=True,
                    )

    if st.button("🔄 重新加载", use_container_width=True):
        st.rerun()
