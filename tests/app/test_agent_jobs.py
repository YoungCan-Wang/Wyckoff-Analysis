"""app/agent_jobs.py 的单元测试。

Streamlit 在测试环境中不可用，使用 sys.modules mock 替代。
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ── Mock streamlit before importing app modules ──

_mock_st = MagicMock()
_mock_st.session_state = {}  # 模拟 session_state 为真实 dict


@pytest.fixture(autouse=True)
def _mock_streamlit(monkeypatch):
    """在每个测试前 mock streamlit 并重新导入 agent_jobs。"""
    _mock_st.session_state.clear()
    monkeypatch.setitem(sys.modules, "streamlit", _mock_st)
    # 清除缓存的导入
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("app.agent_jobs"):
            del sys.modules[mod_name]
    yield


# ── Tests ──


class TestAgentModeEnabled:
    """测试 agent_mode_enabled() 环境变量检测。"""

    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_MODE", None)
            from app.agent_jobs import agent_mode_enabled

            assert agent_mode_enabled() is False

    def test_enabled_with_1(self):
        with patch.dict(os.environ, {"AGENT_MODE": "1"}):
            from app.agent_jobs import agent_mode_enabled

            assert agent_mode_enabled() is True

    def test_enabled_with_true(self):
        with patch.dict(os.environ, {"AGENT_MODE": "true"}):
            from app.agent_jobs import agent_mode_enabled

            assert agent_mode_enabled() is True

    def test_enabled_with_yes(self):
        with patch.dict(os.environ, {"AGENT_MODE": "yes"}):
            from app.agent_jobs import agent_mode_enabled

            assert agent_mode_enabled() is True

    def test_disabled_with_0(self):
        with patch.dict(os.environ, {"AGENT_MODE": "0"}):
            from app.agent_jobs import agent_mode_enabled

            assert agent_mode_enabled() is False


class TestJobStore:
    """测试进程内存储的读写。"""

    def test_store_update_and_get(self):
        from app.agent_jobs import _store_get, _store_update

        run_id = "test_store_001"
        _store_update(run_id, {"status": "queued"})
        result = _store_get(run_id)
        assert result is not None
        assert result["status"] == "queued"

        _store_update(run_id, {"status": "completed", "result": {"ok": True}})
        result = _store_get(run_id)
        assert result["status"] == "completed"
        assert result["result"]["ok"] is True

    def test_store_get_missing(self):
        from app.agent_jobs import _store_get

        assert _store_get("nonexistent_run_id_xyz") is None


class TestRunJob:
    """测试 _run_job 内部执行逻辑。"""

    def test_funnel_screen_success(self):
        from app.agent_jobs import _run_job, _store_get

        fake_result = {"request_id": "test_rj_001", "ok": True, "job_kind": "funnel_screen"}
        with patch("scripts.web_background_job._run_funnel_screen", return_value=fake_result):
            with patch("scripts.web_background_job._apply_funnel_env"):
                _run_job("funnel_screen", "test_rj_001", {})

        job = _store_get("test_rj_001")
        assert job is not None
        assert job["status"] == "completed"
        assert job["result"]["ok"] is True

    def test_batch_ai_report_success(self):
        from app.agent_jobs import _run_job, _store_get

        fake_result = {"request_id": "test_rj_002", "ok": True, "report_text": "## test"}
        with patch("scripts.web_background_job._run_batch_ai_report", return_value=fake_result):
            _run_job("batch_ai_report", "test_rj_002", {"symbols_info": [{"code": "600056"}]})

        job = _store_get("test_rj_002")
        assert job is not None
        assert job["status"] == "completed"
        assert job["result"]["report_text"] == "## test"

    def test_unknown_job_kind_fails(self):
        from app.agent_jobs import _run_job, _store_get

        _run_job("unknown_kind", "test_rj_003", {})

        job = _store_get("test_rj_003")
        assert job is not None
        assert job["status"] == "failed"
        assert "不支持的 job_kind" in job["result"]["error"]

    def test_exception_handling(self):
        from app.agent_jobs import _run_job, _store_get

        with (
            patch(
                "scripts.web_background_job._run_funnel_screen",
                side_effect=RuntimeError("data fetch timeout"),
            ),
            patch("scripts.web_background_job._apply_funnel_env"),
        ):
            _run_job("funnel_screen", "test_rj_004", {})

        job = _store_get("test_rj_004")
        assert job is not None
        assert job["status"] == "failed"
        assert "data fetch timeout" in job["result"]["error"]


class TestFakeRun:
    """测试 _FakeRun 兼容性。"""

    def test_attributes(self):
        from app.agent_jobs import _FakeRun

        run = _FakeRun(run_id="test_fr_005", status="completed", conclusion="success")
        assert run.run_id == "test_fr_005"
        assert run.status == "completed"
        assert run.conclusion == "success"
        assert run.html_url == ""

    def test_repr(self):
        from app.agent_jobs import _FakeRun

        run = _FakeRun(run_id="r1", status="in_progress")
        assert "r1" in repr(run)


class TestSubmitAgentJob:
    """测试 submit_agent_job 提交逻辑。"""

    def test_submit_creates_thread_and_state(self):
        """提交任务应创建 daemon thread 并写入 session_state。"""
        with patch("app.agent_jobs.threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread

            from app.agent_jobs import submit_agent_job

            run_id = submit_agent_job(
                "funnel_screen",
                {"pool_mode": "board"},
                state_key="test_state_key",
            )

            assert run_id.startswith("funnel_screen_")
            mock_thread.start.assert_called_once()
            mock_thread_cls.assert_called_once()
            # 验证 daemon=True
            _, kwargs = mock_thread_cls.call_args
            assert kwargs.get("daemon") is True

            state = _mock_st.session_state.get("test_state_key")
            assert state is not None
            assert state["_agent_mode"] is True
            assert state["request_id"] == run_id


class TestSyncAgentJobState:
    """测试 sync_agent_job_state 状态读取。"""

    def test_sync_completed_job(self):
        from app.agent_jobs import _store_update, sync_agent_job_state

        run_id = "sync_test_001"
        _store_update(
            run_id,
            {
                "status": "completed",
                "result": {"ok": True, "report_text": "done"},
            },
        )

        _mock_st.session_state["sync_test_key"] = {
            "job_kind": "batch_ai_report",
            "request_id": run_id,
            "run": None,
            "result": None,
            "_agent_mode": True,
        }

        state = sync_agent_job_state(state_key="sync_test_key")
        assert state is not None
        assert state["run"].status == "completed"
        assert state["run"].conclusion == "success"
        assert state["result"]["ok"] is True

    def test_sync_in_progress_job(self):
        from app.agent_jobs import _store_update, sync_agent_job_state

        run_id = "sync_test_002"
        _store_update(run_id, {"status": "in_progress"})

        _mock_st.session_state["sync_test_key2"] = {
            "job_kind": "funnel_screen",
            "request_id": run_id,
            "run": None,
            "result": None,
            "_agent_mode": True,
        }

        state = sync_agent_job_state(state_key="sync_test_key2")
        assert state is not None
        assert state["run"].status == "in_progress"
        assert state["result"] is None

    def test_sync_failed_job(self):
        from app.agent_jobs import _store_update, sync_agent_job_state

        run_id = "sync_test_003"
        _store_update(
            run_id,
            {
                "status": "failed",
                "result": {"ok": False, "error": "boom"},
            },
        )

        _mock_st.session_state["sync_test_key3"] = {
            "job_kind": "funnel_screen",
            "request_id": run_id,
            "run": None,
            "result": None,
            "_agent_mode": True,
        }

        state = sync_agent_job_state(state_key="sync_test_key3")
        assert state is not None
        assert state["run"].status == "completed"
        assert state["run"].conclusion == "failure"
        assert state["result"]["ok"] is False

    def test_sync_no_state_returns_none(self):
        from app.agent_jobs import sync_agent_job_state

        result = sync_agent_job_state(state_key="nonexistent_key_xyz")
        assert result is None
