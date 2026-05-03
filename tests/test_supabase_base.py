"""integrations/supabase_base.py 冒烟测试。"""

from __future__ import annotations

from integrations.supabase_base import is_admin_configured


class TestIsAdminConfigured:
    def test_not_configured_when_env_empty(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        monkeypatch.delenv("SUPABASE_KEY", raising=False)
        assert is_admin_configured() is False

    def test_configured_when_env_set(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-key-123")
        assert is_admin_configured() is True
