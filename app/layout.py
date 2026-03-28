import html

import streamlit as st

from app.auth_component import check_auth, login_form
from core.token_storage import restore_tokens_from_storage
from integrations.supabase_market_signal import compose_market_banner, load_latest_market_signal_daily
from integrations.llm_client import OPENAI_COMPATIBLE_BASE_URLS

def _set_default(key: str, value) -> None:
    if key not in st.session_state or st.session_state.get(key) is None:
        st.session_state[key] = value


def init_session_state() -> None:
    _set_default("user", None)
    _set_default("access_token", None)
    _set_default("refresh_token", None)
    _set_default("search_history", [])
    _set_default("current_symbol", "300364")
    _set_default("should_run", False)
    _set_default("mobile_mode", False)
    _set_default("last_home_batch_key", "")
    _set_default("last_home_single_key", "")
    _set_default("last_custom_export_query", "")
    _set_default("custom_export_payload", None)
    _set_default("custom_export_source_id", "")
    _set_default("custom_export_selected_signature", "")
    _set_default("custom_export_selected_path", "")
    _set_default("wyckoff_payload", None)
    if "custom_export_df" in st.session_state:
        del st.session_state["custom_export_df"]

    # 用户敏感配置不从环境变量兜底，避免跨账号污染
    _set_default("feishu_webhook", "")
    if st.session_state.feishu_webhook is None:
        st.session_state.feishu_webhook = ""
    _set_default("wecom_webhook", "")
    if st.session_state.get("wecom_webhook") is None:
        st.session_state.wecom_webhook = ""
    _set_default("dingtalk_webhook", "")
    if st.session_state.get("dingtalk_webhook") is None:
        st.session_state.dingtalk_webhook = ""

    _set_default("gemini_api_key", "")
    if st.session_state.gemini_api_key is None:
        st.session_state.gemini_api_key = ""

    _set_default("tushare_token", "")
    if st.session_state.tushare_token is None:
        st.session_state.tushare_token = ""

    _set_default("gemini_model", "gemini-3.1-flash-lite-preview")
    if st.session_state.gemini_model is None:
        st.session_state.gemini_model = "gemini-3.1-flash-lite-preview"
    for key in (
        "openai_api_key",
        "openai_model",
        "zhipu_api_key",
        "zhipu_model",
        "minimax_api_key",
        "minimax_model",
        "deepseek_api_key",
        "deepseek_model",
        "qwen_api_key",
        "qwen_model",
        "kimi_api_key",
        "kimi_model",
        "volcengine_api_key",
        "volcengine_model",
    ):
        _set_default(key, "" if "model" not in key else "")
    for k in (
        "openai_model",
        "zhipu_model",
        "minimax_model",
        "deepseek_model",
        "qwen_model",
        "kimi_model",
        "volcengine_model",
    ):
        if st.session_state.get(k) is None:
            st.session_state[k] = ""
    _set_default("gemini_base_url", "")
    _set_default("openai_base_url", OPENAI_COMPATIBLE_BASE_URLS.get("openai", ""))
    _set_default("zhipu_base_url", OPENAI_COMPATIBLE_BASE_URLS.get("zhipu", ""))
    _set_default("minimax_base_url", OPENAI_COMPATIBLE_BASE_URLS.get("minimax", ""))
    _set_default("deepseek_base_url", OPENAI_COMPATIBLE_BASE_URLS.get("deepseek", ""))
    _set_default("qwen_base_url", OPENAI_COMPATIBLE_BASE_URLS.get("qwen", ""))
    _set_default("kimi_base_url", OPENAI_COMPATIBLE_BASE_URLS.get("kimi", ""))
    _set_default("volcengine_base_url", OPENAI_COMPATIBLE_BASE_URLS.get("volcengine", ""))

    _set_default("tg_bot_token", "")
    if st.session_state.tg_bot_token is None:
        st.session_state.tg_bot_token = ""

    _set_default("tg_chat_id", "")
    if st.session_state.tg_chat_id is None:
        st.session_state.tg_chat_id = ""

    # 从 localStorage 恢复 token（刷新页面后登录态保持）
    access = st.session_state.get("access_token") or ""
    refresh = st.session_state.get("refresh_token") or ""
    if (not access or not refresh) and not st.session_state.get("_token_restore_attempted"):
        try:
            st.session_state["_token_restore_attempted"] = True
            restored_access, restored_refresh = restore_tokens_from_storage()
            if restored_access and restored_refresh:
                st.session_state.access_token = restored_access
                st.session_state.refresh_token = restored_refresh
        except Exception:
            pass


