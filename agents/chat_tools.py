"""
ADK 工具函数 — 将已有的 Wyckoff 引擎能力暴露给对话 Agent。

每个函数都是一个 ADK tool：普通 Python 函数 + 类型标注 + docstring，
ADK 的 FunctionTool 会自动解析为工具 schema。

用户凭据（API Key / Tushare Token 等）按需从 Supabase 实时获取，
不依赖 st.session_state 或 os.environ 的长链传递。
"""

from __future__ import annotations

import ipaddress
import logging
import math
import os
import pathlib
import re
import shlex
import socket
import threading
import time
from contextlib import suppress
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

try:
    from google.adk.tools import ToolContext
except ImportError:
    # CLI 模式下无 ADK，使用 shim
    class ToolContext:  # type: ignore[no-redef]
        def __init__(self, state=None):
            self.state = state or {}


logger = logging.getLogger(__name__)

_MARKET_HISTORY_INDEXES = {
    "sse": ("000001.SH", "上证指数"),
    "csi300": ("000300.SH", "沪深300"),
    "szse": ("399001.SZ", "深证成指"),
    "chinext": ("399006.SZ", "创业板指"),
    "sse50": ("000016.SH", "上证50"),
    "csi500": ("000905.SH", "中证500"),
}
_MARKET_HISTORY_ALIASES = {
    "sh": "sse",
    "上证": "sse",
    "上证指数": "sse",
    "沪指": "sse",
    "沪深300": "csi300",
    "300": "csi300",
    "sz": "szse",
    "深证": "szse",
    "深成指": "szse",
    "深证成指": "szse",
    "创业板": "chinext",
    "创业板指": "chinext",
    "上证50": "sse50",
    "中证500": "csi500",
}
_NAME_MAP: dict[str, str] | None = None
_MAX_AGENT_FILE_BYTES = 50 * 1024 * 1024
_MAX_AGENT_TEXT_WRITE_BYTES = 2 * 1024 * 1024
_MAX_AGENT_WEB_BYTES = 1024 * 1024

_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|credential|authorization|cookie|session)",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|authorization|cookie)\b"
    r"\s*[:=]\s*([\"']?)[^\s\"',;]+"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")
_COMMON_SECRET_VALUE_RE = re.compile(r"\b(?:sk|ak|pk|ghp|gho|github_pat|glpat|xoxb|xoxp|AIza)[A-Za-z0-9_\-]{12,}\b")
_BLOCKED_PATH_PARTS = {
    ".ssh",
    ".aws",
    ".azure",
    ".config/gcloud",
    ".gnupg",
    ".kube",
    ".docker",
    ".npm",
}
_BLOCKED_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}
_ALLOWED_WYCKOFF_SUBDIRS = {"tool-results", "scratchpad", "exports", "reports"}
_BLOCKED_SYSTEM_ROOTS = (
    pathlib.Path("/bin"),
    pathlib.Path("/etc"),
    pathlib.Path("/Library"),
    pathlib.Path("/private/etc"),
    pathlib.Path("/sbin"),
    pathlib.Path("/System"),
    pathlib.Path("/usr"),
    pathlib.Path("/var"),
)
_BLOCKED_COMMANDS = {
    "bash",
    "chflags",
    "chmod",
    "chown",
    "curl",
    "dd",
    "ftp",
    "env",
    "kill",
    "launchctl",
    "mkfs",
    "nc",
    "ncat",
    "osascript",
    "pkill",
    "printenv",
    "rm",
    "rmdir",
    "rsync",
    "scp",
    "sh",
    "shred",
    "ssh",
    "su",
    "sudo",
    "wget",
    "zsh",
}
_INLINE_CODE_COMMANDS = {"python", "python3", "node", "ruby", "perl", "php"}
_SHELL_META_RE = re.compile(r"[\n\r;&|<>`]|(?<!\\)\$\(")
_SAFE_WRITE_SUFFIXES = {
    ".csv",
    ".html",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
}
_SAFE_WEB_CONTENT_TYPES = (
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "text/",
)


def _security_error(message: str) -> dict:
    return {"error": f"安全拦截: {message}"}


def _redact_sensitive_text(text: str) -> str:
    """Redact obvious credentials before model-facing tool output is returned."""

    if not text:
        return text
    redacted = _SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}={m.group(2)}***REDACTED***", text)
    redacted = _BEARER_RE.sub("Bearer ***REDACTED***", redacted)
    redacted = _COMMON_SECRET_VALUE_RE.sub("***REDACTED***", redacted)
    return redacted


def _redact_sensitive_columns(df: Any) -> Any:
    """Mask sensitive dataframe columns while preserving table shape."""

    try:
        out = df.copy()
        for col in out.columns:
            if _SENSITIVE_KEY_RE.search(str(col)):
                out[col] = "***REDACTED***"
        return out
    except Exception:
        return df


def _path_parts_lower(path: pathlib.Path) -> list[str]:
    return [part.lower() for part in path.parts]


def _is_allowed_wyckoff_path(parts: list[str]) -> bool:
    if ".wyckoff" not in parts:
        return False
    idx = parts.index(".wyckoff")
    return len(parts) > idx + 1 and parts[idx + 1] in _ALLOWED_WYCKOFF_SUBDIRS


