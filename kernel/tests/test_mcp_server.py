"""MCP 适配器：工具面、参数映射、桥往返、stale、未知 run——不联网、不起真浏览器。

MCP server 本身（stdio 协议）依赖 mcp 包，单测只覆盖「内核侧可验证的部分」：
argv 组装、工具分发、桥握手。真 server 的注册留给 SKILL 文档与实际宿主验证。
"""

from __future__ import annotations

import pytest
from fakes import FakeBridge

from ui_agent import mcp_server as mcp
from ui_agent.bridge.protocol import read_json, write_json


class TestToolSurface:
    def test_five_tools_are_declared(self):
        names = {tool["name"] for tool in mcp.TOOLS}
        assert names == {"ui_agent_start", "ui_agent_next", "ui_agent_answer",
                         "ui_agent_status", "ui_agent_abort"}

    def test_every_tool_has_a_schema_and_a_handler(self):
        for tool in mcp.TOOLS:
            assert tool["description"] and tool["inputSchema"]["type"] == "object"
            assert tool["name"] in mcp.DISPATCH

    def test_unknown_tool_is_an_error(self):
        with pytest.raises(KeyError, match="未知工具"):
            mcp.call_tool("ui_agent_teleport", {})


class TestArgv:
    def test_plan_mode_builds_the_same_argv_as_the_cli(self, tmp_path):
        settings = mcp._settings()
        argv = mcp._argv_for_start(settings, plan_file="p.json", url="http://x/",
                                   run_dir=tmp_path, decider="jev", supervisor="agent")
        assert "--plan-file" in argv and "p.json" in argv
        assert "--decider" in argv and "jev" in argv
        assert "--supervisor" in argv and "agent" in argv
        assert "--json" in argv and "--run-dir" in argv

    def test_goal_mode_carries_goal_and_url(self, tmp_path):
        argv = mcp._argv_for_start(mcp._settings(), goal="查询", url="http://x/",
                                   run_dir=tmp_path)
        assert argv[argv.index("--goal") + 1] == "查询"
        assert argv[argv.index("--url") + 1] == "http://x/"

    def test_vars_become_repeated_flags(self, tmp_path):
        argv = mcp._argv_for_start(mcp._settings(), goal="g", url="u", run_dir=tmp_path,
                                   vars_={"账号": "a", "密码": "b"})
        assert argv.count("--var") == 2
        assert "账号=a" in argv and "密码=b" in argv

    def test_bridge_timeout_is_passed_through(self, tmp_path):
        argv = mcp._argv_for_start(mcp._settings(), goal="g", url="u", run_dir=tmp_path,
                                   bridge_timeout=30.0)
        assert argv[argv.index("--bridge-timeout") + 1] == "30.0"


class TestBridgeHandshake:
    """用真实桥目录验证 next / answer 的契约（与 CLI 的 bridge wait/answer 同源）。"""

    @pytest.fixture
    def handle(self, tmp_path, monkeypatch):
        run_dir = tmp_path / "run"
        bridge_dir = run_dir / "bridge"
        bridge_dir.mkdir(parents=True)
        write_json(bridge_dir / "status.json", {"state": "running", "seq": 1})
        write_json(bridge_dir / "ask-0001.json",
                   {"id": 1, "kind": "decide", "deadline_s": 180.0, "payload": {}})
        h = mcp.RunHandle(run_id="run", run_dir=run_dir, argv=[])
        mcp.REGISTRY.add(h)
        monkeypatch.setattr(mcp, "REGISTRY", mcp.REGISTRY)   # 显式：保持同一注册表
        yield h
        mcp.REGISTRY.drop("run")

    def test_next_returns_the_pending_ask(self, handle):
        event = mcp.next_event(run_id="run", timeout_s=0)
        assert event["event"] == "ask" and event["ask"]["id"] == 1
        assert event["run_id"] == "run"

    def test_answer_is_accepted(self, handle):
        result = mcp.answer(run_id="run", ask_id=1, answer={"answers": {"a": 1}})
        assert result["accepted"] is True
        assert read_json(handle.run_dir / "bridge" / "ans-0001.json")["answer"] == {"answers": {"a": 1}}

    def test_second_answer_is_stale(self, handle):
        mcp.answer(run_id="run", ask_id=1, answer={"answers": {}})
        assert mcp.answer(run_id="run", ask_id=1, answer={"answers": {}})["reason"] == "stale"

    def test_next_after_finish_reports_finished(self, handle):
        write_json(handle.run_dir / "bridge" / "status.json",
                   {"state": "finished", "seq": 1, "detail": "status=ok"})
        event = mcp.next_event(run_id="run", timeout_s=0)
        assert event["event"] == "finished" and event["state"] == "finished"

    def test_heartbeat_when_nothing_pending(self, handle):
        (handle.run_dir / "bridge" / "ask-0001.json").unlink()
        assert mcp.next_event(run_id="run", timeout_s=0)["event"] == "heartbeat"

    def test_status_reports_process_state(self, handle):
        data = mcp.status(run_id="run")
        assert data["state"] == "running" and data["run_id"] == "run"
        assert data["process_alive"] is False, "没起进程时为 False，但 status.json 照读"
        assert data["exit_code"] is None

    def test_unknown_run_id_is_an_error(self):
        with pytest.raises(KeyError, match="未知的 run_id"):
            mcp.next_event(run_id="nope", timeout_s=0)


class TestTimeoutClamp:
    def test_timeout_is_clamped_to_the_max(self, handle=None):
        """宿主可能传超大值：适配器夹到上限，避免单次工具调用被宿主杀死。"""
        assert mcp.MAX_NEXT_TIMEOUT_S == 300.0
        assert mcp.DEFAULT_NEXT_TIMEOUT_S == 60.0


class TestAbort:
    def test_abort_on_a_run_we_did_not_start_is_a_no_op(self, tmp_path):
        h = mcp.RunHandle(run_id="foreign", run_dir=tmp_path, argv=[], proc=None)
        mcp.REGISTRY.add(h)
        try:
            result = mcp.abort(run_id="foreign")
            assert result["ok"] is True and result["aborted"] is False
        finally:
            mcp.REGISTRY.drop("foreign")


class TestRegistry:
    def test_add_get_drop(self, tmp_path):
        h = mcp.RunHandle(run_id="x", run_dir=tmp_path, argv=[])
        mcp.REGISTRY.add(h)
        assert mcp.REGISTRY.get("x") is h and h in mcp.REGISTRY.all()
        mcp.REGISTRY.drop("x")
        assert mcp.REGISTRY.get("x") is None

    def test_bridge_dir_follows_the_settings_convention(self, tmp_path):
        h = mcp.RunHandle(run_id="x", run_dir=tmp_path, argv=[])
        assert h.bridge_dir == tmp_path / "bridge"


class TestServeWithoutMcpPackage:
    def test_missing_mcp_package_gives_a_clear_message(self, monkeypatch, capsys):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("mcp.") or name == "mcp":
                raise ImportError("no mcp")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert mcp.serve() == 2
        err = capsys.readouterr().err
        assert "缺少 mcp 包" in err and "内核本身不依赖它" in err
        _ = FakeBridge
