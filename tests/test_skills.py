from __future__ import annotations

import textwrap
from pathlib import Path

from cli.skills import BUILTIN_SKILLS, _parse_skill_md, load_skills


class TestBuiltinSkills:
    def test_five_builtins(self):
        assert len(BUILTIN_SKILLS) == 5

    def test_names(self):
        assert set(BUILTIN_SKILLS.keys()) == {"screen", "checkup", "report", "strategy", "backtest"}

    def test_all_have_description_and_prompt(self):
        for name, skill in BUILTIN_SKILLS.items():
            assert skill.description, f"{name} missing description"
            assert skill.prompt, f"{name} missing prompt"

    def test_user_input_placeholder(self):
        assert "{user_input}" in BUILTIN_SKILLS["screen"].prompt


class TestParseSkillMd:
    def test_with_frontmatter(self, tmp_path: Path):
        md = tmp_path / "morning.md"
        md.write_text(
            textwrap.dedent("""\
            ---
            name: morning
            description: 每日早盘复盘
            ---

            1. 调用 get_market_overview
            2. 综合建议
        """),
            encoding="utf-8",
        )
        skill = _parse_skill_md(md)
        assert skill is not None
        assert skill.name == "morning"
        assert skill.description == "每日早盘复盘"
        assert "get_market_overview" in skill.prompt

    def test_without_frontmatter(self, tmp_path: Path):
        md = tmp_path / "quick.md"
        md.write_text("快速检查持仓", encoding="utf-8")
        skill = _parse_skill_md(md)
        assert skill is not None
        assert skill.name == "quick"
        assert skill.prompt == "快速检查持仓"

    def test_missing_file(self, tmp_path: Path):
        assert _parse_skill_md(tmp_path / "nope.md") is None

    def test_invalid_name_rejected(self, tmp_path: Path):
        md = tmp_path / "bad'name.md"
        md.write_text("prompt content", encoding="utf-8")
        assert _parse_skill_md(md) is None

    def test_invalid_frontmatter_name_rejected(self, tmp_path: Path):
        md = tmp_path / "ok.md"
        md.write_text('---\nname: "bad\'inject"\n---\nprompt', encoding="utf-8")
        assert _parse_skill_md(md) is None


class TestLoadSkills:
    def test_builtin_only(self, monkeypatch):
        monkeypatch.setattr("cli.skills.SKILLS_DIR", Path("/nonexistent"))
        skills = load_skills()
        assert "screen" in skills
        assert len(skills) >= 5

    def test_user_overrides_builtin(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("cli.skills.SKILLS_DIR", tmp_path)
        (tmp_path / "screen.md").write_text(
            textwrap.dedent("""\
            ---
            name: screen
            description: 自定义筛选
            ---
            我的自定义筛选流程
        """),
            encoding="utf-8",
        )
        skills = load_skills()
        assert skills["screen"].description == "自定义筛选"
        assert "自定义筛选流程" in skills["screen"].prompt

    def test_user_adds_new(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("cli.skills.SKILLS_DIR", tmp_path)
        (tmp_path / "morning.md").write_text(
            textwrap.dedent("""\
            ---
            name: morning
            description: 早盘复盘
            ---
            每日早盘
        """),
            encoding="utf-8",
        )
        skills = load_skills()
        assert "morning" in skills
        assert "screen" in skills


class TestLoopGuardRequiredArgs:
    def test_missing_when_wrong_mode(self):
        from cli.loop_guard import TurnExpectation, missing_required_tool

        exp = TurnExpectation(
            required_tool="portfolio",
            reason="test",
            required_args={"mode": "diagnose"},
        )
        assert missing_required_tool(exp, [("portfolio", {"mode": "view"})]) is True

    def test_satisfied_when_correct_mode(self):
        from cli.loop_guard import TurnExpectation, missing_required_tool

        exp = TurnExpectation(
            required_tool="portfolio",
            reason="test",
            required_args={"mode": "diagnose"},
        )
        assert missing_required_tool(exp, [("portfolio", {"mode": "diagnose"})]) is False

    def test_no_required_args_accepts_any(self):
        from cli.loop_guard import TurnExpectation, missing_required_tool

        exp = TurnExpectation(required_tool="portfolio", reason="test")
        assert missing_required_tool(exp, [("portfolio", {})]) is False

    def test_backward_compat_str_entries(self):
        from cli.loop_guard import TurnExpectation, missing_required_tool

        exp = TurnExpectation(required_tool="portfolio", reason="test")
        assert missing_required_tool(exp, ["portfolio"]) is False

    def test_str_entries_fail_when_args_required(self):
        from cli.loop_guard import TurnExpectation, missing_required_tool

        exp = TurnExpectation(
            required_tool="portfolio",
            reason="test",
            required_args={"mode": "diagnose"},
        )
        assert missing_required_tool(exp, ["portfolio"]) is True
