"""Agent 桥客户端：ask 往返、超时、坏答复、id 不匹配、stale、wait 四种事件——不真睡。"""

from __future__ import annotations

import pytest

from ui_agent.bridge.client import AgentBridge
from ui_agent.bridge.protocol import (
    BridgeTimeout,
    BridgeUnavailable,
    ans_name,
    ask_name,
    read_json,
    write_json,
)


def bridge_for(tmp_path, ticks: list[float] | None = None, **kwargs) -> AgentBridge:
    """注入时钟：sleep 不真睡，只是把 ticks 推进一步。"""
    state = {"now": 0.0}

    def clock() -> float:
        return state["now"]

    def sleeper(seconds: float) -> None:
        state["now"] += seconds

    bridge = AgentBridge(tmp_path / "bridge", clock=clock, sleeper=sleeper, **kwargs)
    bridge.ticks = state            # 让用例能看时间走了多少
    _ = ticks
    return bridge


class TestStart:
    def test_start_creates_dir_and_running_status(self, tmp_path):
        bridge = bridge_for(tmp_path)
        bridge.start()
        status = read_json(bridge.status_path)
        assert bridge.dir.is_dir() and status["state"] == "running" and status["seq"] == 0

    def test_start_scans_existing_ids_so_restart_does_not_collide(self, tmp_path):
        bridge = bridge_for(tmp_path)
        bridge.start()
        write_json(bridge.ask_path(5), {"id": 5, "kind": "decide"})
        again = bridge_for(tmp_path)
        again.start()
        assert again._scan_max_id() == 5


class TestAsk:
    def test_immediate_answer_returns_payload_and_meta(self, tmp_path):
        bridge = bridge_for(tmp_path, poll_s=0.01, timeout_s=10)
        bridge.start()

        def answer_immediately() -> None:
            # 模拟宿主：内核一写 ask，ans 就在下一次轮询出现
            write_json(bridge.ans_path(1), {"id": 1, "answer": {"ruling": "HINT", "hint": "x"}})

        bridge._sleep = lambda s: answer_immediately()
        data, meta = bridge.ask("supervise", {"stuck": {"trigger": "blocked"}})

        assert data == {"ruling": "HINT", "hint": "x"}
        assert meta["via"] == "agent" and meta["ask_id"] == 1 and meta["answered_by"] == "agent"
        assert meta["latency_ms"] >= 0

    def test_ask_file_carries_the_envelope(self, tmp_path):
        bridge = bridge_for(tmp_path, timeout_s=7.5)
        bridge.start()
        bridge._sleep = lambda s: write_json(bridge.ans_path(1), {"id": 1, "answer": {"ok": 1}})
        bridge.ask("text", {"field": {"label": "账号"}})

        envelope = read_json(bridge.ask_path(1))
        assert envelope["kind"] == "text" and envelope["deadline_s"] == 7.5
        assert envelope["payload"]["field"]["label"] == "账号"
        assert envelope["at"], "ask 要带时间戳（审计用）"

    def test_ids_are_consecutive_across_asks(self, tmp_path):
        bridge = bridge_for(tmp_path, poll_s=0.01)
        bridge.start()
        delivered = {"n": 0}

        def sleeper(seconds: float) -> None:
            delivered["n"] += 1
            write_json(bridge.ans_path(delivered["n"]), {"id": delivered["n"],
                                                         "answer": {"n": delivered["n"]}})

        bridge._sleep = sleeper
        first, meta1 = bridge.ask("decide", {})
        second, meta2 = bridge.ask("decide", {})
        assert (meta1["ask_id"], meta2["ask_id"]) == (1, 2)
        assert second == {"n": 2}


class TestAskFailures:
    def test_timeout_raises_with_bridge_timeout_wording(self, tmp_path):
        bridge = bridge_for(tmp_path, poll_s=0.25, timeout_s=1.0)
        bridge.start()
        with pytest.raises(BridgeTimeout, match=r"bridge timeout: kind=supervise id=1 after 1\.0s"):
            bridge.ask("supervise", {})

    def test_timeout_leaves_the_ask_file_for_audit(self, tmp_path):
        bridge = bridge_for(tmp_path, poll_s=0.25, timeout_s=0.5)
        bridge.start()
        with pytest.raises(BridgeTimeout):
            bridge.ask("decide", {})
        assert bridge.ask_path(1).is_file(), "超时的 ask 要留在桥目录里作审计"

    def test_broken_answer_json_is_unavailable(self, tmp_path):
        bridge = bridge_for(tmp_path, poll_s=0.01, timeout_s=10)
        bridge.start()

        def write_garbage(seconds: float) -> None:
            bridge.ans_path(1).write_text("{not json", encoding="utf-8")

        bridge._sleep = write_garbage
        with pytest.raises(BridgeUnavailable, match="无法解析"):
            bridge.ask("decide", {})

    def test_answer_with_wrong_id_is_rejected(self, tmp_path):
        bridge = bridge_for(tmp_path, poll_s=0.01, timeout_s=10)
        bridge.start()
        bridge._sleep = lambda s: write_json(bridge.ans_path(1), {"id": 99, "answer": {"a": 1}})
        with pytest.raises(BridgeUnavailable, match="id 与 ask 不匹配"):
            bridge.ask("decide", {})

    def test_answer_missing_the_answer_object_is_rejected(self, tmp_path):
        bridge = bridge_for(tmp_path, poll_s=0.01, timeout_s=10)
        bridge.start()
        bridge._sleep = lambda s: write_json(bridge.ans_path(1), {"id": 1})
        with pytest.raises(BridgeUnavailable, match="ans 结构不合法"):
            bridge.ask("text", {})