def _is_under_path(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_agent_path(path: str, *, for_write: bool = False) -> pathlib.Path | dict:
    if not path or not str(path).strip():
        return _security_error("文件路径不能为空")

    try:
        p = pathlib.Path(path).expanduser().resolve()
    except Exception as e:
        return _security_error(f"文件路径无效: {e}")

    parts = _path_parts_lower(p)
    joined = "/".join(parts)
    name = p.name.lower()
    allowed_wyckoff_path = _is_allowed_wyckoff_path(parts)

    if any(_is_under_path(p, root) for root in _BLOCKED_SYSTEM_ROOTS):
        return _security_error("禁止访问系统目录")

    user_library = pathlib.Path.home().expanduser() / "Library"
    if _is_under_path(p, user_library):
        return _security_error("禁止访问用户 Library 配置目录")

    if ".wyckoff" in parts and not allowed_wyckoff_path:
        return _security_error("禁止读取或写入 Wyckoff 凭据、会话和配置目录")

    if any(part in parts for part in _BLOCKED_PATH_PARTS) or any(part in joined for part in _BLOCKED_PATH_PARTS):
        return _security_error("禁止访问凭据、密钥或云配置目录")

    hidden_parts = [part for part in parts if part.startswith(".") and part not in {".", "..", ".wyckoff"}]
    if hidden_parts and not allowed_wyckoff_path:
        return _security_error("禁止访问隐藏文件或隐藏目录")

    if name in _BLOCKED_FILE_NAMES or name.startswith(".env"):
        return _security_error("禁止访问环境变量或密钥文件")

    if _SENSITIVE_KEY_RE.search(name):
        return _security_error("文件名疑似包含凭据或会话数据")

    if for_write:
        suffix = p.suffix.lower()
        if suffix not in _SAFE_WRITE_SUFFIXES:
            allowed = ", ".join(sorted(_SAFE_WRITE_SUFFIXES))
            return _security_error(f"只允许写入文本/报告类文件: {allowed}")
    return p


def _validate_agent_command(command: str) -> list[str] | dict:
    raw = str(command or "").strip()
    if not raw:
        return _security_error("命令不能为空")
    if len(raw) > 500:
        return _security_error("命令过长，请拆成更小的只读操作")
    if _SHELL_META_RE.search(raw):
        return _security_error("禁止使用 shell 控制符、管道、重定向、命令替换或多条命令")

    try:
        args = shlex.split(raw)
    except ValueError as e:
        return _security_error(f"命令解析失败: {e}")
    if not args:
        return _security_error("命令不能为空")

    executable = pathlib.Path(args[0]).name.lower()
    if executable in _BLOCKED_COMMANDS:
        return _security_error(f"禁止通过 Agent 执行高风险命令: {executable}")
    if executable in _INLINE_CODE_COMMANDS and any(arg in {"-c", "-e"} for arg in args[1:]):
        return _security_error("禁止通过 Agent 执行内联代码")
    for arg in args[1:]:
        lowered = arg.lower()
        touches_wyckoff_config = ".wyckoff" in lowered and not any(
            f".wyckoff/{subdir}" in lowered for subdir in _ALLOWED_WYCKOFF_SUBDIRS
        )
        if _SENSITIVE_KEY_RE.search(arg) or ".ssh" in lowered or ".env" in lowered or touches_wyckoff_config:
            return _security_error("命令参数疑似访问凭据、会话或密钥")
    return args


def _validate_public_http_url(url: str) -> str | dict:
    raw = str(url or "").strip()
    if not raw:
        return _security_error("URL 不能为空")

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        return _security_error("只允许抓取 http/https URL")
    if parsed.username or parsed.password:
        return _security_error("URL 中禁止携带用户名或密码")
    if not parsed.hostname:
        return _security_error("URL 缺少主机名")
    if parsed.port and parsed.port not in {80, 443}:
        return _security_error("禁止抓取非标准端口，避免访问内网服务")

    host = parsed.hostname.strip().lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return _security_error("禁止抓取本机或本地域名")

    try:
        infos = socket.getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        return _security_error("URL 主机无法解析")

    for info in infos:
        ip_text = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            return _security_error("URL 解析到无效地址")
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return _security_error("禁止抓取内网、本机、链路本地或保留地址")
    return raw


def _code_to_name(code: str) -> str:
    """根据股票代码查名称，基于 get_all_stocks() + ETF 池缓存。"""
    global _NAME_MAP
    if _NAME_MAP is None:
        try:
            from integrations.fetch_a_share_csv import get_all_stocks

            _NAME_MAP = {s["code"]: s["name"] for s in get_all_stocks()}
        except Exception:
            _NAME_MAP = {}
        _NAME_MAP.update(_load_etf_name_map())
    return _NAME_MAP.get(code, code)


def _load_etf_name_map() -> dict[str, str]:
    """从 ETF meta 加载 ETF 代码→名称映射。"""
    from pathlib import Path

    try:
        from tools.market_universe_meta import load_symbol_name_map

        meta_map = load_symbol_name_map(("etf_cn",))
        out = {code: name for code, name in meta_map.items() if len(code) == 6 and code.isdigit()}
        if out:
            return out
    except Exception:
        logger.debug("failed to load ETF name map from symbol metadata", exc_info=True)

    path = Path(__file__).resolve().parent.parent / "data" / "market_universes" / "etf_cn.txt"
    if not path.is_file():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and len(parts[0]) == 6 and parts[0].isdigit():
            result[parts[0]] = f"{parts[1]}ETF"
    return result


def _collect_tickflow_limit_hints_from_df(df: Any) -> list[str]:
    if df is None or not hasattr(df, "attrs"):
        return []
    attrs = getattr(df, "attrs", {}) or {}
    hints = attrs.get("tickflow_limit_hints")
    if isinstance(hints, list):
        out: list[str] = []
        for item in hints:
            text = str(item or "").strip()
            if text and text not in out:
                out.append(text)
        if out:
            return out
    one = str(attrs.get("tickflow_limit_hint", "") or "").strip()
    return [one] if one else []


def _hist_metadata(df: Any) -> dict[str, Any]:
    """Return compact provenance metadata for model-facing tool results."""
    if df is None or not hasattr(df, "attrs"):
        return {}
    attrs = getattr(df, "attrs", {}) or {}
    meta: dict[str, Any] = {}
    for key in ("source", "upstream_source", "cache_status", "cached_until"):
        val = str(attrs.get(key, "") or "").strip()
        if val:
            meta[key] = val
    upstream_sources = attrs.get("upstream_sources")
    if isinstance(upstream_sources, list):
        clean = [str(x) for x in upstream_sources if str(x or "").strip()]
        if clean:
            meta["upstream_sources"] = clean
    with suppress(Exception):
        meta["row_count"] = int(len(df))
    return meta


def _latest_hist_date(df: Any, date_col: str = "date") -> str:
    if df is None or not hasattr(df, "empty") or df.empty:
        return ""
    try:
        return str(df.iloc[-1].get(date_col, "") or "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 用户凭据：从 Supabase 实时获取 + 进程内短期缓存
# ---------------------------------------------------------------------------

_cred_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cred_cache_lock = threading.Lock()
_CRED_CACHE_TTL = 300  # 5 分钟


def _load_user_credentials(user_id: str) -> dict[str, Any]:
    """
    从 Supabase user_settings 表获取用户凭据，带 5 分钟内存缓存。

    返回原始 row dict（可能为空 dict）。
    """
    if not user_id:
        return {}

    now = time.monotonic()
    with _cred_cache_lock:
        cached = _cred_cache.get(user_id)
        if cached and (now - cached[0]) < _CRED_CACHE_TTL:
            return cached[1]

    try:
        from integrations.supabase_portfolio import load_user_settings_admin

        row = load_user_settings_admin(user_id) or {}
    except Exception as e:
        logger.warning("_load_user_credentials failed for %s: %s", user_id, e)
        row = {}

    with _cred_cache_lock:
        _cred_cache[user_id] = (time.monotonic(), row)

    return row


_LOCAL_USER_ID = "local"


def _get_user_id(tool_context: ToolContext | None = None) -> str:
    """从 tool_context 获取 user_id，未登录时降级为本地用户。"""
    if tool_context is not None:
        uid = tool_context.state.get("user_id", "")
        if uid:
            return str(uid)
    return _LOCAL_USER_ID


def _has_cloud(tool_context: ToolContext | None) -> bool:
    """判断是否有 Supabase 云端写入能力。"""
    if not tool_context:
        return False
    return bool(tool_context.state.get("access_token", ""))


def _get_credential(tool_context: ToolContext | None, key: str, env_fallback: str = "") -> str:
    """
    获取用户凭据：Supabase → wyckoff.json → 环境变量 → 空串。

    Args:
        tool_context: ADK 注入的上下文（含 user_id）
        key: user_settings 表中的列名，如 "gemini_api_key" / "tushare_token"
        env_fallback: 可选的环境变量名作为兜底
    """
    user_id = _get_user_id(tool_context)
    if user_id:
        creds = _load_user_credentials(user_id)
        val = str(creds.get(key, "") or "").strip()
        if val:
            return val
    # 第二层：wyckoff.json 本地配置
    try:
        from cli.auth import load_config

        local_val = str(load_config().get(key, "") or "").strip()
        if local_val:
            return local_val
    except Exception:
        logger.debug("failed to load credential '%s' from local config", key, exc_info=True)
    # 兜底：环境变量（适用于本地开发 / 未登录场景）
    if env_fallback:
        return os.getenv(env_fallback, "").strip()
    return ""


def _resolve_llm_config(tool_context) -> tuple[str, str, str, str]:
    """解析用户配置的 LLM 凭据：(provider, api_key, model, base_url)。

    优先读 CLI 默认模型配置，其次 Supabase/env gemini 兜底。
    """
    try:
        from cli.auth import load_default_model_id, load_model_configs

        configs = load_model_configs()
        default_id = load_default_model_id()
        cfg = next((c for c in configs if c["id"] == default_id), None)
        if cfg and cfg.get("api_key"):
            prov = cfg.get("provider_name", "openai")
            from integrations._llm_types import OPENAI_COMPATIBLE_BASE_URLS

            base = cfg.get("base_url", "") or OPENAI_COMPATIBLE_BASE_URLS.get(prov, "")
            return prov, cfg["api_key"], cfg.get("model", ""), base
    except Exception:
        logger.debug("failed to load LLM provider config from local config", exc_info=True)
    api_key = _get_credential(tool_context, "gemini_api_key", "GEMINI_API_KEY")
    model = _get_credential(tool_context, "gemini_model", "GEMINI_MODEL") or "gemini-2.0-flash"
    base_url = _get_credential(tool_context, "gemini_base_url", "")
    return "gemini", api_key, model, base_url


def _ensure_tushare_token(tool_context: ToolContext | None) -> None:
    """确保 tushare 能拿到 token：从 Supabase 获取后设置到环境变量。

    tushare 库内部只认 ts.set_token() 或 TUSHARE_TOKEN 环境变量，
    无法通过函数参数传递，所以这里做一次即时注入。
    """
    token = _get_credential(tool_context, "tushare_token", "TUSHARE_TOKEN")
    if token:
        os.environ["TUSHARE_TOKEN"] = token


# ---------------------------------------------------------------------------
# Tool 1: 股票搜索
# ---------------------------------------------------------------------------


def search_stock_by_name(keyword: str, tool_context: ToolContext) -> list[dict]:
    """根据关键词搜索 A 股 / ETF / 美股 / 港股，支持名称和代码双向模糊搜索。

    Args:
        keyword: 搜索关键词，如 "宁德"、"300750"、"纳指100"、"苹果"、"AAPL.US" 或 "00700.HK"

    Returns:
        匹配的股票列表，每项包含 code、name、price、pct_chg、market_cap、news 字段。最多返回 10 条。
    """
    try:
        from integrations.fetch_a_share_csv import get_all_stocks

        kw = keyword.strip()
        stocks = get_all_stocks()
        results: list[dict] = []
        for s in stocks:
            code = s.get("code", "")
            name = s.get("name", "")
            if kw in name or kw in code:
                results.append({"code": code, "name": name})
                if len(results) >= 10:
                    break

        _enrich_search_results(results[:3])
        if len(results) < 10:
            results.extend(_search_market_universe_meta(kw, 10 - len(results)))
        if not results:
            return [{"message": f"未找到与 '{kw}' 匹配的股票"}]
        return results
    except Exception as e:
        logger.exception("search_stock_by_name error")
        return [{"error": str(e)}]


def _search_market_universe_meta(keyword: str, limit: int) -> list[dict]:
    """搜索结构化市场 meta，补充 ETF / 美股 / 港股结果。"""
    try:
        from tools.market_universe_meta import search_market_meta

        rows = search_market_meta(keyword, limit=max(limit, 0))
    except Exception:
        return []
    out: list[dict] = []
    for row in rows:
        symbol = str(row.get("symbol", "") or "")
        code = str(row.get("code", "") or "")
        out.append(
            {
                "code": code,
                "symbol": symbol,
                "name": str(row.get("name", "") or symbol or code),
                "market": str(row.get("market", "") or ""),
                "asset_type": str(row.get("asset_type", "") or ""),
                "currency": str(row.get("currency", "") or ""),
            }
        )
    return out


def _enrich_search_results(items: list[dict]) -> None:
    """为搜索结果前几条附加行情、市值、新闻。"""
    try:
        from integrations.data_source import fetch_stock_spot_snapshot
    except Exception:
        fetch_stock_spot_snapshot = None  # type: ignore[assignment]

    cap_map: dict[str, float] = {}
    try:
        from integrations.data_source import fetch_market_cap_map

        cap_map = fetch_market_cap_map()
    except Exception:
        logger.debug("failed to fetch market cap map", exc_info=True)

    for item in items:
        code = item["code"]
        if fetch_stock_spot_snapshot:
            try:
                snap = fetch_stock_spot_snapshot(code)
                if snap:
                    item["price"] = snap.get("close")
                    item["pct_chg"] = snap.get("pct_chg")
            except Exception:
                logger.debug("failed to fetch spot snapshot for %s", code, exc_info=True)
        if cap_map:
            item["market_cap_yi"] = cap_map.get(code)
        item["news"] = _fetch_news_with_timeout(code)


def _fetch_news_with_timeout(code: str, timeout: float = 5.0) -> list[str]:
    import concurrent.futures

    def _fetch():
        from datetime import datetime, timedelta

        import akshare as ak
        import pandas as pd

        df = ak.stock_news_em(symbol=code)
        cutoff = datetime.now() - timedelta(days=7)
        df["发布时间"] = pd.to_datetime(df["发布时间"])
        recent = df[df["发布时间"] >= cutoff]
        return recent["新闻标题"].head(5).tolist()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_fetch)
            return future.result(timeout=timeout)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Tool 2: 个股分析（合并原 diagnose_stock + get_stock_price）
# ---------------------------------------------------------------------------


def analyze_stock(
    code: str, mode: str = "diagnose", cost: float = 0.0, days: int = 30, tool_context: ToolContext = None
) -> dict:
    """分析单只 A 股股票：Wyckoff 健康诊断或近期行情查询。

    Args:
        code: 6 位股票代码，如 "000001" 或 "600519"
        mode: "diagnose" 做 Wyckoff 结构化健康诊断；"price" 仅返回近期 OHLCV 行情数据
        cost: 持仓成本价，仅 diagnose 模式使用，默认 0 表示未持仓
        days: 获取天数，仅 price 模式使用，默认 30，最大 250

    Returns:
        诊断结果或行情数据 dict。
    """
    try:
        _ensure_tushare_token(tool_context)
        from core.stock_cache import _COL_MAP
        from integrations.stock_hist_repository import get_stock_hist

        mode = (mode or "diagnose").strip().lower()
        if mode not in ("diagnose", "price"):
            return {"error": f"mode 参数无效: '{mode}'，可选值: diagnose, price"}
        end_date = date.today()

        if mode == "price":
            days = min(max(days, 1), 250)
            start_date = end_date - timedelta(days=int(days * 1.6))
            df = get_stock_hist(code, start_date, end_date, user_id=_get_user_id(tool_context))
            if df is None or df.empty:
                return {"error": f"无法获取 {code} 的行情数据"}
            hist_hints = _collect_tickflow_limit_hints_from_df(df)
            hist_meta = _hist_metadata(df)
            df = df.rename(columns=_COL_MAP)
            df = df.tail(days)
            latest = df.iloc[-1] if len(df) > 0 else {}
            records = []
            for _, row in df.iterrows():
                records.append(
                    {
                        "date": str(row.get("date", "")),
                        "open": round(float(row.get("open", 0)), 2),
                        "high": round(float(row.get("high", 0)), 2),
                        "low": round(float(row.get("low", 0)), 2),
                        "close": round(float(row.get("close", 0)), 2),
                        "volume": int(row.get("volume", 0)),
                        "pct_chg": round(float(row.get("pct_chg", 0)), 2),
                    }
                )
            return {
                "code": code,
                "days": len(records),
                "latest_close": round(float(latest.get("close", 0)), 2),
                "latest_date": str(latest.get("date", "")),
                "data_status": "ok",
                **hist_meta,
                "data": records,
                **({"tickflow_limit_hint": hist_hints[0]} if hist_hints else {}),
            }

        # mode == "diagnose"
        from core.holding_diagnostic import diagnose_one_stock, format_diagnostic_text

        start_date = end_date - timedelta(days=500)
        df = get_stock_hist(code, start_date, end_date, user_id=_get_user_id(tool_context))
        if df is None or df.empty:
            return {"error": f"无法获取 {code} 的行情数据"}
        hist_hints = _collect_tickflow_limit_hints_from_df(df)
        hist_meta = _hist_metadata(df)
        latest_date = _latest_hist_date(df, "日期")
        df = df.rename(columns=_COL_MAP)
        name = _code_to_name(code)
        d = diagnose_one_stock(code, name, cost, df)
        text = format_diagnostic_text(d)
        return {
            "code": d.code,
            "name": d.name,
            "health": d.health,
            "pnl_pct": round(d.pnl_pct, 2),
            "latest_close": d.latest_close,
            "ma_pattern": d.ma_pattern,
            "l2_channel": d.l2_channel,
            "track": d.track,
            "accum_stage": d.accum_stage,
            "l4_triggers": d.l4_triggers,
            "exit_signal": d.exit_signal,
            "stop_loss_status": d.stop_loss_status,
            "vol_ratio_20_60": round(d.vol_ratio_20_60, 2),
            "range_60d_pct": round(d.range_60d_pct, 1),
            "ret_10d_pct": round(d.ret_10d_pct, 1),
            "ret_20d_pct": round(d.ret_20d_pct, 1),
            "from_year_high_pct": round(d.from_year_high_pct, 1),
            "from_year_low_pct": round(d.from_year_low_pct, 1),
            "health_reasons": d.health_reasons,
            "formatted_text": text,
            "data_status": "ok",
            "latest_date": latest_date or _latest_hist_date(df),
            **hist_meta,
            **({"tickflow_limit_hint": hist_hints[0]} if hist_hints else {}),
        }
    except Exception as e:
        logger.exception("analyze_stock error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 3: 持仓（合并原 get_portfolio + diagnose_portfolio）
# ---------------------------------------------------------------------------


def portfolio(mode: str = "view", tool_context: ToolContext = None) -> dict:
    """查看或诊断用户当前持仓。

    Args:
        mode: "view" 仅返回持仓列表和可用资金；"diagnose" 对每只持仓做 Wyckoff 健康诊断

    Returns:
        持仓数据或诊断结果。
    """
    try:
        from integrations.supabase_portfolio import build_user_live_portfolio_id

        user_id = _get_user_id(tool_context)
        portfolio_id = build_user_live_portfolio_id(user_id)
        state = None

        from core.cache_whitelist import is_user_in_cache_whitelist

        use_remote = is_user_in_cache_whitelist(user_id) and _has_cloud(tool_context)

        if use_remote:
            from integrations.supabase_portfolio import load_portfolio_state

            _client = _get_user_client(tool_context)
            state = _with_auth_retry(tool_context, load_portfolio_state, portfolio_id, client=_client)
            if state:
                try:
                    from integrations.local_db import save_portfolio

                    save_portfolio(
                        portfolio_id,
                        float(state.get("free_cash", 0) or 0),
                        [
                            {
                                "code": p.get("code", ""),
                                "name": p.get("name", ""),
                                "shares": p.get("shares", 0),
                                "cost_price": p.get("cost", p.get("cost_price", 0)),
                                "buy_dt": p.get("buy_dt", ""),
                                "stop_loss": p.get("stop_loss"),
                            }
                            for p in state.get("positions", [])
                        ],
                    )
                except Exception:
                    logger.warning("failed to cache portfolio %s locally", portfolio_id, exc_info=True)

        if state is None:
            try:
                from integrations.local_db import load_portfolio

                state = load_portfolio(portfolio_id)
            except Exception:
                logger.warning("failed to load portfolio %s from local DB", portfolio_id, exc_info=True)

        if state is None:
            return {"message": "未找到持仓记录，可通过 update_portfolio 添加", "positions": [], "free_cash": 0}

        mode = (mode or "view").strip().lower()
        if mode not in ("view", "diagnose"):
            return {"error": f"mode 参数无效: '{mode}'，可选值: view, diagnose"}

        if mode == "view":
            positions = []
            for p in state.get("positions", []):
                positions.append(
                    {
                        "code": p.get("code", ""),
                        "name": p.get("name", ""),
                        "shares": p.get("shares", 0),
                        "cost_price": p.get("cost", p.get("cost_price", 0)),
                        "buy_dt": p.get("buy_dt", ""),
                    }
                )
            return {
                "portfolio_id": portfolio_id,
                "free_cash": state.get("free_cash", 0),
                "position_count": len(positions),
                "positions": positions,
            }

        # mode == "diagnose"
        _ensure_tushare_token(tool_context)
        from core.holding_diagnostic import diagnose_one_stock, format_diagnostic_text
        from integrations.stock_hist_repository import get_stock_hist

        if not state.get("positions"):
            return {
                "message": "持仓记录存在但无头寸",
                "portfolio_id": portfolio_id,
                "free_cash": state.get("free_cash", 0),
                "positions": [],
            }

        end_date = date.today()
        start_date = end_date - timedelta(days=500)
        results = []
        hist_tickflow_hints: list[str] = []
        successful_count = 0
        failed_count = 0
        for pos in state["positions"]:
            pos_code = pos.get("code", "") or pos.get("code", "")
            pos_name = pos.get("name", pos_code)
            pos_cost = float(pos.get("cost", pos.get("cost_price", 0)) or 0)
            try:
                df = get_stock_hist(pos_code, start_date, end_date, user_id=_get_user_id(tool_context))
                if df is None or df.empty:
                    failed_count += 1
                    results.append({"code": pos_code, "name": pos_name, "error": "无行情数据"})
                    continue
                hist_meta = _hist_metadata(df)
                latest_date = _latest_hist_date(df, "日期")
                for hint in _collect_tickflow_limit_hints_from_df(df):
                    if hint not in hist_tickflow_hints:
                        hist_tickflow_hints.append(hint)
                from core.stock_cache import _COL_MAP

                df = df.rename(columns=_COL_MAP)
                d = diagnose_one_stock(pos_code, pos_name, pos_cost, df)
                successful_count += 1
                results.append(
                    {
                        "code": d.code,
                        "name": d.name,
                        "health": d.health,
                        "pnl_pct": round(d.pnl_pct, 2),
                        "latest_close": d.latest_close,
                        "l2_channel": d.l2_channel,
                        "l4_triggers": d.l4_triggers,
                        "health_reasons": d.health_reasons,
                        "formatted_text": format_diagnostic_text(d),
                        "data_status": "ok",
                        "latest_date": latest_date or _latest_hist_date(df),
                        **hist_meta,
                    }
                )
            except Exception as e:
                failed_count += 1
                results.append({"code": pos_code, "name": pos_name, "error": str(e)})

        result = {
            "portfolio_id": portfolio_id,
            "free_cash": state.get("free_cash", 0),
            "position_count": len(state["positions"]),
            "successful_count": successful_count,
            "failed_count": failed_count,
            "diagnostics": results,
        }
        if hist_tickflow_hints:
            result["tickflow_limit_hint"] = hist_tickflow_hints[0]
        return result
    except Exception as e:
        logger.exception("portfolio error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 5: 大盘概览
# ---------------------------------------------------------------------------


def get_market_overview(tool_context: ToolContext) -> dict:
    """获取当前 A 股大盘环境概览。

    返回主要指数（上证、深证、创业板）的最新收盘数据和涨跌幅。

    Returns:
        大盘概览 dict，包含各指数的涨跌幅和近期走势。
    """
    try:
        errors: list[str] = []
        indices = {
            "000001.SH": "上证指数",
            "399001.SZ": "深证成指",
            "399006.SZ": "创业板指",
            "000016.SH": "上证50",
            "000905.SH": "中证500",
        }

        # 优先 tushare（有 token 时数据更稳定）
        try:
            _ensure_tushare_token(tool_context)
            from integrations.tushare_client import get_pro

            pro = get_pro()
            if pro is not None:
                end_date = date.today().strftime("%Y%m%d")
                start_date = (date.today() - timedelta(days=10)).strftime("%Y%m%d")
                result = {}
                for ts_code, name in indices.items():
                    try:
                        df = pro.index_daily(
                            ts_code=ts_code,
                            start_date=start_date,
                            end_date=end_date,
                        )
                        if df is not None and not df.empty:
                            df = df.sort_values("trade_date")
                            latest = df.iloc[-1]
                            result[name] = {
                                "ts_code": ts_code,
                                "trade_date": str(latest.get("trade_date", "")),
                                "close": round(float(latest.get("close", 0)), 2),
                                "pct_chg": round(float(latest.get("pct_chg", 0)), 2),
                                "vol": int(latest.get("vol", 0)),
                                "amount": round(float(latest.get("amount", 0)), 2),
                            }
                    except Exception as e:
                        result[name] = {"error": str(e)}
                if result:
                    return {"indices": result, "source": "tushare"}
            else:
                errors.append("tushare: token 未配置或 client 不可用")
        except Exception as e:
            errors.append(f"tushare: {e}")

        # 兜底 akshare（无需 token）
        try:
            import akshare as ak

            spot = ak.stock_zh_index_spot_em()
            if spot is None or spot.empty:
                errors.append("akshare: stock_zh_index_spot_em 返回空")
            else:
                # 兼容不同版本列名
                col_code = "代码" if "代码" in spot.columns else ("指数代码" if "指数代码" in spot.columns else "")
                col_name = "名称" if "名称" in spot.columns else ("指数名称" if "指数名称" in spot.columns else "")
                col_close = "最新价" if "最新价" in spot.columns else ("最新" if "最新" in spot.columns else "")
                col_pct = "涨跌幅" if "涨跌幅" in spot.columns else ("涨跌幅(%)" if "涨跌幅(%)" in spot.columns else "")
                col_vol = "成交量" if "成交量" in spot.columns else ""
                col_amount = "成交额" if "成交额" in spot.columns else ""
                if not col_code:
                    errors.append("akshare: 缺少指数代码列")
                else:
                    code_to_ts = {
                        "000001": "000001.SH",
                        "399001": "399001.SZ",
                        "399006": "399006.SZ",
                        "000016": "000016.SH",
                        "000905": "000905.SH",
                    }
                    target_codes = set(code_to_ts.keys())
                    today = date.today().strftime("%Y%m%d")
                    result = {}
                    for _, row in spot.iterrows():
                        code_raw = str(row.get(col_code, "") or "").strip()
                        code = "".join(ch for ch in code_raw if ch.isdigit())[-6:]
                        if code not in target_codes:
                            continue
                        name_cn = str(row.get(col_name, "") or "").strip() or indices[code_to_ts[code]]
                        try:
                            close_v = float(row.get(col_close, 0) or 0) if col_close else 0.0
                        except Exception:
                            close_v = 0.0
                        try:
                            pct_v = float(row.get(col_pct, 0) or 0) if col_pct else 0.0
                        except Exception:
                            pct_v = 0.0
                        try:
                            vol_v = int(float(row.get(col_vol, 0) or 0)) if col_vol else 0
                        except Exception:
                            vol_v = 0
                        try:
                            amount_v = round(float(row.get(col_amount, 0) or 0), 2) if col_amount else 0.0
                        except Exception:
                            amount_v = 0.0

                        result[name_cn] = {
                            "ts_code": code_to_ts[code],
                            "trade_date": today,
                            "close": round(close_v, 2),
                            "pct_chg": round(pct_v, 2),
                            "vol": vol_v,
                            "amount": amount_v,
                        }

                    if result:
                        return {"indices": result, "source": "akshare"}
                    errors.append("akshare: 目标指数未命中")
        except Exception as e:
            errors.append(f"akshare: {e}")

        return {
            "error": "无法获取大盘数据",
            "details": "; ".join(errors) if errors else "unknown",
        }
    except Exception as e:
        logger.exception("get_market_overview error")
        return {"error": str(e)}


def _resolve_market_history_index(index: str) -> tuple[str, str, str]:
    raw = str(index or "sse").strip()
    key = _MARKET_HISTORY_ALIASES.get(raw, _MARKET_HISTORY_ALIASES.get(raw.lower(), raw.lower()))
    if key in _MARKET_HISTORY_INDEXES:
        symbol, name = _MARKET_HISTORY_INDEXES[key]
        return key, symbol, name
    code = raw.upper()
    for item_key, (symbol, name) in _MARKET_HISTORY_INDEXES.items():
        if code in {symbol, symbol.split(".", 1)[0]}:
            return item_key, symbol, name
    symbol, name = _MARKET_HISTORY_INDEXES["sse"]
    return "sse", symbol, name


def _json_float(value: Any, digits: int = 2) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return round(out, digits)


def _prepare_market_history_frame(df: Any, days: int) -> Any:
    import pandas as pd

    out = df.copy()
    for col in ("open", "high", "low", "close", "volume", "amount", "prev_close"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    if "date" not in out.columns and "datetime" in out.columns:
        out["date"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%Y-%m-%d")
    if "pct_chg" not in out.columns:
        basis = out["prev_close"] if "prev_close" in out.columns else out["close"].shift(1)
        out["pct_chg"] = (out["close"] / basis - 1.0) * 100.0
    out["pct_chg"] = pd.to_numeric(out["pct_chg"], errors="coerce")
    cols = ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]
    for col in cols:
        if col not in out.columns:
            out[col] = None
    for col in ("open", "high", "low", "close", "volume", "amount", "pct_chg"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["date", "close"]).sort_values("date").tail(days)
    return out[cols].reset_index(drop=True)


def _fetch_market_history_frame(symbol: str, days: int, tool_context: ToolContext | None) -> tuple[Any, str, list[str]]:
    errors: list[str] = []
    api_key = _get_credential(tool_context, "tickflow_api_key", "TICKFLOW_API_KEY")
    if api_key:
        try:
            from integrations.tickflow_client import TickFlowClient

            client = TickFlowClient(api_key=api_key)
            return client.get_klines(symbol, period="1d", count=days, adjust="none"), "tickflow", errors
        except Exception as e:
            errors.append(f"tickflow: {e}")
    else:
        errors.append("tickflow: TICKFLOW_API_KEY 未配置")
    try:
        _ensure_tushare_token(tool_context)
        from integrations.data_source import fetch_index_hist

        end = date.today()
        start = end - timedelta(days=int(days * 2.4) + 30)
        return fetch_index_hist(symbol, start, end), "tushare/akshare", errors
    except Exception as e:
        errors.append(f"tushare/akshare: {e}")
    raise RuntimeError("; ".join(errors))


def _market_history_summary(df: Any) -> dict[str, Any]:
    close = df["close"]
    volume = df["volume"]
    latest = df.iloc[-1]
    roll_max = close.cummax()
    drawdown = ((close / roll_max) - 1.0) * 100.0
    tail20 = df.tail(min(len(df), 20))
    prior = df.iloc[:-20] if len(df) > 20 else df.iloc[:0]
    prior_volume = prior["volume"].mean() if len(prior) else None
    recent_volume = tail20["volume"].mean()
    period_return = (float(close.iloc[-1]) / float(close.iloc[0]) - 1.0) * 100.0
    recent_return = (float(tail20["close"].iloc[-1]) / float(tail20["close"].iloc[0]) - 1.0) * 100.0
    return {
        "latest_date": str(latest["date"]),
        "latest_close": _json_float(latest["close"]),
        "latest_pct_chg": _json_float(latest["pct_chg"]),
        "period_return_pct": _json_float(period_return),
        "recent_20d_return_pct": _json_float(recent_return),
        "latest_volume_ratio_20d": _json_float(
            float(latest["volume"]) / float(recent_volume) if recent_volume else None
        ),
        "recent_20d_volume_vs_prior": _json_float(float(recent_volume) / float(prior_volume) if prior_volume else None),
        "max_drawdown_pct": _json_float(drawdown.min()),
        "up_days": int((df["pct_chg"] > 0).sum()),
        "down_days": int((df["pct_chg"] < 0).sum()),
        "price_up_volume_up_days": int(((df["pct_chg"] > 0) & (volume > volume.shift(1))).sum()),
        "price_down_volume_up_days": int(((df["pct_chg"] < 0) & (volume > volume.shift(1))).sum()),
    }


def _market_history_rows(df: Any) -> list[dict[str, Any]]:
    rows = []
    for row in df.to_dict("records"):
        rows.append(
            {
                "date": str(row.get("date", "")),
                "open": _json_float(row.get("open")),
                "high": _json_float(row.get("high")),
                "low": _json_float(row.get("low")),
                "close": _json_float(row.get("close")),
                "pct_chg": _json_float(row.get("pct_chg")),
                "volume": _json_float(row.get("volume"), 0),
            }
        )
    return rows


def get_market_history(days: int = 100, index: str = "sse", tool_context: ToolContext = None) -> dict:
    """回看 A 股主要指数过去 N 个交易日的日线量价关系。

    Args:
        days: 回看交易日数量，默认 100，最大 320。
        index: 指数别名或代码，支持 sse/csi300/szse/chinext/sse50/csi500。

    Returns:
        历史大盘日线摘要、最近 N 个交易日量价切片和数据源。
    """
    try:
        requested_days = max(1, min(int(days or 100), 320))
        lookback = max(20, requested_days)
        key, symbol, name = _resolve_market_history_index(index)
        raw, source, errors = _fetch_market_history_frame(symbol, lookback, tool_context)
        df = _prepare_market_history_frame(raw, lookback).tail(requested_days).reset_index(drop=True)
        if df.empty:
            return {"error": f"{name} {symbol} 没有可用历史 K 线", "source": source}
        return {
            "ok": True,
            "index": {"key": key, "symbol": symbol, "name": name},
            "requested_days": requested_days,
            "returned_days": int(len(df)),
            "source": source,
            "fallback_errors": errors,
            "summary": _market_history_summary(df),
            "rows": _market_history_rows(df),
        }
    except Exception as e:
        logger.exception("get_market_history error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 6: Wyckoff 漏斗筛选
# ---------------------------------------------------------------------------

_VALID_BOARDS = {"all", "main", "chinext"}
_BOARD_ALIAS = {
    "gem": "chinext",
    "创业板": "chinext",
    "主板": "main",
    "全部": "all",
    "main_chinext": "all",
    "main-chinext": "all",
    "main+chinext": "all",
}


def screen_stocks(board: str = "all", tool_context: ToolContext = None) -> dict:
    """运行 Wyckoff 五层漏斗筛选，从全市场中筛选出具有结构性机会的股票。

    筛选过程包括：L1 基本面过滤、L2 通道分类、L3 板块轮动、L4 触发检测、L5 退出信号过滤。
    整个过程可能需要几分钟时间。

    Args:
        board: 股票池板块，可选 "all"（全部主板+创业板）、"main"（仅主板）、"chinext"（仅创业板）

    Returns:
        筛选结果 dict，包含各层统计和最终候选股票列表。
    """
    try:
        _ensure_tushare_token(tool_context)
        # 参数校验与别名映射
        board = str(board or "all").strip().lower()
        board = _BOARD_ALIAS.get(board, board)
        if board not in _VALID_BOARDS:
            return {"error": f"不支持的 board 值 '{board}'，可选: all / main / chinext"}

        # 保存并设置环境变量（调用后恢复）
        prev_mode = os.environ.get("FUNNEL_POOL_MODE")
        prev_board = os.environ.get("FUNNEL_POOL_BOARD")
        prev_exec = os.environ.get("FUNNEL_EXECUTOR_MODE")
        os.environ["FUNNEL_POOL_MODE"] = "board"
        os.environ["FUNNEL_POOL_BOARD"] = board
        # CLI 后台线程中 fork 子进程会触发 Python 3.13+ fds_to_keep 错误，强制用 thread
        os.environ["FUNNEL_EXECUTOR_MODE"] = "thread"

        from scripts.wyckoff_funnel import run as run_funnel

        try:
            ok, symbols, bench_ctx, details = run_funnel(
                "",
                notify=False,
                return_details=True,
            )
        finally:
            # 恢复环境变量，避免影响后续调用
            if prev_mode is None:
                os.environ.pop("FUNNEL_POOL_MODE", None)
            else:
                os.environ["FUNNEL_POOL_MODE"] = prev_mode
            if prev_board is None:
                os.environ.pop("FUNNEL_POOL_BOARD", None)
            else:
                os.environ["FUNNEL_POOL_BOARD"] = prev_board
            if prev_exec is None:
                os.environ.pop("FUNNEL_EXECUTOR_MODE", None)
            else:
                os.environ["FUNNEL_EXECUTOR_MODE"] = prev_exec

        metrics = details.get("metrics") or {}
        triggers = details.get("triggers") or {}
        name_map = details.get("name_map") or {}

        trigger_summary = {}
        for trigger_name, rows in triggers.items():
            trigger_summary[trigger_name] = [
                {
                    "code": str(code),
                    "name": str(name_map.get(str(code), code)),
                    "score": round(float(score), 2),
                }
                for code, score in rows
            ]

        return {
            "ok": bool(ok),
            "summary": {
                "total_scanned": int(metrics.get("total_symbols", 0)),
                "layer1_passed": int(metrics.get("layer1", 0)),
                "layer2_passed": int(metrics.get("layer2", 0)),
                "layer3_passed": int(metrics.get("layer3", 0)),
            },
            "trigger_groups": trigger_summary,
            "top_sectors": metrics.get("top_sectors", []),
            "symbols_for_report": symbols,
        }
    except Exception as e:
        logger.exception("screen_stocks error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 7: AI 研报生成
# ---------------------------------------------------------------------------


def generate_ai_report(stock_codes: list[str], tool_context: ToolContext) -> dict:
    """对指定股票列表生成威科夫三阵营 AI 深度研报。

    使用 LLM 对股票进行威科夫供需分析，将股票分为三个阵营：
    - 逻辑破产 (Invalidated)
    - 储备营地 (Building Cause)
    - 起跳板 (On the Springboard)

    需要配置大模型 API Key 才能使用。

    Args:
        stock_codes: 股票代码列表，如 ["000001", "600519", "300750"]，最多 10 只

    Returns:
        包含研报文本和起跳板代码的 dict。
    """
    try:
        _ensure_tushare_token(tool_context)
        if not stock_codes:
            return {"error": "请提供至少一个股票代码"}
        if len(stock_codes) > 10:
            stock_codes = stock_codes[:10]

        provider, api_key, model, base_url = _resolve_llm_config(tool_context)
        if not api_key:
            return {"error": "未配置 LLM API Key，无法生成 AI 研报。请通过 /model 或设置页面配置。"}

        # 构建 symbols_info 格式
        symbols_info = []
        for code in stock_codes:
            code = str(code).strip()
            name = _code_to_name(code)
            symbols_info.append({"code": code, "name": name, "tag": "chat_request"})

        from core.batch_report import run_step3

        ok, reason, report_text = run_step3(
            symbols_info,
            webhook_url="",
            api_key=api_key,
            model=model,
            benchmark_context=None,
            notify=False,
            provider=provider,
            llm_base_url=base_url,
        )

        return {
            "ok": bool(ok),
            "reason": str(reason or ""),
            "report_text": str(report_text or ""),
            "model": model,
            "stock_count": len(symbols_info),
        }
    except Exception as e:
        logger.exception("generate_ai_report error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 8: 持仓策略决策
# ---------------------------------------------------------------------------


def generate_strategy_decision(tool_context: ToolContext) -> dict:
    """生成持仓去留决策和新标的买入策略（需要先运行筛选和研报）。

    使用威科夫方法论，综合审视当前持仓和外部候选，给出：
    - 现有持仓的去留决策（EXIT/TRIM/HOLD）
    - 外部候选的买入建议（PROBE/ATTACK）

    需要配置大模型 API Key 和持仓数据。

    Returns:
        策略决策结果 dict。
    """
    try:
        _ensure_tushare_token(tool_context)

        provider, api_key, model, base_url = _resolve_llm_config(tool_context)
        if not api_key:
            return {"error": "未配置 LLM API Key，无法生成策略决策。请通过 /model 或设置页面配置。"}

        user_id = _get_user_id(tool_context)
        from integrations.supabase_portfolio import build_user_live_portfolio_id

        portfolio_id = build_user_live_portfolio_id(user_id)

        # 先运行筛选获取候选
        screen_result = screen_stocks(board="all")
        if screen_result.get("error"):
            return {"error": f"筛选失败: {screen_result['error']}"}

        symbols_info = screen_result.get("symbols_for_report", [])

        # 生成研报
        report_text = ""
        if symbols_info:
            from core.batch_report import run_step3

            ok, reason, report_text = run_step3(
                symbols_info,
                webhook_url="",
                api_key=api_key,
                model=model,
                benchmark_context=None,
                notify=False,
                provider=provider,
                llm_base_url=base_url,
            )

        # 生成策略决策（需要 Telegram 配置来发送，但在聊天模式下直接返回结果）
        from core.strategy import run_step4

        tg_bot_token = os.getenv("TG_BOT_TOKEN", "")
        tg_chat_id = os.getenv("TG_CHAT_ID", "")

        if not tg_bot_token or not tg_chat_id:
            return {
                "message": "策略分析完成，但未配置 Telegram 无法发送通知。以下是筛选和研报结果。",
                "screen_summary": screen_result.get("summary", {}),
                "report_preview": (report_text[:2000] + "...") if len(report_text) > 2000 else report_text,
            }

        ok, reason = run_step4(
            external_report=report_text,
            benchmark_context=None,
            api_key=api_key,
            model=model,
            portfolio_id=portfolio_id,
            tg_bot_token=tg_bot_token,
            tg_chat_id=tg_chat_id,
        )

        return {
            "ok": bool(ok),
            "reason": str(reason or ""),
            "screen_summary": screen_result.get("summary", {}),
        }
    except Exception as e:
        logger.exception("generate_strategy_decision error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 5: 历史记录查询（合并原 recommendation / signal / tail_buy）
# ---------------------------------------------------------------------------


def query_history(source: str, status: str = "all", run_date: str = "", decision: str = "", limit: int = 20) -> dict:
    """查询历史记录：形态复盘、信号确认池或尾盘买入记录。

    Args:
        source: "recommendation" 查形态复盘；"signal" 查信号确认池；"tail_buy" 查尾盘买入记录
        status: 仅 signal 源使用，"all"/"pending"/"confirmed"/"expired"
        run_date: 仅 tail_buy 源使用，按日期过滤（YYYY-MM-DD）
        decision: 仅 tail_buy 源使用，按决策过滤（BUY/WATCH 等）
        limit: 返回记录数上限，默认 20

    Returns:
        对应来源的历史记录列表。
    """
    source = (source or "").strip().lower()
    if source == "recommendation":
        return _query_recommendation(limit)
    elif source == "signal":
        return _query_signal(status, limit)
    elif source == "tail_buy":
        return _query_tail_buy(run_date, decision, limit)
    else:
        return {"error": f"不支持的 source：{source}，请用 'recommendation'、'signal' 或 'tail_buy'"}


def _query_recommendation(limit: int) -> dict:
    try:
        limit = min(max(limit, 1), 50)
        records = []
        try:
            from integrations.local_db import load_recommendations

            records = load_recommendations(limit=limit)
        except Exception:
            logger.warning("failed to load recommendations from local DB", exc_info=True)
        if not records:
            from integrations.supabase_recommendation import load_recommendation_tracking

            records = load_recommendation_tracking(limit=limit)
            if records:
                try:
                    from integrations.local_db import save_recommendations

                    save_recommendations(records)
                except Exception:
                    logger.warning("failed to cache recommendations locally", exc_info=True)
        if not records:
            return {"message": "暂无复盘记录", "records": []}
        simplified = [
            {
                "code": str(r.get("code", "")),
                "name": str(r.get("name", "")),
                "recommend_date": str(r.get("recommend_date", "")),
                "recommend_price": r.get("recommend_price"),
                "current_price": r.get("current_price"),
                "pnl_pct": r.get("pnl_pct"),
                "max_pnl_pct": r.get("max_pnl_pct"),
                "camp": str(r.get("camp", "")),
                "status": str(r.get("status", "")),
            }
            for r in records
        ]
        return {"total": len(simplified), "records": simplified}
    except Exception as e:
        logger.exception("query_history(recommendation) error")
        return {"error": str(e)}


def _query_signal(status: str, limit: int) -> dict:
    try:
        limit = min(max(limit, 1), 100)
        rows: list[dict] = []
        try:
            from integrations.local_db import load_signals

            st = status if status in ("pending", "confirmed", "expired") else None
            rows = load_signals(status=st, limit=limit)
        except Exception:
            logger.warning("failed to load signals from local DB", exc_info=True)
        if not rows:
            from core.constants import TABLE_SIGNAL_PENDING
            from integrations.supabase_base import create_admin_client, is_admin_configured

            if not is_admin_configured():
                return {"error": "本地无缓存且 Supabase 未配置"}
            client = create_admin_client()
            query = client.table(TABLE_SIGNAL_PENDING).select("*")
            if status in ("pending", "confirmed", "expired"):
                query = query.eq("status", status)
            rows = query.order("updated_at", desc=True).limit(limit).execute().data or []
            if rows:
                try:
                    from integrations.local_db import save_signals

                    save_signals(rows)
                except Exception:
                    logger.warning("failed to cache signals locally", exc_info=True)
        if not rows:
            status_label = {"pending": "待确认", "confirmed": "已确认", "expired": "已过期"}.get(status, "")
            return {"message": f"暂无{status_label}信号记录", "records": []}
        records = [
            {
                "code": f"{int(r.get('code', 0)):06d}",
                "name": str(r.get("name", "")),
                "signal_type": str(r.get("signal_type", "")),
                "signal_date": str(r.get("signal_date", "")),
                "status": str(r.get("status", "")),
                "days_elapsed": r.get("days_elapsed", 0),
                "ttl_days": r.get("ttl_days", 3),
                "signal_score": r.get("signal_score", 0),
                "snap_close": r.get("snap_close"),
                "confirm_date": str(r.get("confirm_date", "") or ""),
                "expire_date": str(r.get("expire_date", "") or ""),
                "confirm_reason": str(r.get("confirm_reason", "") or ""),
                "regime": str(r.get("regime", "") or ""),
                "industry": str(r.get("industry", "") or ""),
            }
            for r in rows
        ]
        status_counts: dict[str, int] = {}
        for rec in records:
            s = rec["status"]
            status_counts[s] = status_counts.get(s, 0) + 1
        return {"total": len(records), "status_counts": status_counts, "records": records}
    except Exception as e:
        logger.exception("query_history(signal) error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Helper — 缓存 user client，避免重复消费 refresh_token
# ---------------------------------------------------------------------------

_user_client_cache: dict[str, Any] = {}  # user_id → Client


def _get_user_client(tool_context: ToolContext | None):
    """获取或复用 user client。token 变化时重建；auth 失败自动重登。"""
    if tool_context is None:
        return None
    at = tool_context.state.get("access_token") or ""
    if not at:
        return None
    user_id = _get_user_id(tool_context)
    cache_key = f"{user_id}:{at[:16]}"
    cached = _user_client_cache.get(cache_key)
    if cached is not None:
        return cached
    rt = tool_context.state.get("refresh_token") or ""
    from integrations.supabase_base import create_user_client, get_session_tokens

    try:
        client = create_user_client(at, rt)
        new_at, new_rt = get_session_tokens(client)
    except Exception as e:
        if _is_auth_error(e):
            client, new_at, new_rt = _relogin_and_create_client(tool_context)
            if client is None:
                return None
        else:
            raise
    if new_at:
        tool_context.state["access_token"] = new_at
    if new_rt:
        tool_context.state["refresh_token"] = new_rt
    final_key = f"{user_id}:{(new_at or at)[:16]}"
    _user_client_cache[final_key] = client
    return client


def _relogin_and_create_client(tool_context: ToolContext | None):
    """用 wyckoff.json 中的凭证重新登录，返回 (client, access_token, refresh_token)。"""
    from cli.auth import _auto_relogin

    data = _auto_relogin()
    if not data:
        return None, "", ""
    tool_context.state["access_token"] = data["access_token"]
    tool_context.state["refresh_token"] = data["refresh_token"]
    from integrations.supabase_base import create_user_client, get_session_tokens

    client = create_user_client(data["access_token"], data["refresh_token"])
    new_at, new_rt = get_session_tokens(client)
    return client, new_at or data["access_token"], new_rt or data["refresh_token"]


_AUTH_ERR_KEYWORDS = ("invalid", "expired", "revoked", "refresh", "jwt", "token")


def _is_auth_error(e: Exception) -> bool:
    err = str(e).lower()
    return any(k in err for k in _AUTH_ERR_KEYWORDS)


def _with_auth_retry(tool_context: ToolContext | None, fn, *args, **kwargs):
    """执行 fn，遇到 auth 错误时自动重登并重试一次。"""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        if not _is_auth_error(e) or tool_context is None:
            raise
    _user_client_cache.clear()
    client, new_at, new_rt = _relogin_and_create_client(tool_context)
    if client is None:
        return None
    if "client" in kwargs:
        kwargs["client"] = client
    elif args:
        args_list = list(args)
        for i, a in enumerate(args_list):
            if hasattr(a, "auth") and hasattr(a, "postgrest"):
                args_list[i] = client
                break
        args = tuple(args_list)
    return fn(*args, **kwargs)


def _to_ts_code(code: str) -> str:
    """6 位代码 → tushare ts_code（如 000001 → 000001.SZ）。"""
    code = str(code).strip()
    if "." in code:
        return code
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    return f"{code}.SZ"


# ---------------------------------------------------------------------------
# Tool 6: 持仓管理（合并原 update_portfolio + delete_tracking_records）
# ---------------------------------------------------------------------------


def update_portfolio(
    action: str,
    code: str = "",
    name: str = "",
    shares: int = 0,
    cost_price: float = 0,
    buy_dt: str = "",
    free_cash: float = 0,
    table: str = "",
    codes: list[str] = None,
    tool_context: ToolContext = None,
) -> dict:
    """管理用户持仓或删除追踪记录。

    Args:
        action: 操作类型，"add"（新增/加仓）、"update"（修改）、"remove"（删除）、"set_cash"（设置可用资金）、"delete_records"（删除推荐/信号记录）
        code: 6 位股票代码（add/update/remove 时必填）
        name: 股票名称（可选）
        shares: 持仓股数
        cost_price: 成本价
        buy_dt: 买入日期（YYYYMMDD 格式）
        free_cash: 可用资金（set_cash 时使用）
        table: 仅 delete_records 使用，'recommendation' 或 'signal'
        codes: 仅 delete_records 使用，要删除的股票代码列表

    Returns:
        操作结果。
    """
    try:
        action = action.strip().lower()

        if action == "delete_records":
            if not codes:
                return {"error": "请指定要删除的股票代码 codes"}
            codes = [str(c).strip() for c in codes if str(c).strip()]
            if table == "recommendation":
                from integrations.local_db import delete_recommendations

                n = delete_recommendations(codes)
                return {"deleted": n, "table": "recommendation_tracking", "codes": codes}
            elif table == "signal":
                from integrations.local_db import delete_signals

                n = delete_signals(codes)
                return {"deleted": n, "table": "signal_pending", "codes": codes}
            else:
                return {"error": f"不支持的表：{table}，请用 'recommendation' 或 'signal'"}

        from integrations.supabase_portfolio import build_user_live_portfolio_id

        user_id = _get_user_id(tool_context)
        portfolio_id = build_user_live_portfolio_id(user_id)
        cloud = _has_cloud(tool_context)
        msg = ""

        if action in ("add", "update"):
            if not code:
                return {"error": "add/update 操作需要提供股票代码 code"}
            code = code.strip()
            real_name = _code_to_name(code)
            if real_name and name and real_name != name:
                return {"error": f"代码 {code} 对应的股票是「{real_name}」，而非「{name}」，请确认代码或名称是否正确"}
            if real_name and not name:
                name = real_name
            if not real_name and not name:
                return {"error": f"代码 {code} 在股票列表中未找到，请确认代码是否正确"}
            if cloud:
                from integrations.supabase_portfolio import upsert_position

                client = _get_user_client(tool_context)
                ok, msg = _with_auth_retry(
                    tool_context,
                    upsert_position,
                    portfolio_id,
                    {
                        "code": code,
                        "name": name,
                        "shares": shares,
                        "cost_price": cost_price,
                        "buy_dt": buy_dt,
                    },
                    client=client,
                )
                if not ok:
                    return {"error": msg}
            from integrations.local_db import upsert_local_position

            upsert_local_position(portfolio_id, code, name, shares, cost_price, buy_dt)
            msg = msg or f"{code} 已更新"

        elif action == "remove":
            if not code:
                return {"error": "remove 操作需要提供股票代码 code"}
            code = code.strip()
            if cloud:
                from integrations.supabase_portfolio import delete_position

                client = _get_user_client(tool_context)
                ok, msg = _with_auth_retry(tool_context, delete_position, portfolio_id, code, client=client)
                if not ok:
                    return {"error": msg}
            from integrations.local_db import delete_local_position

            delete_local_position(portfolio_id, code)
            msg = msg or f"{code} 已删除"

        elif action == "set_cash":
            if cloud:
                from integrations.supabase_portfolio import update_free_cash

                client = _get_user_client(tool_context)
                ok, msg = _with_auth_retry(tool_context, update_free_cash, portfolio_id, free_cash, client=client)
                if not ok:
                    return {"error": msg}
            from integrations.local_db import update_local_free_cash

            update_local_free_cash(portfolio_id, free_cash)
            msg = msg or f"可用资金已更新为 {free_cash:,.2f}"

        else:
            return {"error": f"未知操作: {action}，支持 add/update/remove/set_cash/delete_records"}

        # Supabase write-through: 读回最新状态同步到本地
        if cloud:
            try:
                from integrations.supabase_portfolio import load_portfolio_state

                client = _get_user_client(tool_context)
                state = _with_auth_retry(tool_context, load_portfolio_state, portfolio_id, client=client)
                if state:
                    from integrations.local_db import save_portfolio

                    save_portfolio(
                        portfolio_id,
                        float(state.get("free_cash", 0) or 0),
                        [
                            {
                                "code": p.get("code", ""),
                                "name": p.get("name", ""),
                                "shares": p.get("shares", 0),
                                "cost_price": p.get("cost", p.get("cost_price", 0)),
                                "buy_dt": p.get("buy_dt", ""),
                                "stop_loss": p.get("stop_loss"),
                            }
                            for p in state.get("positions", [])
                        ],
                    )
            except Exception:
                logger.warning("failed to cache portfolio %s locally after update", portfolio_id, exc_info=True)

        # 读本地最新状态返回
        from integrations.local_db import load_portfolio

        state = load_portfolio(portfolio_id)
        if not state:
            return {"success": True, "message": msg, "positions": []}

        summary = []
        for p in state.get("positions", []):
            summary.append(f"{p['code']} {p.get('name', '')} {p.get('shares', 0)}股 成本{p.get('cost_price', 0)}")
        result = {
            "success": True,
            "message": msg,
            "free_cash": state.get("free_cash", 0),
            "position_count": len(state.get("positions", [])),
            "positions_summary": summary,
        }
        if not cloud:
            result["storage"] = "local"
        return result
    except Exception as e:
        logger.exception("update_portfolio error")
        return {"error": str(e)}


def _query_tail_buy(run_date: str, decision: str, limit: int) -> dict:
    try:
        limit = min(max(int(limit), 1), 200)
        from integrations.local_db import load_tail_buy_history

        records = load_tail_buy_history(
            run_date=str(run_date or "").strip(),
            decision=str(decision or "").strip(),
            limit=limit,
        )
        if not records:
            from integrations.supabase_tail_buy import load_tail_buy_from_supabase

            sb_rows = load_tail_buy_from_supabase(limit=limit)
            if sb_rows:
                from integrations.local_db import save_tail_buy_results

                save_tail_buy_results(
                    [
                        {
                            "code": str(r.get("code", "")),
                            "name": r.get("name", ""),
                            "run_date": str(r.get("run_date", "")),
                            "signal_date": r.get("signal_date", ""),
                            "signal_type": r.get("signal_type", ""),
                            "status": "",
                            "final_decision": r.get("final_decision", "BUY"),
                            "rule_score": float(r.get("rule_score", 0)),
                            "priority_score": float(r.get("priority_score", 0)),
                            "rule_reasons": r.get("rule_reasons", ""),
                            "llm_decision": r.get("llm_decision", ""),
                            "llm_reason": r.get("llm_reason", ""),
                        }
                        for r in sb_rows
                    ]
                )
                records = load_tail_buy_history(
                    run_date=str(run_date or "").strip(),
                    decision=str(decision or "").strip(),
                    limit=limit,
                )
        if not records:
            return {"message": "暂无尾盘策略记录", "records": []}
        simplified = [
            {
                "code": str(r.get("code", "")),
                "name": str(r.get("name", "")),
                "run_date": str(r.get("run_date", "")),
                "signal_type": str(r.get("signal_type", "")),
                "final_decision": str(r.get("final_decision", "")),
                "rule_score": r.get("rule_score", 0),
                "priority_score": r.get("priority_score", 0),
                "llm_decision": str(r.get("llm_decision", "")),
                "llm_reason": str(r.get("llm_reason", "")),
            }
            for r in records
        ]
        return {"total": len(simplified), "records": simplified}
    except Exception as e:
        logger.exception("query_history(tail_buy) error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 15: 回测
# ---------------------------------------------------------------------------


def run_backtest(
    start: str = "",
    end: str = "",
    hold_days: int = 10,
    top_n: int = 3,
    board: str = "main_chinext",
    stop_loss_pct: float = -7.0,
    take_profit_pct: float = 18.0,
    tool_context: ToolContext = None,
) -> dict:
    """回测威科夫五层漏斗策略的历史表现。耗时较长（3-10分钟），会在后台执行。

    基于历史数据模拟漏斗筛选 + 信号触发 → 买入 → 止盈止损退出的完整流程，
    输出胜率、Sharpe 比率、最大回撤等核心指标。

    Args:
        start: 开始日期（YYYY-MM-DD），默认 6 个月前
        end: 结束日期（YYYY-MM-DD），默认昨天
        hold_days: 最大持仓天数（5/10/15/30），默认 10
        top_n: 每日最大候选数（0=不限），默认 3
        board: 股票池 'main_chinext'/'main'/'chinext'/'all'
        stop_loss_pct: 止损百分比（负数），默认 -7.0
        take_profit_pct: 止盈百分比，默认 18.0

    Returns:
        回测结果摘要：胜率、Sharpe、最大回撤、交易笔数等。
    """
    try:
        from datetime import date, timedelta

        from core.backtester import run_backtest as _run_backtest

        _ensure_tushare_token(tool_context)

        start_dt = date.fromisoformat(str(start).strip()[:10]) if start else date.today() - timedelta(days=180)
        end_dt = date.fromisoformat(str(end).strip()[:10]) if end else date.today() - timedelta(days=1)

        hold_days = max(1, min(int(hold_days), 60))
        top_n = max(0, min(int(top_n), 20))
        stop_loss_pct = min(0.0, float(stop_loss_pct))
        take_profit_pct = max(0.0, float(take_profit_pct))

        _trades_df, summary = _run_backtest(
            start_dt=start_dt,
            end_dt=end_dt,
            hold_days=hold_days,
            top_n=top_n,
            board=str(board or "main_chinext").strip(),
            sample_size=0,
            trading_days=320,
            max_workers=8,
            exit_mode="sltp",
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
        )

        return {
            "period": f"{start_dt} ~ {end_dt}",
            "hold_days": hold_days,
            "top_n": top_n,
            "board": board,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "trades": summary.get("trades", 0),
            "win_rate_pct": summary.get("win_rate_pct"),
            "avg_ret_pct": summary.get("avg_ret_pct"),
            "median_ret_pct": summary.get("median_ret_pct"),
            "sharpe_ratio": summary.get("sharpe_ratio"),
            "max_drawdown_pct": summary.get("max_drawdown_pct"),
            "portfolio_total_ret_pct": summary.get("portfolio_total_ret_pct"),
            "portfolio_ann_ret_pct": summary.get("portfolio_ann_ret_pct"),
            "max_consecutive_losses": summary.get("max_consecutive_losses"),
        }
    except Exception as e:
        logger.exception("run_backtest error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 15-18: Agent 标准工具（仅 CLI，Web 端不暴露）
# ---------------------------------------------------------------------------


def exec_command(command: str, timeout: int = 30, tool_context: ToolContext = None) -> dict:
    """在用户本地执行单条低风险命令并返回输出。

    Args:
        command: 要执行的命令；禁止 shell 控制符、管道、重定向和高风险命令
        timeout: 超时秒数，默认 30

    Returns:
        包含 stdout, stderr, returncode 的 dict。
    """
    import subprocess

    args = _validate_agent_command(command)
    if isinstance(args, dict):
        return args

    timeout = max(1, min(int(timeout), 120))
    try:
        r = subprocess.run(
            args,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=os.path.expanduser("~"),
        )
        stdout = _redact_sensitive_text(r.stdout)
        stderr = _redact_sensitive_text(r.stderr)
        return {
            "stdout": stdout[:8000] + ("...(截断)" if len(stdout) > 8000 else ""),
            "stderr": stderr[:2000] + ("...(截断)" if len(stderr) > 2000 else ""),
            "returncode": r.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"error": f"命令超时（{timeout}s）", "returncode": -1}
    except Exception as e:
        return {"error": str(e)}


def read_file(path: str, encoding: str = "utf-8", tool_context: ToolContext = None) -> dict:
    """读取用户本地文件内容。支持 txt/csv/json/xlsx 等格式，CSV 自动解析为表格预览。

    Args:
        path: 文件绝对路径或 ~ 开头的路径
        encoding: 文件编码，默认 utf-8

    Returns:
        包含 path, size, content 的 dict。CSV 返回 markdown 表格预览。
    """
    p = _validate_agent_path(path, for_write=False)
    if isinstance(p, dict):
        return p
    if not p.exists():
        return {"error": f"文件不存在: {p}"}
    if not p.is_file():
        return {"error": f"不是文件: {p}"}
    size = p.stat().st_size
    if size > _MAX_AGENT_FILE_BYTES:
        return {"error": f"文件过大 ({size / 1024 / 1024:.1f}MB)，上限 50MB"}

    suffix = p.suffix.lower()
    try:
        if suffix == ".csv" or suffix in (".xls", ".xlsx"):
            import pandas as pd

            df = pd.read_csv(p, encoding=encoding, nrows=50) if suffix == ".csv" else pd.read_excel(p, nrows=50)
            preview = _redact_sensitive_columns(df).to_markdown(index=False)
            return {"path": str(p), "size": size, "rows_total": "≤50(预览)", "content": _redact_sensitive_text(preview)}
        elif suffix == ".json":
            import json as _json

            text = p.read_text(encoding=encoding)[:10000]
            try:
                content = _json.dumps(_json.loads(text), ensure_ascii=False, indent=2)[:10000]
            except _json.JSONDecodeError:
                content = text
            return {"path": str(p), "size": size, "content": _redact_sensitive_text(content)}
        else:
            text = p.read_text(encoding=encoding)
            content = _redact_sensitive_text(text)
            return {
                "path": str(p),
                "size": size,
                "content": content[:10000] + ("...(截断)" if len(content) > 10000 else ""),
            }
    except Exception as e:
        return {"error": f"读取失败: {e}"}


def write_file(path: str, content: str, encoding: str = "utf-8", tool_context: ToolContext = None) -> dict:
    """将内容写入用户本地文件。自动创建父目录。

    Args:
        path: 文件路径
        content: 要写入的内容
        encoding: 文件编码，默认 utf-8

    Returns:
        包含 path, size 的 dict。
    """
    p = _validate_agent_path(path, for_write=True)
    if isinstance(p, dict):
        return p
    try:
        content_bytes = str(content).encode(encoding)
    except LookupError:
        return {"error": f"写入失败: 不支持的编码 {encoding}"}
    if len(content_bytes) > _MAX_AGENT_TEXT_WRITE_BYTES:
        return _security_error("写入内容过大，上限 2MB")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding=encoding)
        return {"path": str(p), "size": p.stat().st_size}
    except Exception as e:
        return {"error": f"写入失败: {e}"}


def web_fetch(url: str, tool_context: ToolContext = None) -> dict:
    """抓取指定 URL 的网页内容并返回纯文本。

    Args:
        url: 要抓取的网页 URL

    Returns:
        包含 url, status, content 的 dict。
    """
    import requests

    safe_url = _validate_public_http_url(url)
    if isinstance(safe_url, dict):
        return safe_url

    try:
        resp = requests.get(
            safe_url,
            timeout=(3, 15),
            headers={"User-Agent": "Wyckoff-Agent/1.0"},
            stream=True,
        )
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "").lower()
        if ctype and not any(ctype.startswith(prefix) for prefix in _SAFE_WEB_CONTENT_TYPES):
            return _security_error(f"拒绝抓取非文本内容: {ctype}")

        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_AGENT_WEB_BYTES:
                return _security_error("网页响应过大，上限 1MB")
        body = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")

        if "json" in ctype:
            text = body[:8000]
        elif "html" in ctype:
            text = re.sub(r"<script[^>]*>.*?</script>", "", body, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()[:8000]
        else:
            text = body[:8000]
        return {"url": safe_url, "status": resp.status_code, "content": _redact_sensitive_text(text)}
    except Exception as e:
        return {"error": f"抓取失败: {e}"}


# ---------------------------------------------------------------------------
# 工具列表导出（Web/Streamlit 端，不含 exec/read/write/web_fetch）
# ---------------------------------------------------------------------------

WYCKOFF_TOOLS = [
    search_stock_by_name,
    analyze_stock,
    portfolio,
    get_market_overview,
    get_market_history,
    screen_stocks,
    generate_ai_report,
    generate_strategy_decision,
    query_history,
    update_portfolio,
    run_backtest,
]
