"""app/background_jobs.py AGENT_MODE 开关集成测试。

Streamlit 和 integrations.github_actions 在测试环境中不可用，使用 mock 替代。
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ── Mock streamlit + supabase + github_actions before importing ──

_mock_st = MagicMock()
_mock_st.session_state = {}


@pytest.fixture(autouse=True)
def _mock_dependencies(monkeypatch):
    """Mock streamlit 和相关依赖。"""
    _mock_st.session_state.clear()
    monkeypatch.setitem(sys.modules, "streamlit", _mock_st)
    # mock supabase
    for mod in ["supabase", "postgrest", "postgrest.exceptions"]:
        if mod not in sys.modules:
            monkeypatch.setitem(sys.modules, mod, MagicMock())
    # 清除缓存导入
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith(("app.agent_jobs", "app.background_jobs", "integrations.github_actions")):
            del sys.modules[mod_name]
    # Mock github_actions 模块
    mock_gh = MagicMock()
    mock_gh.github_actions_ready = MagicMock(return_value=(True, ""))
    mock_gh.background_jobs_allowed_for_user = MagicMock(return_value=True)
    mock_gh.trigger_web_job = MagicMock(return_value="req_gh_123")
    mock_gh.find_run_by_request_id = MagicMock(return_value=None)
    mock_gh.load_latest_result = MagicMock(return_value=(None, None))
    mock_gh.load_result_json_for_run = MagicMock(return_value=None)
    mock_gh.clear_github_actions_caches = MagicMock()
    monkeypatch.setitem(sys.modules, "integrations.github_actions", mock_gh)
    yield


# ── Tests ──


class TestBackgroundJobsAgentModeSwitch:
    """验证 AGENT_MODE 开关正确路由到不同实现。"""

    def test_ready_check_agent_mode(self):
        """AGENT_MODE=1 时，ready 检查不需要 GH Actions Token。"""
        _mock_st.session_state["user"] = {"id": "test-user-001"}

        with patch.dict(os.environ, {"AGENT_MODE": "1"}):
            from app.background_jobs import background_jobs_ready_for_current_user

            ready, msg = background_jobs_ready_for_current_user()
            assert ready is True
            assert msg == ""

    def test_ready_check_agent_mode_no_login(self):
        """AGENT_MODE=1 但未登录时，仍然返回未登录。"""
        _mock_st.session_state.pop("user", None)

        with patch.dict(os.environ, {"AGENT_MODE": "1"}):
            from app.background_jobs import background_jobs_ready_for_current_user

            ready, msg = background_jobs_ready_for_current_user()
            assert ready is False
            assert "未登录" in msg

    def test_ready_check_gh_actions_mode(self):
        """AGENT_MODE 关闭时，走 GH Actions 路径。"""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_MODE", None)
            # 让 github_actions_ready 返回 False
            gh_mod = sys.modules["integrations.github_actions"]
            gh_mod.github_actions_ready.return_value = (False, "未配置 GITHUB_ACTIONS_TOKEN")

            from app.background_jobs import background_jobs_ready_for_current_user

            ready, msg = background_jobs_ready_for_current_user()
            assert ready is False
            assert "GITHUB_ACTIONS_TOKEN" in msg

    def test_submit_agent_mode(self):
        """AGENT_MODE=1 时，submit 走 agent_jobs。"""
        _mock_st.session_state["user"] = {"id": "test-user-001"}

        with patch.dict(os.environ, {"AGENT_MODE": "1"}):
            with patch("app.agent_jobs.submit_agent_job", return_value="funnel_screen_abc123") as mock_submit:
                from app.background_jobs import submit_background_job

                rid = submit_background_job("funnel_screen", {"board": "all"}, state_key="test_key")
                assert rid == "funnel_screen_abc123"
                mock_submit.assert_called_once()

    def test_submit_gh_actions_mode(self):
        """AGENT_MODE 关闭时，submit 走 trigger_web_job。"""
        _mock_st.session_state["user"] = {"id": "test-user-001"}

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_MODE", None)
            from app.background_jobs import submit_background_job

            rid = submit_background_job("funnel_screen", {"board": "all"}, state_key="test_gh_key")

            gh_mod = sys.modules["integrations.github_actions"]
            gh_mod.trigger_web_job.assert_called_once()
            assert rid == "req_gh_123"

    def test_sync_agent_mode_flag(self):
        """session_state 中 _agent_mode=True 时，sync 走 agent_jobs。"""
        _mock_st.session_state["test_sync_key"] = {
            "job_kind": "funnel_screen",
            "request_id": "test_run",
            "run": None,
            "result": None,
            "_agent_mode": True,
        }

        with patch("app.agent_jobs.sync_agent_job_state", return_value={"status": "done"}) as mock_sync:
            from app.background_jobs import sync_background_job_state

            sync_background_job_state(state_key="test_sync_key")
            mock_sync.assert_called_once_with(state_key="test_sync_key")

    def test_sync_gh_actions_flag(self):
        """session_state 中无 _agent_mode 时，走 GH Actions 路径。"""
        _mock_st.session_state["test_sync_gh"] = {
            "job_kind": "funnel_screen",
            "request_id": "req_xyz",
            "run": None,
            "result": None,
        }

        from app.background_jobs import sync_background_job_state

        sync_background_job_state(state_key="test_sync_gh")

        gh_mod = sys.modules["integrations.github_actions"]
        gh_mod.find_run_by_request_id.assert_called_once_with("req_xyz")
