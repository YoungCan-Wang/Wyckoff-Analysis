# -*- coding: utf-8 -*-
"""
工具注册表 — 复用 agents/chat_tools.py 的 10 个函数，去除 ADK 依赖。

核心思路：
1. ToolContext 用 shim 类替代（只需 .state 属性）
2. 工具 JSON Schema 手动定义（比自动生成更可控）
3. 凭证通过 .env 环境变量提供
"""
from __future__ import annotations

import inspect
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ToolContext shim — 替代 google.adk.tools.ToolContext
# ---------------------------------------------------------------------------

class ToolContext:
    """最小化 ToolContext shim，提供 .state / .provider / .registry。"""

    def __init__(self, state: dict[str, Any] | None = None):
        self.state = state or {}
        self.provider = None
        self.registry = None


# ---------------------------------------------------------------------------
# 工具 Schema 定义（标准 JSON Schema，三家 Provider 通用）
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "search_stock_by_name",
        "description": "根据关键词搜索 A 股股票，支持名称、代码、拼音首字母模糊搜索。最多返回 10 条。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "搜索关键词，如 '宁德' 或 '300750' 或 'gzmt'"},
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "analyze_stock",
        "description": "分析单只 A 股股票：Wyckoff 健康诊断或近期行情查询。",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "6 位股票代码，如 '000001' 或 '600519'"},
                "mode": {"type": "string", "enum": ["diagnose", "price"], "description": "'diagnose' 做 Wyckoff 结构化诊断；'price' 仅返回近期 OHLCV 行情"},
                "cost": {"type": "number", "description": "持仓成本价（仅 diagnose 模式），默认 0"},
                "days": {"type": "integer", "description": "获取天数（仅 price 模式），默认 30，最大 250"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "portfolio",
        "description": "查看或诊断用户持仓。mode='view' 返回持仓列表和资金；mode='diagnose' 对每只持仓做 Wyckoff 健康诊断。",
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["view", "diagnose"], "description": "'view' 仅查看持仓数据；'diagnose' 做持仓诊断"},
            },
        },
    },
    {
        "name": "get_market_overview",
        "description": "获取 A 股大盘环境概览，返回上证、深证、创业板等主要指数的最新收盘和涨跌幅。",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "screen_stocks",
        "description": "运行 Wyckoff 五层漏斗筛选，从全市场筛选出具有结构性机会的股票。整个过程可能需要几分钟。",
        "parameters": {
            "type": "object",
            "properties": {
                "board": {
                    "type": "string",
                    "description": "股票池板块：'all'（全部）、'main'（主板）、'chinext'（创业板）",
                },
            },
        },
    },
    {
        "name": "generate_ai_report",
        "description": "对指定股票列表生成威科夫三阵营 AI 深度研报（逻辑破产/储备营地/起跳板）。需要 Gemini API Key。最多 10 只。",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "股票代码列表，如 ['000001', '600519']",
                },
            },
            "required": ["stock_codes"],
        },
    },
    {
        "name": "generate_strategy_decision",
        "description": "综合持仓和候选标的，生成去留决策（EXIT/TRIM/HOLD/PROBE/ATTACK）。需要 Gemini API Key 和持仓数据。",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "query_history",
        "description": "查询历史记录：AI 推荐追踪、信号确认池或尾盘买入记录。",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["recommendation", "signal", "tail_buy"], "description": "'recommendation' 推荐追踪；'signal' 信号确认池；'tail_buy' 尾盘买入"},
                "status": {"type": "string", "description": "仅 signal：'all'/'pending'/'confirmed'/'expired'"},
                "run_date": {"type": "string", "description": "仅 tail_buy：按日期过滤 YYYY-MM-DD"},
                "decision": {"type": "string", "description": "仅 tail_buy：按决策过滤 BUY/WATCH"},
                "limit": {"type": "integer", "description": "返回记录数上限，默认 20"},
            },
            "required": ["source"],
        },
    },
    {
        "name": "update_portfolio",
        "description": "管理用户持仓或删除追踪记录。操作后返回最新状态。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "update", "remove", "set_cash", "delete_records"],
                    "description": "操作类型：add/update/remove/set_cash 管理持仓；delete_records 删除推荐或信号记录",
                },
                "code": {"type": "string", "description": "6 位股票代码（add/update/remove 时必填）"},
                "name": {"type": "string", "description": "股票名称（可选）"},
                "shares": {"type": "integer", "description": "持仓股数"},
                "cost_price": {"type": "number", "description": "成本价"},
                "buy_dt": {"type": "string", "description": "买入日期（YYYYMMDD 格式）"},
                "free_cash": {"type": "number", "description": "可用资金（set_cash 时使用）"},
                "table": {"type": "string", "description": "仅 delete_records：'recommendation' 或 'signal'"},
                "codes": {"type": "array", "items": {"type": "string"}, "description": "仅 delete_records：股票代码列表"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "check_background_tasks",
        "description": "查询后台任务执行状态。用户问'扫描好了没''任务进度'时调用。",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "run_backtest",
        "description": "回测威科夫五层漏斗策略的历史表现。耗时 3-10 分钟，后台执行。用户问'帮我回测''跑个回测'时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "开始日期 YYYY-MM-DD，默认 6 个月前"},
                "end": {"type": "string", "description": "结束日期 YYYY-MM-DD，默认昨天"},
                "hold_days": {"type": "integer", "description": "最大持仓天数（5/10/15/30），默认 10"},
                "top_n": {"type": "integer", "description": "每日最大候选数，默认 3"},
                "board": {"type": "string", "description": "股票池：'main_chinext'/'main'/'chinext'/'all'"},
                "stop_loss_pct": {"type": "number", "description": "止损百分比（负数），默认 -7.0"},
                "take_profit_pct": {"type": "number", "description": "止盈百分比，默认 18.0"},
            },
        },
    },
    # ── 委派工具 ──
    {
        "name": "delegate_to_research",
        "description": "委派研究员收集市场数据和情报。用于全市场扫描、信号查询、推荐记录、回测等数据收集任务。",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "研究任务描述"},
                "context": {"type": "string", "description": "相关上下文信息（如持仓数据、大盘状态）"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "delegate_to_analysis",
        "description": "委派分析师做深度分析。用于个股诊断、持仓体检、AI 研报等需要 Wyckoff 框架深度分析的任务。",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "分析任务描述"},
                "context": {"type": "string", "description": "相关上下文信息（如行情数据、大盘状态）"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "delegate_to_trading",
        "description": "委派交易员做去留决策。用于持仓去留判断、攻防指令、调仓执行等交易决策任务。",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "交易决策任务描述"},
                "context": {"type": "string", "description": "相关上下文信息（如持仓列表、诊断结果）"},
            },
            "required": ["task"],
        },
    },
    # ── Agent 标准工具 ──
    {
        "name": "exec_command",
        "description": "在用户本地执行 shell 命令并返回输出。可用于安装软件、查看系统状态、运行脚本等。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 shell 命令"},
                "timeout": {"type": "integer", "description": "超时秒数，默认 30，最大 120"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "读取用户本地文件内容。支持 txt/csv/json/xlsx 等格式。用户发来文件路径时使用此工具。CSV/Excel 自动解析为表格预览（前 50 行）。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（绝对路径或 ~ 开头）"},
                "encoding": {"type": "string", "description": "文件编码，默认 utf-8"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "将内容写入用户本地文件。自动创建父目录。可用于导出分析报告、保存数据等。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "要写入的内容"},
                "encoding": {"type": "string", "description": "文件编码，默认 utf-8"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "web_fetch",
        "description": "抓取指定 URL 的网页内容并返回纯文本。可用于查看财经新闻、公告、在线数据等。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取的网页 URL"},
            },
            "required": ["url"],
        },
    },
]

