# -*- coding: utf-8 -*-
"""Agent Skills — 内置 + 用户自定义 skill 加载。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

SKILLS_DIR = Path(os.path.expanduser("~/.wyckoff/skills"))

_VALID_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    prompt: str


# ---------------------------------------------------------------------------
# 内置 Skills
# ---------------------------------------------------------------------------

BUILTIN_SKILLS: dict[str, Skill] = {
    "screen": Skill(
        name="screen",
        description="全市场漏斗筛选，发现结构性机会",
        prompt=(
            "执行全市场威科夫五层漏斗筛选：\n"
            "1. 调用 screen_stocks(board=\"{user_input}\") 执行筛选"
            "（若用户未指定 board 则用 \"all\"）\n"
            "2. 对筛选出的每只股票，调用 analyze_stock(code=..., mode=\"diagnose\") 做深度诊断\n"
            "3. 按健康度排序，给出 Top 5 推荐及理由\n"
            "4. 用表格展示：代码 | 名称 | 阶段 | 触发信号 | 健康度 | 推荐理由"
        ),
    ),
    "checkup": Skill(
        name="checkup",
        description="持仓全面体检，综合诊断建议",
        prompt=(
            "执行持仓全面体检：\n"
            "1. 调用 portfolio(mode=\"diagnose\") 获取持仓诊断数据\n"
            "2. 调用 get_market_overview() 了解大盘环境\n"
            "3. 综合分析每只持仓的威科夫阶段、健康度、风险\n"
            "4. 给出具体操作建议（持有/减仓/加仓/清仓）及理由"
        ),
    ),
    "report": Skill(
        name="report",
        description="AI 深度研报，三阵营分类",
        prompt=(
            "生成 AI 深度研报：\n"
            "1. 调用 generate_ai_report(stock_codes=[{user_input}]) 生成报告\n"
            "2. 将报告内容按三阵营（进攻/防守/观察）分类展示\n"
            "3. 对每只股票给出核心逻辑和风险点"
        ),
    ),
    "strategy": Skill(
        name="strategy",
        description="攻防决策，持仓去留指令",
        prompt=(
            "生成攻防决策：\n"
            "1. 调用 generate_strategy_decision() 获取策略决策\n"
            "2. 清晰展示去留指令：哪些持有、哪些清仓、哪些加仓\n"
            "3. 如有新推荐买入标的，说明入场逻辑和仓位建议"
        ),
    ),
    "backtest": Skill(
        name="backtest",
        description="回测威科夫策略历史表现",
        prompt=(
            "运行策略回测：\n"
            "1. 调用 run_backtest({user_input}) 执行回测\n"
            "   （若用户未指定参数则使用默认值）\n"
            "2. 展示关键指标：总收益率、年化、最大回撤、胜率、盈亏比\n"
            "3. 对策略表现给出简要评价和改进建议"
        ),
    ),
}


# ---------------------------------------------------------------------------
# 加载用户自定义 skills
# ---------------------------------------------------------------------------

def _parse_skill_md(path: Path) -> Skill | None:
    """解析 ~/.wyckoff/skills/<name>.md 文件。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    if not text.startswith("---"):
        name = path.stem.strip().lower()
        if not _VALID_NAME_RE.match(name):
            return None
        return Skill(name=name, description="", prompt=text.strip())

    parts = text.split("---", 2)
    if len(parts) < 3:
        name = path.stem.strip().lower()
        if not _VALID_NAME_RE.match(name):
            return None
        return Skill(name=name, description="", prompt=text.strip())

    import yaml
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except Exception:
        meta = {}

    name = str(meta.get("name", path.stem)).strip().lower()
    if not _VALID_NAME_RE.match(name):
        return None
    description = meta.get("description", "")
    prompt = parts[2].strip()
    return Skill(name=name, description=description, prompt=prompt)


def load_user_skills() -> dict[str, Skill]:
    """从 ~/.wyckoff/skills/ 加载用户自定义 skills。"""
    skills: dict[str, Skill] = {}
    if not SKILLS_DIR.is_dir():
        return skills
    for f in sorted(SKILLS_DIR.glob("*.md")):
        skill = _parse_skill_md(f)
        if skill:
            skills[skill.name] = skill
    return skills


def load_skills() -> dict[str, Skill]:
    """合并内置 + 用户 skills（用户同名覆盖内置）。"""
    merged = dict(BUILTIN_SKILLS)
    merged.update(load_user_skills())
    return merged
