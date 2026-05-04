"""
读盘室 Agent 端到端提示词效果测试。

验证用户消息 → Agent 是否调了正确工具 → 回复是否符合威科夫人设。
工具层 mock 掉（不发真实网络请求），但 LLM 用真实模型。

默认使用 LongCat（OpenAI 兼容），也支持 Gemini。

用法:
    # 使用 LongCat（默认，读 .env 中的 LONGCAT_* 配置）
    .venv/bin/python -m pytest tests/test_chat_agent_e2e.py -v -s

    # 使用 Gemini
    GEMINI_API_KEY=xxx .venv/bin/python -m pytest tests/test_chat_agent_e2e.py -v -s

    # 直接运行（更详细输出）
    .venv/bin/python tests/test_chat_agent_e2e.py
"""

from __future__ import annotations

import os
import sys

import pytest

# ── 加载 .env & 确定使用的 LLM ──
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# 优先 LongCat，其次 Gemini
_longcat_key = os.getenv("LONGCAT_API_KEY", "")
_longcat_model = os.getenv("LONGCAT_MODEL", "LongCat-Flash-Thinking-2601")
_longcat_base_url = os.getenv("LONGCAT_BASE_URL", "https://api.longcat.chat/openai")
_gemini_key = os.getenv("GEMINI_API_KEY", "")

if _longcat_key:
    _provider = "openai"
    _api_key = _longcat_key
    _model = _longcat_model
    _base_url = _longcat_base_url
elif _gemini_key:
    _provider = "gemini"
    _api_key = _gemini_key
    _model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    _base_url = ""
else:
    _provider = ""
    _api_key = ""
    _model = ""
    _base_url = ""

skip_no_key = pytest.mark.skipif(
    not _api_key,
    reason="Neither LONGCAT_API_KEY nor GEMINI_API_KEY set",
)


# ── Mock 数据 ──
MOCK_DIAGNOSE_RESULT = {
    "code": "000001",
    "name": "平安银行",
    "health": "HEALTHY",
    "pnl_pct": 0.0,
    "latest_close": 12.50,
    "ma_pattern": "多头排列",
    "l2_channel": "上升通道",
    "track": "markup",
    "accum_stage": "Phase_D",
    "l4_triggers": ["SOS（量价点火）"],
    "exit_signal": "",
    "stop_loss_status": "未触发",
    "vol_ratio_20_60": 1.35,
    "range_60d_pct": 15.2,
    "ret_10d_pct": 3.5,
    "ret_20d_pct": 8.1,
    "from_year_high_pct": -5.2,
    "from_year_low_pct": 42.3,
    "health_reasons": ["均线多头排列", "SOS信号触发", "量能配合"],
    "formatted_text": "平安银行(000001): 健康 | 多头排列 | Phase_D | SOS触发",
}

MOCK_SEARCH_RESULT = [
    {"code": "300750", "ts_code": "300750.SZ", "name": "宁德时代", "industry": "电池", "area": "福建"},
]

MOCK_MARKET_OVERVIEW = {
    "indices": {
        "上证指数": {"ts_code": "000001.SH", "close": 3350.0, "pct_chg": 0.85},
        "深证成指": {"ts_code": "399001.SZ", "close": 10850.0, "pct_chg": 1.12},
        "创业板指": {"ts_code": "399006.SZ", "close": 2180.0, "pct_chg": 1.45},
    },
    "source": "mock",
}

MOCK_PRICE_RESULT = {
    "code": "600519",
    "days": 5,
    "latest_close": 1688.0,
    "latest_date": "2026-04-11",
    "data": [
        {
            "date": "2026-04-07",
            "open": 1670.0,
            "high": 1685.0,
            "low": 1665.0,
            "close": 1680.0,
            "volume": 25000,
            "pct_chg": 0.5,
        },
        {
            "date": "2026-04-08",
            "open": 1680.0,
            "high": 1690.0,
            "low": 1675.0,
            "close": 1685.0,
            "volume": 28000,
            "pct_chg": 0.3,
        },
        {
            "date": "2026-04-09",
            "open": 1685.0,
            "high": 1695.0,
            "low": 1680.0,
            "close": 1690.0,
            "volume": 30000,
            "pct_chg": 0.3,
        },
        {
            "date": "2026-04-10",
            "open": 1690.0,
            "high": 1700.0,
            "low": 1682.0,
            "close": 1695.0,
            "volume": 32000,
            "pct_chg": 0.3,
        },
        {
            "date": "2026-04-11",
            "open": 1695.0,
            "high": 1698.0,
            "low": 1680.0,
            "close": 1688.0,
            "volume": 27000,
            "pct_chg": -0.4,
        },
    ],
}