class TestStatusLifecycle:
    def test_finish_marks_finished_and_keeps_seq(self, tmp_path):
        bridge = bridge_for(tmp_path)
        bridge.start()
        write_json(bridge.status_path, {"state": "running", "seq": 4, "detail": ""})
        bridge.finish("status=ok")
        status = read_json(bridge.status_path)
        assert status["state"] == "finished" and status["seq"] == 4
        assert status["detail"] == "status=ok"

    def test_crashed_keeps_the_exception_digest(self, tmp_path):
        bridge = bridge_for(tmp_path)
        bridge.start()
        bridge.crashed("KeyboardInterrupt: 用户中断")
        status = read_json(bridge.status_path)
        assert status["state"] == "crashed" and "KeyboardInterrupt" in status["detail"]

    def test_finish_from_result_reads_the_status_field(self, tmp_path):
        bridge = bridge_for(tmp_path)
        bridge.start()
        bridge.finish_from_result({"status": "check_failed"})
        assert read_json(bridge.status_path)["detail"] == "status=check_failed"


class TestCLIWait:
    def test_missing_status_is_treated_as_running(self, tmp_path):
        """启动竞态：status 还没写出来时不要误报 finished。"""
        bridge = bridge_for(tmp_path, poll_s=0.01)
        bridge.dir.mkdir(parents=True)
        assert bridge.wait(timeout_s=0.0) == {"ok": True, "event": "heartbeat"}

    def test_returns_the_earliest_unanswered_ask(self, tmp_path):
        bridge = bridge_for(tmp_path, poll_s=0.01)
        bridge.start()
        write_json(bridge.ask_path(1), {"id": 1, "kind": "supervise", "payload": {}})
        write_json(bridge.ask_path(2), {"id": 2, "kind": "decide", "payload": {}})
        write_json(bridge.ans_path(1), {"id": 1, "answer": {"a": 1}})

        event = bridge.wait(timeout_s=0.0)
        assert event["event"] == "ask" and event["kind"] == "decide"
        assert event["ask"]["id"] == 2

    def test_finished_wins_over_a_stale_unanswered_ask(self, tmp_path):
        """已结束的运行即使还有过期 ask，也不再让宿主作答。"""
        bridge = bridge_for(tmp_path, poll_s=0.01)
        bridge.start()
        write_json(bridge.ask_path(1), {"id": 1, "kind": "decide", "payload": {}})
        bridge.finish("status=ok")

        event = bridge.wait(timeout_s=0.0)
        assert event["event"] == "finished" and event["state"] == "finished"

    def test_finished_attaches_result_json(self, tmp_path):
        bridge = bridge_for(tmp_path, poll_s=0.01)
        bridge.start()
        write_json(bridge.dir.parent / "result.json", {"status": "ok", "checks": []})
        bridge.finish_from_result({"status": "ok"})
        assert bridge.wait(timeout_s=0.0)["result"] == {"status": "ok", "checks": []}

    def test_finished_falls_back_to_summary_json(self, tmp_path):
        bridge = bridge_for(tmp_path, poll_s=0.01)
        bridge.start()
        write_json(bridge.dir.parent / "summary.json", {"kind": "plan", "status": "ok"})
        bridge.finish()
        assert bridge.wait(timeout_s=0.0)["result"]["kind"] == "plan"

    def test_finished_without_result_gives_null(self, tmp_path):
        bridge = bridge_for(tmp_path, poll_s=0.01)
        bridge.start()
        bridge.finish()
        assert bridge.wait(timeout_s=0.0)["result"] is None

    def test_crashed_also_reports_finished_event(self, tmp_path):
        bridge = bridge_for(tmp_path, poll_s=0.01)
        bridge.start()
        bridge.crashed("boom")
        event = bridge.wait(timeout_s=0.0)
        assert event["event"] == "finished" and event["state"] == "crashed"

    def test_no_event_times_out_as_heartbeat(self, tmp_path):
        bridge = bridge_for(tmp_path, poll_s=0.5)
        bridge.start()
        assert bridge.wait(timeout_s=1.0) == {"ok": True, "event": "heartbeat"}
        assert bridge.ticks["now"] >= 1.0

    def test_two_waiters_are_harmless(self, tmp_path):
        """双 shell 同时 wait 只读：都拿到同一个 ask，谁也不消费谁。"""
        bridge = bridge_for(tmp_path, poll_s=0.01)
        bridge.start()
        write_json(bridge.ask_path(1), {"id": 1, "kind": "text", "payload": {}})
        assert bridge.wait(timeout_s=0.0)["ask"]["id"] == 1
        assert bridge.wait(timeout_s=0.0)["ask"]["id"] == 1