# 后台执行的长任务工具
BACKGROUND_TOOLS = {"screen_stocks", "generate_ai_report", "generate_strategy_decision", "run_backtest"}

# 需要用户确认的高风险写操作工具
CONFIRM_TOOLS = {"exec_command", "write_file", "update_portfolio"}

# 工具中文显示名，用于终端展示
TOOL_DISPLAY_NAMES: dict[str, str] = {
    "search_stock_by_name": "搜索股票",
    "analyze_stock": "个股分析",
    "portfolio": "持仓",
    "get_market_overview": "大盘水温",
    "screen_stocks": "全市场扫描",
    "generate_ai_report": "深度审讯",
    "generate_strategy_decision": "攻防决策",
    "query_history": "历史查询",
    "update_portfolio": "调仓操作",
    "run_backtest": "回测",
    "check_background_tasks": "任务状态",
    "exec_command": "执行命令",
    "read_file": "读取文件",
    "write_file": "写入文件",
    "web_fetch": "抓取网页",
    "delegate_to_research": "委派研究员",
    "delegate_to_analysis": "委派分析师",
    "delegate_to_trading": "委派交易员",
}


# ---------------------------------------------------------------------------
# ToolRegistry — 管理工具注册和执行
# ---------------------------------------------------------------------------

class ToolRegistry:
    """工具注册表：注册、查询 schema、执行工具。"""

    def __init__(self, user_id: str = "", access_token: str = "", refresh_token: str = ""):
        self._tool_context = ToolContext(state={
            "user_id": user_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
        })
        self._tool_context.registry = self
        self._tools = self._register_tools()
        self._bg_manager = None
        self._on_bg_complete = None
        self._confirm_callback = None
        self._always_allowed: set[str] = set()

    def set_provider(self, provider):
        """注入 LLM Provider，供委派工具启动 sub-agent。"""
        self._tool_context.provider = provider

    def set_confirm_callback(self, callback):
        """注入确认回调，高风险工具执行前会调用。callback(name, args) -> dict。"""
        self._confirm_callback = callback

    def set_background_manager(self, bg_manager, on_complete=None):
        from cli.background import BackgroundTaskManager
        self._bg_manager: BackgroundTaskManager = bg_manager
        self._on_bg_complete = on_complete

    @property
    def state(self) -> dict:
        """统一的 session state，__main__ 和工具共享同一份。"""
        return self._tool_context.state

    def _register_tools(self) -> dict[str, callable]:
        """注册所有工具函数。"""
        from agents.chat_tools import (
            search_stock_by_name,
            analyze_stock,
            portfolio,
            get_market_overview,
            screen_stocks,
            generate_ai_report,
            generate_strategy_decision,
            query_history,
            update_portfolio,
            run_backtest,
            exec_command,
            read_file,
            write_file,
            web_fetch,
        )
        from cli.sub_agents import (
            delegate_to_research,
            delegate_to_analysis,
            delegate_to_trading,
        )
        return {
            "search_stock_by_name": search_stock_by_name,
            "analyze_stock": analyze_stock,
            "portfolio": portfolio,
            "get_market_overview": get_market_overview,
            "screen_stocks": screen_stocks,
            "generate_ai_report": generate_ai_report,
            "generate_strategy_decision": generate_strategy_decision,
            "query_history": query_history,
            "update_portfolio": update_portfolio,
            "run_backtest": run_backtest,
            "delegate_to_research": delegate_to_research,
            "delegate_to_analysis": delegate_to_analysis,
            "delegate_to_trading": delegate_to_trading,
            "exec_command": exec_command,
            "read_file": read_file,
            "write_file": write_file,
            "web_fetch": web_fetch,
        }

    def schemas(self) -> list[dict[str, Any]]:
        """返回所有工具的 JSON Schema。"""
        return TOOL_SCHEMAS

    def execute(self, name: str, args: dict[str, Any]) -> Any:
        """执行指定工具，返回结果。长任务自动提交后台。"""
        # check_background_tasks 直接返回状态
        if name == "check_background_tasks":
            if not self._bg_manager:
                return {"tasks": [], "message": "无后台任务"}
            return {"tasks": self._bg_manager.list_tasks()}

        fn = self._tools.get(name)
        if fn is None:
            return {"error": f"未知工具: {name}"}

        # 高风险工具确认
        if name in CONFIRM_TOOLS and self._confirm_callback and name not in self._always_allowed:
            confirm = self._confirm_callback(name, args)
            action = confirm.get("action", "deny")
            if action == "deny":
                return {"error": "用户拒绝执行此操作"}
            if action == "always":
                self._always_allowed.add(name)
            if action == "edit":
                args = confirm.get("modified_args", args)

        # 用副本注入 tool_context，避免污染原始 args（会被序列化进 messages）
        call_args = dict(args)
        sig = inspect.signature(fn)
        if "tool_context" in sig.parameters:
            call_args["tool_context"] = self._tool_context

        # 长任务提交后台
        if name in BACKGROUND_TOOLS and self._bg_manager is not None:
            task_id = f"bg_{int(time.time())}_{name}"
            display = TOOL_DISPLAY_NAMES.get(name, name)
            self._bg_manager.submit(
                task_id, name, fn, call_args,
                on_complete=self._on_bg_complete,
            )
            return {
                "status": "background",
                "task_id": task_id,
                "message": f"{display}已提交后台执行，您可以继续提问。任务完成后会自动通知。",
            }

        try:
            return fn(**call_args)
        except Exception as e:
            logger.exception("Tool %s execution failed", name)
            return {"error": f"工具执行失败: {e}"}

    def display_name(self, name: str) -> str:
        """返回工具的中文显示名。"""
        return TOOL_DISPLAY_NAMES.get(name, name)