def _make_mock_tool(name: str, return_value):
    """创建一个带正确签名的 mock 函数，供 ADK FunctionTool 解析。

    关键：复制原函数的 __doc__、__annotations__、__module__ 和 __globals__，
    这样 typing.get_type_hints() 能正确解析 ToolContext 等前向引用。
    """

    import agents.chat_tools as ct

    original = getattr(ct, name, None)

    # 通过 exec 在正确的 globals 环境中动态创建函数，让 get_type_hints 能找到 ToolContext
    func_globals = dict(ct.__dict__)  # 包含 ToolContext 的 import
    func_globals["_return_value"] = return_value

    # 构建一个与原函数签名一致的 wrapper
    if original:
        import inspect

        sig = inspect.signature(original)
        params = []
        for pname, param in sig.parameters.items():
            if pname == "tool_context":
                continue  # ADK 会自动注入，mock 中去掉避免用户需要传
            if param.default is inspect.Parameter.empty:
                params.append(pname)
            else:
                params.append(f"{pname}={param.default!r}")
        param_str = ", ".join(params)

        code = f"def {name}({param_str}):\n"
        code += f'    """{original.__doc__}"""\n' if original.__doc__ else ""
        code += "    return _return_value\n"

        exec(code, func_globals)
        mock_fn = func_globals[name]
    else:

        def mock_fn(**kwargs):
            return return_value

        mock_fn.__name__ = name
        mock_fn.__qualname__ = name

    return mock_fn