def _inject_base_ui_css() -> None:
    """注入全局基础样式，统一中文字体与控件排版。"""
    st.markdown(
        """
<style>
:root {
  --app-font-stack: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei",
    "Noto Sans CJK SC", "Source Han Sans SC", -apple-system, BlinkMacSystemFont,
    "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}

[data-testid="stAppViewContainer"],
[data-testid="stSidebar"],
[data-testid="stHeader"] {
  font-family: var(--app-font-stack);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  text-rendering: optimizeLegibility;
}

h1, h2, h3 {
  letter-spacing: 0;
}

[data-testid="stNumberInput"] input {
  font-family: var(--app-font-stack);
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum" 1;
  line-height: 1.35;
}

[data-testid="stNumberInput"] button {
  line-height: 1;
  font-weight: 600;
}

[data-testid="stDataEditor"] * {
  font-family: var(--app-font-stack) !important;
}

[data-testid="stDataEditor"] [role="columnheader"],
[data-testid="stDataEditor"] [role="gridcell"] {
  line-height: 1.35;
}

.market-signal-banner {
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
  padding: 0.8rem 1rem 0.85rem;
  margin: 0.2rem 0 1rem;
  border-radius: 16px;
  border: 1px solid #e7eaf0;
  background: linear-gradient(180deg, #fbfcfe 0%, #f7f9fc 100%);
}

.market-signal-banner .ms-top {
  display: flex;
  align-items: center;
  gap: 0.7rem;
  justify-content: space-between;
  flex-wrap: wrap;
}

.market-signal-banner .ms-left {
  display: flex;
  align-items: center;
  gap: 0.7rem;
  min-width: 0;
}

.market-signal-banner .ms-title {
  font-size: 0.98rem;
  font-weight: 700;
  color: #1f2937;
  line-height: 1.35;
}

.market-signal-banner .ms-tag {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 60px;
  padding: 0.22rem 0.6rem;
  border-radius: 999px;
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.01em;
}

.market-signal-banner .ms-tag-severe {
  background: #fde8e8;
  color: #b42318;
}

.market-signal-banner .ms-tag-conservative {
  background: #fff1e6;
  color: #c4320a;
}

.market-signal-banner .ms-tag-cautious {
  background: #f2f4f7;
  color: #475467;
}

.market-signal-banner .ms-tag-cautious-positive {
  background: #ecfdf3;
  color: #027a48;
}

.market-signal-banner .ms-tag-positive {
  background: #e6f4ea;
  color: #166534;
}

.market-signal-banner .ms-chips {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.market-signal-banner .ms-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  padding: 0.18rem 0.55rem;
  border-radius: 999px;
  background: #ffffff;
  border: 1px solid #e6e9f0;
  color: #344054;
  font-size: 0.78rem;
  line-height: 1.25;
  transition: color 120ms ease;
}

.market-signal-banner .ms-chip-label {
  color: #667085;
  font-size: 0.78rem;
}

.market-signal-banner .ms-chip-value {
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  font-size: 0.86rem;
}

.market-signal-banner .ms-chip-positive {
  color: #c43221;
}

.market-signal-banner .ms-chip-positive .ms-chip-label {
  color: #e04f39;
}

.market-signal-banner .ms-chip-neutral {
  color: #475467;
}

.market-signal-banner .ms-chip-neutral .ms-chip-label {
  color: #667085;
}

.market-signal-banner .ms-chip-caution {
  color: #c2410c;
}

.market-signal-banner .ms-chip-caution .ms-chip-label {
  color: #ea580c;
}

.market-signal-banner .ms-chip-negative {
  color: #067647;
}

.market-signal-banner .ms-chip-negative .ms-chip-label {
  color: #039855;
}

.market-signal-banner .ms-body {
  color: #475467;
  font-size: 0.9rem;
  line-height: 1.5;
}

@media (max-width: 768px) {
  .market-signal-banner {
    padding: 0.75rem 0.85rem 0.8rem;
  }
  .market-signal-banner .ms-title {
    font-size: 0.92rem;
  }
  .market-signal-banner .ms-body {
    font-size: 0.86rem;
  }
}
</style>
        """,
        unsafe_allow_html=True,
    )


def _tone_slug(tone: str) -> str:
    mapping = {
        "恶劣": "severe",
        "保守": "conservative",
        "谨慎": "cautious",
        "谨慎乐观": "cautious-positive",
        "乐观": "positive",
    }
    return mapping.get(str(tone or "").strip(), "cautious")


def _benchmark_regime_cn(regime: str) -> str:
    mapping = {
        "RISK_ON": "偏强",
        "NEUTRAL": "中性",
        "RISK_OFF": "偏弱",
        "CRASH": "极弱",
        "BLACK_SWAN": "恶劣",
    }
    return mapping.get(str(regime or "").strip().upper(), "待确认")


def _benchmark_chip_tone(regime: str) -> str:
    normalized = str(regime or "").strip().upper()
    if normalized == "RISK_ON":
        return "positive"
    if normalized == "NEUTRAL":
        return "neutral"
    if normalized == "RISK_OFF":
        return "caution"
    if normalized in {"CRASH", "BLACK_SWAN"}:
        return "negative"
    return "neutral"


