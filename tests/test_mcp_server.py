from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType


class FakeFastMCP:
    def __init__(self, name: str) -> None:
        self.name = name

    def tool(self):
        return lambda func: func

    def run(self) -> None:
        return None


def import_mcp_server(monkeypatch):
    mcp_pkg = ModuleType("mcp")
    server_pkg = ModuleType("mcp.server")
    fastmcp_pkg = ModuleType("mcp.server.fastmcp")
    fastmcp_pkg.FastMCP = FakeFastMCP
    monkeypatch.setitem(sys.modules, "mcp", mcp_pkg)
    monkeypatch.setitem(sys.modules, "mcp.server", server_pkg)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_pkg)
    sys.modules.pop("mcp_server", None)
    return importlib.import_module("mcp_server")


def test_run_funnel_simulation_maps_main_chinext_and_restores_env(monkeypatch):
    mcp_server = import_mcp_server(monkeypatch)
    captured_env = {}
    fake_pipeline = ModuleType("core.funnel_pipeline")

    def fake_run_funnel(*args, **kwargs):
        captured_env["mode"] = os.environ.get("FUNNEL_POOL_MODE")
        captured_env["board"] = os.environ.get("FUNNEL_POOL_BOARD")
        captured_env["executor"] = os.environ.get("FUNNEL_EXECUTOR_MODE")
        return True, [{"code": "000001"}], {"regime": "NEUTRAL"}, {"metrics": {}}

    fake_pipeline.run_funnel = fake_run_funnel
    monkeypatch.setitem(sys.modules, "core.funnel_pipeline", fake_pipeline)
    monkeypatch.setenv("FUNNEL_POOL_MODE", "manual")
    monkeypatch.setenv("FUNNEL_POOL_BOARD", "chinext")
    monkeypatch.setenv("FUNNEL_EXECUTOR_MODE", "process")

    result = mcp_server.run_funnel_simulation(board="main_chinext")

    assert result["success"] is True
    assert captured_env == {"mode": "board", "board": "all", "executor": "thread"}
    assert os.environ["FUNNEL_POOL_MODE"] == "manual"
    assert os.environ["FUNNEL_POOL_BOARD"] == "chinext"
    assert os.environ["FUNNEL_EXECUTOR_MODE"] == "process"