class AgentTestHarness:
    """
    测试专用 harness，用真实 LLM + mock 工具运行 Agent。
    收集 tool_call 事件和最终回复。
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash",
        provider: str = "gemini",
        base_url: str = "",
    ):
        from agents.session_manager import ChatSessionManager
        from agents.wyckoff_chat_agent import create_agent

        self.provider = provider
        self.model = model
        self.agent = create_agent(
            model=model,
            api_key=api_key,
            provider=provider,
            base_url=base_url,
        )

        # 替换工具为 mock 版本
        self._install_mock_tools()

        self.mgr = ChatSessionManager(
            user_id="test_user",
            agent=self.agent,
            api_key=api_key,
        )

    def _install_mock_tools(self):
        """用 mock 工具替换 agent 的真实工具。"""
        from google.adk.tools import FunctionTool

        mock_map = {
            "search_stock_by_name": MOCK_SEARCH_RESULT,
            "analyze_stock": MOCK_DIAGNOSE_RESULT,
            "portfolio": {"message": "当前没有持仓数据", "positions": []},
            "get_market_overview": MOCK_MARKET_OVERVIEW,
            "screen_stocks": {
                "ok": True,
                "summary": {"total_scanned": 4500, "layer1_passed": 800, "layer2_passed": 200, "layer3_passed": 50},
                "trigger_groups": {},
                "symbols_for_report": [],
            },
            "generate_ai_report": {"ok": True, "report_text": "测试报告", "stock_count": 1},
            "generate_strategy_decision": {"message": "策略分析完成"},
            "query_history": {"message": "暂无推荐跟踪记录", "records": []},
        }

        mock_tools = []
        for name, ret_val in mock_map.items():
            mock_fn = _make_mock_tool(name, ret_val)
            mock_tools.append(FunctionTool(mock_fn))

        self.agent.tools = mock_tools

    def send(self, user_msg: str) -> dict:
        """
        发送消息，返回结构化结果。

        Returns:
            {
                "tool_calls": [{"name": ..., "args": ...}, ...],
                "tool_results": [...],
                "thinking": str,
                "response": str,
                "text_chunks": [str, ...],
            }
        """
        result = {
            "tool_calls": [],
            "tool_results": [],
            "thinking": "",
            "response": "",
            "text_chunks": [],
        }

        for event_type, data in self.mgr.send_message_streaming(user_msg):
            if event_type == "tool_call":
                result["tool_calls"].append(data)
            elif event_type == "tool_result":
                result["tool_results"].append(data)
            elif event_type == "thinking":
                result["thinking"] += data
            elif event_type == "text_chunk":
                result["text_chunks"].append(data)
            elif event_type == "done":
                result["response"] = data
            elif event_type == "error":
                err = str(data)
                if any(marker in err for marker in ("RateLimitError", "too_many_requests", "达到使用量上限", "429")):
                    pytest.skip(f"Live model quota/rate limit: {err[:160]}")
                result["response"] = f"ERROR: {data}"

        return result


# ── 测试用例 ──


@skip_no_key
class TestChatAgentRouting:
    """验证用户消息能路由到正确的工具。"""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.harness = AgentTestHarness(
            api_key=_api_key,
            model=_model,
            provider=_provider,
            base_url=_base_url,
        )

    def test_search_routes_to_search_tool(self):
        """'搜一下宁德时代' → 应调 search_stock_by_name"""
        r = self.harness.send("搜一下宁德时代")
        tool_names = [tc["name"] for tc in r["tool_calls"]]
        assert "search_stock_by_name" in tool_names, f"Expected search tool, got: {tool_names}"
        print(f"[OK] Tool calls: {tool_names}")
        print(f"[Response preview]: {r['response'][:200]}")

    def test_diagnose_routes_to_analyze_stock(self):
        """'帮我看看 000001' → 应调 analyze_stock(mode=diagnose)"""
        r = self.harness.send("帮我看看 000001")
        calls = r["tool_calls"]
        tool_names = [tc["name"] for tc in calls]
        assert "analyze_stock" in tool_names, f"Expected analyze_stock tool, got: {tool_names}"
        diag_call = next(tc for tc in calls if tc["name"] == "analyze_stock")
        assert diag_call["args"].get("mode", "diagnose") == "diagnose", (
            f"Expected diagnose mode, got: {diag_call['args']}"
        )
        print(f"[OK] Tool calls: {tool_names}")

    def test_market_routes_to_market_overview(self):
        """'大盘水温怎么样' → 应调 get_market_overview"""
        r = self.harness.send("今天大盘水温怎么样")
        tool_names = [tc["name"] for tc in r["tool_calls"]]
        assert "get_market_overview" in tool_names, f"Expected market tool, got: {tool_names}"
        print(f"[OK] Tool calls: {tool_names}")

    def test_price_routes_to_analyze_stock(self):
        """'600519 最近走势' → 应调 analyze_stock(mode=price)"""
        r = self.harness.send("600519 最近走势怎么样")
        calls = r["tool_calls"]
        tool_names = [tc["name"] for tc in calls]
        assert "analyze_stock" in tool_names, f"Expected analyze_stock tool, got: {tool_names}"
        price_call = next(tc for tc in calls if tc["name"] == "analyze_stock")
        assert price_call["args"].get("mode") == "price", f"Expected price mode, got: {price_call['args']}"
        print(f"[OK] Tool calls: {tool_names}")


@skip_no_key
class TestChatAgentPersona:
    """验证回复风格符合威科夫人设。"""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.harness = AgentTestHarness(
            api_key=_api_key,
            model=_model,
            provider=_provider,
            base_url=_base_url,
        )

    def test_wyckoff_persona_in_diagnose(self):
        """诊断回复应包含威科夫术语。"""
        r = self.harness.send("帮我看看 000001")
        resp = r["response"]
        # 应包含至少一个威科夫/供需相关关键词
        wyckoff_keywords = [
            "吸筹",
            "派发",
            "主力",
            "供需",
            "综合人",
            "Composite",
            "SOS",
            "Spring",
            "LPS",
            "Phase",
            "支撑",
            "阻力",
            "量价",
            "威科夫",
            "Wyckoff",
            "筹码",
            "多头",
            "空头",
        ]
        found = [kw for kw in wyckoff_keywords if kw in resp]
        assert len(found) >= 2, f"Expected wyckoff terminology, found: {found}\nResponse: {resp[:500]}"
        print(f"[OK] Found Wyckoff terms: {found}")

    def test_chinese_output(self):
        """回复应为中文。"""
        r = self.harness.send("今天大盘水温怎么样")
        resp = r["response"]
        # 简单判断：中文字符占比应显著
        chinese_chars = sum(1 for ch in resp if "\u4e00" <= ch <= "\u9fff")
        total_alpha = sum(1 for ch in resp if ch.isalpha() or "\u4e00" <= ch <= "\u9fff")
        ratio = chinese_chars / max(total_alpha, 1)
        assert ratio > 0.3, f"Expected Chinese output, ratio={ratio:.2f}\nResponse: {resp[:300]}"
        print(f"[OK] Chinese ratio: {ratio:.2f}")

    def test_risk_disclaimer(self):
        """涉及操作建议时应有风险提示。"""
        r = self.harness.send("帮我看看 000001，能不能买")
        resp = r["response"]
        risk_keywords = ["风险", "免责", "不构成", "自主", "谨慎", "止损"]
        found = [kw for kw in risk_keywords if kw in resp]
        # 放宽条件：有风险相关词即可
        assert len(found) >= 1, f"Expected risk disclaimer, found: {found}\nResponse: {resp[:500]}"
        print(f"[OK] Risk terms: {found}")


# ── 直接运行入口 ──

if __name__ == "__main__":
    if not _api_key:
        print("ERROR: 请设置 LONGCAT_API_KEY 或 GEMINI_API_KEY 环境变量")
        sys.exit(1)

    print("=" * 60)
    print(f"读盘室 Agent 端到端测试  provider={_provider}  model={_model}")
    print("=" * 60)

    harness = AgentTestHarness(
        api_key=_api_key,
        model=_model,
        provider=_provider,
        base_url=_base_url,
    )

    test_cases = [
        ("搜索路由", "搜一下宁德时代"),
        ("诊断路由", "帮我看看 000001"),
        ("大盘路由", "今天大盘水温怎么样"),
        ("行情路由", "600519 最近走势"),
        ("持仓审判", "审判我的持仓"),
        ("机会扫描", "有什么机会"),
    ]

    for test_name, user_msg in test_cases:
        print(f"\n{'─' * 40}")
        print(f"测试: {test_name}")
        print(f"用户: {user_msg}")
        print(f"{'─' * 40}")

        r = harness.send(user_msg)

        print(f"工具调用: {[tc['name'] for tc in r['tool_calls']]}")
        if r["thinking"]:
            print(f"思考过程: {r['thinking'][:150]}...")
        print(f"回复片段: {r['response'][:300]}...")
        print(f"回复长度: {len(r['response'])} 字符")
