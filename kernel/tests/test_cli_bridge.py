"""CLI 契约：bridge wait/answer 的 JSON 与退出码、agent 模式的 --run-dir 校验、--relearn 透传。

约定（P0）：带 --json 时 stdout 只留一份 JSON；0=正常（含 stale）、2=参数/格式错误。
"""

from __future__ import annotations

import json

import pytest
from fakes import FakeBridge

import ui_agent.bridge_cli as bridge_cli
import ui_agent.cli as cli
from ui_agent.bridge.protocol import read_json, write_json


@pytest.fixture
def bridge_run(tmp_path):
    """造一个像真的桥目录：status=running + 一条待答的 ask。"""
    run_dir = tmp_path / "run"
    folder = run_dir / "bridge"
    folder.mkdir(parents=True)
    write_json(folder / "status.json", {"state": "running", "seq": 1, "detail": "",
                                        "updated_at": "2026-10-19T10:00:00.000"})
    write_json(folder / "ask-0001.json", {"id": 1, "kind": "supervise", "deadline_s": 180.0,
                                          "at": "2026-10-19T10:00:00.000",
                                          "payload": {"stuck": {"trigger": "blocked",
                                                                "detail": "无法推进"}}})
    return run_dir


class TestBridgeWait:
    def test_returns_the_ask_as_json(self, bridge_run, capsys):
        code = bridge_cli.main(["wait", "--run-dir", str(bridge_run), "--timeout", "0", "--json"])
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

        assert code == 0
        assert payload["ok"] is True and payload["event"] == "ask"
        assert payload["kind"] == "supervise" and payload["ask"]["id"] == 1

    def test_heartbeat_when_nothing_is_pending(self, tmp_path, capsys):
        run_dir = tmp_path / "run"
        (run_dir / "bridge").mkdir(parents=True)
        write_json(run_dir / "bridge" / "status.json", {"state": "running", "seq": 0})
        code = bridge_cli.main(["wait", "--run-dir", str(run_dir), "--timeout", "0", "--json"])
        assert code == 0 and json.loads(capsys.readouterr().out)["event"] == "heartbeat"

    def test_finished_event_carries_state_and_result(self, bridge_run, capsys):
        write_json(bridge_run / "bridge" / "status.json",
                   {"state": "finished", "seq": 1, "detail": "status=ok"})
        write_json(bridge_run / "result.json", {"status": "ok", "checks": []})
        code = bridge_cli.main(["wait", "--run-dir", str(bridge_run), "--timeout", "0", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0 and payload["event"] == "finished"
        assert payload["state"] == "finished" and payload["result"]["status"] == "ok"

    def test_missing_bridge_dir_exits_2(self, tmp_path, capsys):
        code = bridge_cli.main(["wait", "--run-dir", str(tmp_path / "nope"),
                                "--wait-dir", "0", "--json"])
        assert code == 2
        assert "桥目录不存在" in capsys.readouterr().err

    def test_waits_for_the_bridge_dir_to_appear(self, tmp_path, capsys):
        """宿主总是「起 run 后立刻 wait」，桥目录要几秒后才出现——不能就此报错退出。"""
        import threading

        run_dir = tmp_path / "late"
        folder = run_dir / "bridge"

        def _create():
            folder.mkdir(parents=True)
            write_json(folder / "status.json", {"state": "running", "seq": 2, "detail": "",
                                                "updated_at": "2026-10-19T10:00:00.000"})
            write_json(folder / "ask-0002.json", {"id": 2, "kind": "decide",
                                                  "deadline_s": 180.0, "at": "2026-10-19T10:00:00.000",
                                                  "payload": {"state": {}, "questions": {}}})

        timer = threading.Timer(0.4, _create)
        timer.start()
        try:
            code = bridge_cli.main(["wait", "--run-dir", str(run_dir), "--wait-dir", "5",
                                    "--timeout", "2", "--json"])
        finally:
            timer.join()
        event = json.loads(capsys.readouterr().out)
        assert code == 0 and event["event"] == "ask" and event["ask"]["id"] == 2

    def test_wait_dir_timeout_still_reports_missing(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(bridge_cli, "_await_dir", lambda path, seconds: False)
        code = bridge_cli.main(["wait", "--run-dir", str(tmp_path / "nope"), "--wait-dir", "3", "--json"])
        err = capsys.readouterr().err
        assert code == 2 and "桥目录不存在" in err and "等了 3s 仍未出现" in err

    def test_json_mode_prints_exactly_one_json_line(self, bridge_run, capsys):
        bridge_cli.main(["wait", "--run-dir", str(bridge_run), "--timeout", "0", "--json"])
        out = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert len(out) == 1 and json.loads(out[0])["event"] == "ask"


class TestBridgeAnswer:
    def test_writes_the_answer(self, bridge_run, capsys):
        code = bridge_cli.main(["answer", "--run-dir", str(bridge_run), "--id", "1",
                                "--data", '{"answer": {"ruling": "HINT", "hint": "先展开"}}',
                                "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0 and payload == {"ok": True, "accepted": True, "id": 1}
        assert read_json(bridge_run / "bridge" / "ans-0001.json")["answer"]["ruling"] == "HINT"

    def test_note_and_answered_by_are_recorded(self, bridge_run):
        bridge_cli.main(["answer", "--run-dir", str(bridge_run), "--id", "1",
                         "--data", '{"answer": {"ruling": "ABORT", "reason": "404"},'
                                   ' "answered_by": "qoder", "note": "入口就挂了"}'])
        envelope = read_json(bridge_run / "bridge" / "ans-0001.json")
        assert envelope["answered_by"] == "qoder" and envelope["note"] == "入口就挂了"

    def test_answer_from_file(self, bridge_run, tmp_path):
        path = tmp_path / "ans.json"
        path.write_text('{"answer": {"text": "sysadmin"}}', encoding="utf-8")
        bridge_cli.main(["answer", "--run-dir", str(bridge_run), "--id", "1",
                         "--file", str(path)])
        assert read_json(bridge_run / "bridge" / "ans-0001.json")["answer"]["text"] == "sysadmin"

    def test_duplicate_answer_reports_stale_but_exits_0(self, bridge_run, capsys):
        data = '{"answer": {"ruling": "HINT", "hint": "x"}}'
        bridge_cli.main(["answer", "--run-dir", str(bridge_run), "--id", "1", "--data", data])
        capsys.readouterr()
        code = bridge_cli.main(["answer", "--run-dir", str(bridge_run), "--id", "1",
                                "--data", data, "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0, "stale 不是错误：晚到的答复只被忽略"
        assert payload["accepted"] is False and payload["reason"] == "stale"

    def test_late_answer_after_finish_is_stale(self, bridge_run, capsys):
        write_json(bridge_run / "bridge" / "status.json", {"state": "finished", "seq": 1})
        code = bridge_cli.main(["answer", "--run-dir", str(bridge_run), "--id", "1",
                                "--data", '{"answer": {"text": "x"}}', "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0 and payload["reason"] == "stale"

    def test_malformed_json_exits_2(self, bridge_run, capsys):
        code = bridge_cli.main(["answer", "--run-dir", str(bridge_run), "--id", "1",
                                "--data", "{not json"])
        assert code == 2 and "合法 JSON" in capsys.readouterr().err

    def test_missing_answer_object_exits_2(self, bridge_run, capsys):
        code = bridge_cli.main(["answer", "--run-dir", str(bridge_run), "--id", "1",
                                "--data", '{"note": "忘了 answer"}'])
        assert code == 2 and "answer 必须是对象" in capsys.readouterr().err

    def test_top_level_must_be_an_object(self, bridge_run, capsys):
        code = bridge_cli.main(["answer", "--run-dir", str(bridge_run), "--id", "1",
                                "--data", "[]"])
        assert code == 2 and "顶层必须是对象" in capsys.readouterr().err

    def test_file_and_data_are_mutually_exclusive(self, bridge_run):
        with pytest.raises(SystemExit):
            bridge_cli.main(["answer", "--run-dir", str(bridge_run), "--id", "1",
                             "--data", "{}", "--file", "x.json"])

    def test_one_of_them_is_required(self, bridge_run):
        with pytest.raises(SystemExit):
            bridge_cli.main(["answer", "--run-dir", str(bridge_run), "--id", "1"])


class TestEntryDispatch:
    def test_bridge_argv_goes_to_the_light_module(self, monkeypatch):
        import ui_agent.entry as entry

        calls: list = []
        monkeypatch.setattr("ui_agent.bridge_cli.main",
                            lambda argv: calls.append(("bridge", argv)) or 0)
        assert entry.main(["bridge", "wait", "--run-dir", "x"]) == 0
        assert calls == [("bridge", ["wait", "--run-dir", "x"])]

    def test_other_argv_goes_to_cli(self, monkeypatch):
        import ui_agent.entry as entry

        calls: list = []
        monkeypatch.setattr("ui_agent.cli.main", lambda argv: calls.append(argv) or 0)
        assert entry.main(["case", "compile", "--in", "x.yaml"]) == 0
        assert calls == [["case", "compile", "--in", "x.yaml"]]

    def test_bridge_module_does_not_pull_in_playwright(self):
        """bridge_cli 只依赖 bridge 包与 settings：不 import driver/runner。"""
        import subprocess
        import sys

        code = ("import sys, ui_agent.bridge_cli;"
                "print('playwright' in sys.modules)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             encoding="utf-8")
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "False", "bridge 子命令不该连带加载 playwright"


class TestAgentModeRunDir:
    def test_agent_decider_requires_run_dir(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(cli, "Runner", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("不该构造 Runner：--run-dir 缺失应当在装配前报错")))
        code = cli.main(["run", "--goal", "查询", "--url", "http://x/",
                         "--decider", "agent", "--run-dir", None,
                         "--json"])
        assert code == 2
        assert "decider=agent 必须指定 --run-dir" in capsys.readouterr().err

    def test_agent_supervisor_requires_run_dir(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(cli, "Runner", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("不该构造 Runner")))
        code = cli.main(["run", "--goal", "查询", "--url", "http://x/",
                         "--supervisor", "agent", "--json"])
        assert code == 2
        assert "--supervisor agent 必须指定 --run-dir" in capsys.readouterr().err

    def test_off_decider_needs_no_run_dir(self, tmp_path, capsys, monkeypatch):
        """零模型回放完全不要求桥、不要求 --run-dir。"""
        class StubRunner:
            def __init__(self, *args, **kwargs):
                self.kwargs = kwargs

            def run(self):
                from ui_agent.run.runner import RunResult
                return RunResult("cache_missed", 0, tmp_path, "no-entry")

        monkeypatch.setattr(cli, "Runner", StubRunner)
        code = cli.main(["run", "--goal", "查询", "--url", "http://x/",
                         "--decider", "off", "--supervisor", "off", "--json"])
        assert code == 1, "用例未成功＝退出码 1，不是装配错误 2"


class TestSupervisorMapping:
    def test_off_maps_to_master_switch_off(self):
        args = _run_args(supervisor="off")
        settings = cli.apply_overrides(cli.Settings(), args)
        assert settings.ui_agent_supervisor is False

    def test_on_maps_to_model_mode(self):
        args = _run_args(supervisor="on")
        settings = cli.apply_overrides(cli.Settings(), args)
        assert settings.ui_agent_supervisor is True
        assert settings.ui_agent_supervisor_mode == "model"

    def test_agent_maps_to_agent_mode(self):
        args = _run_args(supervisor="agent")
        settings = cli.apply_overrides(cli.Settings(), args)
        assert settings.ui_agent_supervisor is True
        assert settings.ui_agent_supervisor_mode == "agent"

    def test_needs_bridge_only_in_agent_modes(self):
        """默认就是 agent 监督（问宿主），所以不带 flag 也要桥；只有总闸关掉才不要。"""
        assert cli.apply_overrides(cli.Settings(), _run_args(decider="agent")).needs_bridge
        assert cli.apply_overrides(cli.Settings(), _run_args(supervisor="agent")).needs_bridge
        assert not cli.apply_overrides(cli.Settings(), _run_args(supervisor="off")).needs_bridge
        msg = "监督缝默认问宿主，默认就需要桥"
        assert cli.apply_overrides(cli.Settings(), _run_args()).needs_bridge, msg

    def test_master_switch_off_ignores_agent_mode(self):
        settings = cli.apply_overrides(cli.Settings(), _run_args(supervisor="off"))
        settings = settings.model_copy(update={"ui_agent_supervisor_mode": "agent"})
        assert not settings.needs_bridge

    def test_bridge_timeout_override(self):
        args = _run_args(bridge_timeout=30.0)
        assert cli.apply_overrides(cli.Settings(), args).ui_agent_bridge_timeout_s == 30.0


class TestBridgeLifecycleInCli:
    def test_bridge_is_started_and_finished(self, tmp_path, monkeypatch):
        created: list = []

        class StubBridge(FakeBridge):
            def __init__(self, path, timeout_s=180.0):
                super().__init__()
                self.path = path
                self.dir = path
                created.append(self)

        monkeypatch.setattr(cli, "AgentBridge", StubBridge)

        class StubRunner:
            def __init__(self, *args, **kwargs):
                self.kwargs = kwargs

            def run(self):
                from ui_agent.run.runner import RunResult
                return RunResult("ok", 1, run_dir, "全部断言通过")

        monkeypatch.setattr(cli, "Runner", StubRunner)
        run_dir = tmp_path / "run"
        code = cli.main(["run", "--goal", "查询", "--url", "http://x/",
                         "--decider", "agent", "--run-dir", str(run_dir), "--json"])

        assert code == 0
        assert len(created) == 1 and created[0].started == 1
        assert created[0].finished[-1].startswith("finished:")
        assert created[0].path == run_dir / "bridge"

    def test_crash_marks_the_bridge_crashed(self, tmp_path, monkeypatch):
        created: list = []

        class StubBridge(FakeBridge):
            def __init__(self, path, timeout_s=180.0):
                super().__init__()
                self.dir = path
                created.append(self)

        monkeypatch.setattr(cli, "AgentBridge", StubBridge)

        class BoomRunner:
            def __init__(self, *args, **kwargs):
                pass

            def run(self):
                raise RuntimeError("驱动炸了")

        monkeypatch.setattr(cli, "Runner", BoomRunner)
        code = cli.main(["run", "--goal", "查询", "--url", "http://x/",
                         "--decider", "agent", "--run-dir", str(tmp_path / "run"), "--json"])
        assert code == 2, "内核错误＝退出码 2，与本改动前一致"
        assert created[0].crashed_detail and "驱动炸了" in created[0].crashed_detail

    def test_keyboard_interrupt_marks_the_bridge_crashed(self, tmp_path, monkeypatch):
        created: list = []

        class StubBridge(FakeBridge):
            def __init__(self, path, timeout_s=180.0):
                super().__init__()
                self.dir = path
                created.append(self)

        monkeypatch.setattr(cli, "AgentBridge", StubBridge)

        class BoomRunner:
            def __init__(self, *args, **kwargs):
                pass

            def run(self):
                raise KeyboardInterrupt

        monkeypatch.setattr(cli, "Runner", BoomRunner)
        code = cli.main(["run", "--goal", "查询", "--url", "http://x/",
                         "--decider", "agent", "--run-dir", str(tmp_path / "run"), "--json"])
        assert code == 130, "Ctrl-C 仍是 130（cli 统一捕获）"
        assert created[0].crashed_detail, "中断也要把桥标成 crashed，否则宿主一直等"


def _run_args(**overrides):
    class Args:
        mode = None
        max_actions = None
        conf_min = None
        decider = None
        supervisor = None
        bridge_timeout = None
        cache = None
        cache_strict = False
        upload_mode = None
        dialog_policy = None

    args = Args()
    for key, value in overrides.items():
        setattr(args, key, value)
    return args
