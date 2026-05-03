"""推荐跟踪页面。"""

from datetime import date, datetime

import pandas as pd
import streamlit as st

from app.layout import setup_page
from app.navigation import show_right_nav
from app.ui_helpers import show_page_loading
from integrations.supabase_client import get_supabase_client
from integrations.supabase_recommendation import load_recommendation_tracking

setup_page(page_title="推荐跟踪", page_icon="🎯")


def _format_pct(val):
    if val is None:
        return "-"
    color = "red" if val > 0 else "green" if val < 0 else "gray"
    return f":{color}[{val:+.2f}%]"


content_col = show_right_nav()

with content_col:
    st.title("🎯 推荐跟踪")
    st.markdown("记录每日定时任务生成的威科夫推荐股票，并跟踪其后续的表现。数据在每日定时任务后自动刷新。")

    # 1. 加载数据
    loading = show_page_loading(title="思考中...", subtitle="从数据库加载推荐历史")
    try:
        raw_data = load_recommendation_tracking(limit=2000, client=get_supabase_client())
    finally:
        loading.empty()

    if not raw_data:
        st.info("目前暂无推荐跟踪数据，请等待下一次定时任务运行。")
        st.stop()

    # 2. 转换数据为 DataFrame 并处理展示逻辑
    df = pd.DataFrame(raw_data)
    if "recommend_date" not in df.columns:
        st.error("推荐跟踪数据缺少 recommend_date 字段，请检查 recommendation_tracking 表结构或 RLS 权限。")
        st.caption("返回字段：" + ", ".join(str(c) for c in df.columns.tolist()))
        st.stop()
    if "code" not in df.columns:
        st.error("推荐跟踪数据缺少 code 字段，请检查 recommendation_tracking 表结构或 RLS 权限。")
        st.caption("返回字段：" + ", ".join(str(c) for c in df.columns.tolist()))
        st.stop()
    if "name" not in df.columns:
        df["name"] = ""
    if "recommend_reason" not in df.columns:
        df["recommend_reason"] = ""
    if "is_ai_recommended" not in df.columns:
        df["is_ai_recommended"] = False
    if "funnel_score" not in df.columns:
        df["funnel_score"] = pd.NA
    df["is_ai_recommended"] = (
        df["is_ai_recommended"].apply(lambda x: str(x).strip().lower() in {"1", "true", "t", "yes", "y"}).astype(bool)
    )
    df["funnel_score"] = pd.to_numeric(df["funnel_score"], errors="coerce")
    df["initial_price"] = pd.to_numeric(df.get("initial_price"), errors="coerce")
    df["current_price"] = pd.to_numeric(df.get("current_price"), errors="coerce")
    df["change_pct"] = pd.to_numeric(df.get("change_pct"), errors="coerce")
    if "recommend_count" not in df.columns:
        df["recommend_count"] = 1
    df["recommend_count"] = pd.to_numeric(df.get("recommend_count"), errors="coerce").fillna(1).astype(int)
    df["recommend_date"] = pd.to_numeric(df.get("recommend_date"), errors="coerce").fillna(0).astype(int)

    # 格式化日期 (INT YYYYMMDD -> YYYY-MM-DD str)
    def _parse_date(v: int):
        s = str(int(v))
        if len(s) == 8 and s.isdigit():
            try:
                return datetime.strptime(s, "%Y%m%d").date()
            except Exception:
                return None
        return None

    def _format_date(v: int) -> str:
        dt = _parse_date(v)
        if dt is not None:
            return dt.strftime("%Y-%m-%d")
        return "-"

    today = date.today()
    df["recommend_date_dt"] = df["recommend_date"].apply(_parse_date)
    df["recommend_date_str"] = df["recommend_date"].apply(_format_date)

    # 格式化代码 (INT -> 000001 str)
    df["code"] = pd.to_numeric(df.get("code"), errors="coerce").fillna(0).astype(int)
    df["display_code"] = df["code"].apply(lambda x: f"{int(x):06d}")

    # ── 同一只股票去重：按 code 聚合，只保留最新一条，推荐次数取最大值 ──
    df = df.sort_values("recommend_date", ascending=False)
    agg_map = {
        "name": "first",
        "recommend_date": "first",
        "recommend_date_dt": "first",
        "recommend_date_str": "first",
        "initial_price": "first",
        "current_price": "first",
        "change_pct": "first",
        "is_ai_recommended": "any",  # 任意一次被 AI 推荐过就标 True
        "recommend_count": "max",  # 取最大推荐次数
        "recommend_reason": "first",
        "funnel_score": "first",
        "display_code": "first",
    }
    # 只聚合存在的列
    agg_map = {k: v for k, v in agg_map.items() if k in df.columns}
    df = df.groupby("code", as_index=False).agg(agg_map)

    df["days_since_recommend"] = df["recommend_date_dt"].apply(lambda d: (today - d).days if d is not None else pd.NA)

    # 3. 统计指标 (KPIs)
    st.markdown("### 📊 表现摘要")
    col1, col2, col3, col4, col5 = st.columns(5)
    avg_change = df["change_pct"].mean()
    max_change = df["change_pct"].max()
    min_change = df["change_pct"].min()
    total_recommend_events = int(df["recommend_count"].sum())

    col1.metric("覆盖股票数", f"{len(df)} 支")
    col2.metric("平均表现", f"{avg_change:+.2f}%")
    col3.metric("最高涨幅", f"{max_change:+.2f}%")
    col4.metric("最大跌幅", f"{min_change:+.2f}%")
    col5.metric("总推荐次数", f"{total_recommend_events} 次")

    st.divider()

    # 4. 搜索与排序增强
    st.markdown("### 🔍 筛选与搜索")
    search_col, ai_col, sort_col, order_col = st.columns([2, 1, 1, 1])

    with search_col:
        search_query = st.text_input("搜索代码或名字", placeholder="输入 000001 或 平安银行...", key="rec_search")

    with ai_col:
        only_ai = st.checkbox("只看AI推荐", value=False, key="rec_only_ai")

    with sort_col:
        sort_by = st.selectbox(
            "排序",
            options=["默认（涨幅→推荐次数→AI→日期→分值→现价）", "推荐日期", "涨跌幅", "分值", "代码"],
            index=0,
        )
    with order_col:
        sort_order = st.radio("顺序", options=["降序", "升序"], horizontal=True)

    # 筛选
    filtered_df = df.copy()
    if search_query:
        filtered_df = filtered_df[
            (filtered_df["display_code"].str.contains(search_query, na=False))
            | (filtered_df["name"].astype(str).str.contains(search_query, na=False))
        ]
    if only_ai:
        filtered_df = filtered_df[filtered_df["is_ai_recommended"]]

    # 排序：默认多级（涨幅高→低，同涨幅推荐次数高→低，同则 AI 在上，同则日期新→旧，同则分值高→低，同则现价高→低）
    if sort_by == "默认（涨幅→推荐次数→AI→日期→分值→现价）":
        filtered_df = filtered_df.sort_values(
            by=[
                "change_pct",
                "recommend_count",
                "is_ai_recommended",
                "recommend_date",
                "funnel_score",
                "current_price",
            ],
            ascending=[False, False, False, False, False, False],
            na_position="last",
        )
    else:
        sort_map = {
            "推荐日期": "recommend_date",
            "涨跌幅": "change_pct",
            "分值": "funnel_score",
            "代码": "code",
        }
        filtered_df = filtered_df.sort_values(
            by=sort_map[sort_by],
            ascending=(sort_order == "升序"),
            na_position="last",
        )

    # 5. 结果展示
    # 构建最终展示的列表
    display_df = filtered_df[
        [
            "display_code",
            "name",
            "change_pct",
            "initial_price",
            "current_price",
            "is_ai_recommended",
            "recommend_count",
            "days_since_recommend",
            "recommend_date_str",
            "recommend_reason",
            "funnel_score",
        ]
    ].copy()
    display_df["is_ai_recommended"] = display_df["is_ai_recommended"].map(lambda x: "是" if bool(x) else "否")

    display_df.columns = [
        "代码",
        "名称",
        "累计涨跌幅",
        "加入价",
        "当前价",
        "AI推荐",
        "推荐次数",
        "加入推荐天数",
        "推荐日期",
        "推荐原因",
        "推荐分值",
    ]

    # 使用 dataframe 渲染，增加一些样式建议
    st.dataframe(
        display_df.style.format(
            {
                "加入推荐天数": lambda v: "-" if pd.isna(v) else f"{int(v)}",
                "推荐分值": lambda v: "-" if pd.isna(v) else f"{float(v):.2f}",
                "加入价": "{:.2f}",
                "当前价": "{:.2f}",
                "累计涨跌幅": "{:+.2f}%",
            }
        )
        .map(
            lambda v: (
                "color: red;"
                if isinstance(v, str) and "+" in v
                else ("color: green;" if isinstance(v, str) and "-" in v else "")
            ),
            subset=["累计涨跌幅"],
        )
        .map(lambda v: "color: #16a34a; font-weight: 700;" if v == "是" else "color: #6b7280;", subset=["AI推荐"]),
        use_container_width=True,
        hide_index=True,
        height=600,
    )

    if st.button("🔄 手动刷新数据"):
        st.rerun()
