# -*- coding: utf-8 -*-
"""命令面板 Provider — 为 Ctrl+P 提供模糊搜索命令列表。"""
from __future__ import annotations

from textual.command import Hit, Hits, Provider


def _skill_commands() -> list[tuple[str, str, str]]:
    from cli.skills import load_skills
    return [(f"/{s.name}", f"run_skill('{s.name}')", s.description) for s in load_skills().values()]


class WyckoffCommands(Provider):
    """Wyckoff CLI 命令面板。"""

    async def search(self, query: str) -> Hits:
        commands = [
            ("切换模型", "switch_model", "选择当前使用的模型"),
            ("恢复对话", "resume_session", "恢复历史会话"),
            ("新对话", "new_chat", "清空消息开始新对话"),
            ("清屏", "clear_chat", "清空聊天记录"),
            ("模型列表", "list_models", "查看已配置的模型"),
            ("添加模型", "add_model", "配置新的 LLM 模型"),
            ("登录", "start_login", "登录 Wyckoff 账号"),
            ("退出登录", "do_logout", "退出当前账号"),
            ("Token 用量", "show_token", "查看本次会话 Token 用量"),
            ("切换主题", "switch_theme", "切换终端配色主题"),
            ("退出", "quit", "退出程序"),
        ] + _skill_commands()
        matcher = self.matcher(query)
        for name, action, help_text in commands:
            score = matcher.match(name)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(name),
                    partial=action,
                    help=help_text,
                )

    async def discover(self) -> Hits:
        """未输入时显示全部命令。"""
        commands = [
            ("切换模型", "switch_model", "/model"),
            ("恢复对话", "resume_session", "/resume"),
            ("新对话", "new_chat", "Ctrl+N"),
            ("清屏", "clear_chat", "Ctrl+L"),
            ("模型列表", "list_models", "/model list"),
            ("添加模型", "add_model", "/model add"),
            ("切换主题", "switch_theme", ""),
            ("Token 用量", "show_token", "/token"),
            ("退出", "quit", "Ctrl+Q"),
        ] + _skill_commands()
        for name, action, help_text in commands:
            yield Hit(1.0, name, partial=action, help=help_text)