def _signed_chip_tone(raw) -> str:
    try:
        if raw is None or str(raw).strip() == "":
            return "neutral"
        value = float(raw)
    except Exception:
        return "neutral"
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "neutral"


def _vix_chip_tone(raw) -> str:
    try:
        if raw is None or str(raw).strip() == "":
            return "neutral"
        value = float(raw)
    except Exception:
        return "neutral"
    if value >= 15:
        return "negative"
    if value >= 8:
        return "caution"
    if value > 0:
        return "caution"
    if value < 0:
        return "positive"
    return "neutral"


def _fmt_date_ymd(raw) -> str:
    try:
        if raw is None:
            return "--"
        text = str(raw).strip()
        if not text:
            return "--"
        if len(text) >= 10 and "-" in text:
            return text[:10]
        if len(text) == 8 and text.isdigit():
            return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
        return text
    except Exception:
        return "--"


def _render_market_signal_banner() -> None:
    row = load_latest_market_signal_daily()
    if not isinstance(row, dict):
        return

    banner = compose_market_banner(row)
    tone = str(banner.get("banner_tone", "谨慎") or "谨慎").strip()
    title = str(banner.get("banner_title", "") or "").strip()
    body = str(banner.get("banner_message", "") or "").strip()
    benchmark_regime_raw = str(row.get("benchmark_regime", "") or "")
    regime = _benchmark_regime_cn(benchmark_regime_raw)
    benchmark_date = _fmt_date_ymd(row.get("trade_date"))
    a50_date = _fmt_date_ymd(row.get("a50_value_date") or row.get("trade_date"))
    vix_date = _fmt_date_ymd(row.get("vix_value_date") or row.get("trade_date"))
    main_close = row.get("main_index_close")
    a50_close = row.get("a50_close")
    a50_pct = row.get("a50_pct_chg")
    vix_close = row.get("vix_close")
    vix_pct = row.get("vix_pct_chg")

    def _fmt_pct(raw) -> str:
        try:
            if raw is None or str(raw).strip() == "":
                return "--"
            return f"{float(raw):+.2f}%"
        except Exception:
            return "--"

    def _fmt_plain(raw) -> str:
        try:
            if raw is None or str(raw).strip() == "":
                return "--"
            return f"{float(raw):.2f}"
        except Exception:
            return "--"

    chips = [
        (
            f"大盘水温（上证 {benchmark_date}）",
            f"{regime} {_fmt_plain(main_close)}",
            _benchmark_chip_tone(benchmark_regime_raw),
        ),
        (
            f"A50（盘前风向标 {a50_date}）",
            f"{_fmt_plain(a50_close)} / {_fmt_pct(a50_pct)}",
            _signed_chip_tone(a50_pct),
        ),
        (
            f"VIX（恐慌指数 {vix_date}）",
            f"{_fmt_plain(vix_close)} / {_fmt_pct(vix_pct)}",
            _vix_chip_tone(vix_pct),
        ),
    ]
    chips_html = "".join(
        (
            f'<span class="ms-chip ms-chip-{html.escape(chip_tone)}">'
            f'<span class="ms-chip-label">{html.escape(label)}</span>'
            f'<span class="ms-chip-value">{html.escape(value)}</span>'
            "</span>"
        )
        for label, value, chip_tone in chips
    )
    st.markdown(
        f"""
<div class="market-signal-banner">
  <div class="ms-top">
    <div class="ms-left">
      <span class="ms-tag ms-tag-{_tone_slug(tone)}">{html.escape(tone)}</span>
      <div class="ms-title">{html.escape(title or "亲爱的投资者，最新交易日市场信号已更新。")}</div>
    </div>
    <div class="ms-chips">{chips_html}</div>
  </div>
  <div class="ms-body">{html.escape(body)}</div>
</div>
        """,
        unsafe_allow_html=True,
    )


def require_auth() -> None:
    if check_auth():
        return
    empty_container = st.empty()
    with empty_container.container():
        login_form()
    st.stop()


def setup_page(
    *,
    page_title: str,
    page_icon: str,
    layout: str = "wide",
    require_login: bool = True,
) -> None:
    st.set_page_config(page_title=page_title, page_icon=page_icon, layout=layout)
    init_session_state()
    _inject_base_ui_css()
    if require_login:
        require_auth()
        _render_market_signal_banner()


def is_data_source_failure_message(msg: str) -> bool:
    """判断是否为数据源拉取失败提示（已标明失败数据源，非程序 bug）"""
    return "拉取失败（非程序错误）" in msg or ("免费数据源" in msg and "均" in msg)


def show_user_error(message: str, err: Exception | None = None) -> None:
    st.error(message)
    if err is not None:
        st.caption(f"详情: {err}")