class TestCLIAnswer:
    def test_writes_the_answer_envelope(self, tmp_path):
        bridge = bridge_for(tmp_path, poll_s=0.01)
        bridge.start()
        write_json(bridge.ask_path(1), {"id": 1, "kind": "supervise", "payload": {}})

        result = bridge.answer(1, {"ruling": "HINT", "hint": "先展开高级查询"}, note="看 trace 第 3 行")
        assert result == {"ok": True, "accepted": True, "id": 1}
        envelope = read_json(bridge.ans_path(1))
        assert envelope["id"] == 1 and envelope["note"] == "看 trace 第 3 行"
        assert envelope["answered_by"] == "agent"

    def test_duplicate_answer_is_stale(self, tmp_path):
        """重复答复幂等：第一个落盘生效，后续一律 stale。"""
        bridge = bridge_for(tmp_path, poll_s=0.01)
        bridge.start()
        write_json(bridge.ask_path(1), {"id": 1, "kind": "decide", "payload": {}})
        bridge.answer(1, {"a": 1})
        assert bridge.answer(1, {"a": 2}) == {"ok": True, "accepted": False, "reason": "stale"}
        assert read_json(bridge.ans_path(1))["answer"] == {"a": 1}, "先落盘的那份不被覆盖"

    def test_late_answer_after_finish_is_stale(self, tmp_path):
        bridge = bridge_for(tmp_path, poll_s=0.01)
        bridge.start()
        write_json(bridge.ask_path(1), {"id": 1, "kind": "decide", "payload": {}})
        bridge.finish()
        assert bridge.answer(1, {"a": 1})["reason"] == "stale"

    def test_late_answer_after_crash_is_stale(self, tmp_path):
        bridge = bridge_for(tmp_path, poll_s=0.01)
        bridge.start()
        write_json(bridge.ask_path(1), {"id": 1, "kind": "decide", "payload": {}})
        bridge.crashed("boom")
        assert bridge.answer(1, {"a": 1})["reason"] == "stale"

    def test_unknown_ask_id_is_stale(self, tmp_path):
        bridge = bridge_for(tmp_path, poll_s=0.01)
        bridge.start()
        assert bridge.answer(42, {"a": 1})["reason"] == "stale"
        assert not bridge.ans_path(42).exists()

    def test_missing_bridge_dir_is_stale(self, tmp_path):
        bridge = bridge_for(tmp_path, poll_s=0.01)
        assert bridge.answer(1, {"a": 1})["reason"] == "stale"

    def test_answer_must_be_an_object(self, tmp_path):
        bridge = bridge_for(tmp_path, poll_s=0.01)
        bridge.start()
        with pytest.raises(Exception, match="必须是对象"):
            bridge.answer(1, ["not", "a", "dict"])


class TestEndToEndRoundTrip:
    def test_kernel_and_host_agree_on_one_round(self, tmp_path):
        """一次完整往返：内核写 ask → 宿主 wait 拿到 → answer → 内核拿到答复。"""
        bridge = bridge_for(tmp_path, poll_s=0.01, timeout_s=10)
        bridge.start()
        delivered: dict = {}

        def host(seconds: float) -> None:
            event = bridge.wait(timeout_s=0.0)
            delivered.update(event)
            bridge.answer(event["ask"]["id"], {"text": "sysadmin"})

        bridge._sleep = host
        data, meta = bridge.ask("text", {"field": {"label": "账号"}})

        assert delivered["event"] == "ask" and delivered["kind"] == "text"
        assert data == {"text": "sysadmin"} and meta["ask_id"] == 1


class TestBridgeDirUnwritable:
    def test_unwritable_dir_fails_at_start(self, tmp_path):
        """桥目录建不出来就抛，不许静默降级。"""
        blocker = tmp_path / "blocked"
        blocker.write_text("i am a file", encoding="utf-8")
        bridge = AgentBridge(blocker / "bridge")
        with pytest.raises(Exception, match="桥目录"):
            bridge.start()

    def test_ask_name_helper_matches_written_files(self, tmp_path):
        bridge = bridge_for(tmp_path, poll_s=0.01)
        bridge.start()
        bridge._sleep = lambda s: write_json(bridge.ans_path(1), {"id": 1, "answer": {}})
        bridge.ask("decide", {})
        assert [p.name for p in bridge.dir.glob("ask-*.json")] == [ask_name(1)]
        assert [p.name for p in bridge.dir.glob("ans-*.json")] == [ans_name(1)]
