"""Runner 的 agent 接线：全链路（桥决策 + 桥监督）、无 Phase 2、AbortDecision 立即 aborted、
decide_invalid 升级、text source=agent 进录像、trace 的 via 口径。全部离线（FakeDriver + FakeBridge）。
"""

from __future__ import annotations

import pytest
from fakes import (
    FakeBridge,
    FakeDriver,
    read_rows,
    read_trace,
    sealed_answers,
    settings_for,
    snapshot,
)

from ui_agent.bridge.protocol import BridgeTimeout
from ui_agent.llm import textfill as textfill_mod
from ui_agent.run.runner import Runner
from ui_agent.verify.asserts import parse_check

CHECKS = ["text_contains=查询完成"]


def agent_settings(tmp_path, **overrides):
    base = {"ui_agent_decider": "agent", "ui_agent_supervisor": True,
            "ui_agent_supervisor_mode": "agent"}
    return settings_for(tmp_path, **{**base, **overrides})


class ScriptedAgent:
    """按 kind 脚本答复的假宿主：decide 的回包自己按 questions 造合法 answers。"""

    def __init__(self, ops: list[tuple[str, str | None]], text: str = "sysadmin",
                 supervise: list[dict] | None = None):
        self.ops = list(ops)
        self.text = text
        self.supervise = list(supervise or [])
        self.bridge = FakeBridge()
        self.bridge.ask = self._ask  # type: ignore[method-assign]

    def _ask(self, kind: str, payload: dict) -> tuple[dict, dict]:
        self.bridge.asks.append({"kind": kind, "payload": payload})
        meta = {"via": "agent", "ask_id": len(self.bridge.asks), "latency_ms": 4}
        if kind == "decide":
            operation, target = self.ops.pop(0)
            return {"answers": sealed_answers(payload["questions"], operation, target)}, meta
        if kind == "text":
            return {"text": self.text}, meta
        if kind == "supervise":
            if not self.supervise:
                raise BridgeTimeout("bridge timeout: kind=supervise id=1 after 180.0s")
            return self.supervise.pop(0), meta
        raise AssertionError(f"未知的 ask kind：{kind}")

    def answers_with(self, factory):
        """用自定义的「答复正文」工厂替换 decide/text/supervise 的处理（仍记录 ask）。

        factory 返回正文 dict；返回异常实例则原样抛出。**只记录一次** ask。
        """
        def ask(kind: str, payload: dict) -> tuple[dict, dict]:
            self.bridge.asks.append({"kind": kind, "payload": payload})
            body = factory(kind, payload)
            if isinstance(body, Exception):
                raise body
            return body, {"via": "agent", "ask_id": len(self.bridge.asks), "latency_ms": 4}

        return ask


def run_agent(tmp_path, snapshots, agent: ScriptedAgent, checks=CHECKS, **overrides):
    settings = agent_settings(tmp_path, **overrides)
    driver = FakeDriver(snapshots)
    runner = Runner(settings, "查询", "http://x/", checks=[parse_check(c) for c in checks],
                    task="t", driver=driver, bridge=agent.bridge, verbose=False)
    return runner.run()


class TestAgentDecisionLoop:
    def test_full_chain_ok_with_agent_decisions(self, tmp_path):
        agent = ScriptedAgent([("CLICK", "1"), ("DONE", None)])
        result = run_agent(tmp_path, [snapshot("a"), snapshot("b", text="查询完成"),
                                      snapshot("b", text="查询完成")], agent)

        assert result.status == "ok"
        trace = read_trace(result.run_dir)
        assert [row["operation"] for row in trace] == ["CLICK", "DONE"]
        assert {row["model"] for row in trace} == {"agent"}
        assert {row["via"] for row in trace} == {"agent"}, "决策行要标 via=agent"

    def test_run_start_records_bridge_config(self, tmp_path):
        agent = ScriptedAgent([("DONE", None)])
        result = run_agent(tmp_path, [snapshot("a", text="查询完成")], agent)

        start = read_rows(result.run_dir)[0]
        assert start["decider"] == "agent"
        assert start["supervisor_mode"] == "agent"
        assert start["bridge_dir"] and start["bridge_timeout_s"] == 180.0

    def test_result_json_carries_supervisor_mode(self, tmp_path):
        import json
        agent = ScriptedAgent([("DONE", None)])
        result = run_agent(tmp_path, [snapshot("a", text="查询完成")], agent)
        data = json.loads((result.run_dir / "result.json").read_text(encoding="utf-8"))
        assert data["decider"] == "agent" and data["supervisor_mode"] == "agent"


class TestAbortDecision:
    def test_abort_returns_immediately_without_retry(self, tmp_path):
        """模型主动 {"error"}：立即 aborted，绝不当成可重试异常再问一次。"""
        agent = ScriptedAgent([])
        agent.ops = []
        agent.bridge.ask = agent.answers_with(lambda kind, payload: {"error": "页面上没有目标元素"})
        result = run_agent(tmp_path, [snapshot("a")], agent)

        assert result.status == "aborted" and "页面上没有目标元素" in result.detail
        kinds = [ask["kind"] for ask in agent.bridge.asks]
        assert kinds == ["decide"], "中止只问一次，不再重问"

    def test_abort_in_phase1_is_not_treated_as_retryable(self, tmp_path):
        """AbortDecision 不是 ValueError 的子类：绝不能被 Phase 1 的重问吞掉。"""
        from ui_agent.decide.delegate import AbortDecision
        assert not issubclass(AbortDecision, ValueError)


class TestNoPhase2:
    def test_agent_engine_never_falls_back_to_the_http_thinking_path(self, tmp_path, monkeypatch):
        """Phase 1 两次都答歪 → 直接 decide_invalid 升级，不构造 LlmEngine。"""
        def boom(*args, **kwargs):
            raise AssertionError("agent 模式不该调用 HTTP 文本模型（Phase 2 必须跳过）")

        monkeypatch.setattr(textfill_mod, "chat_json", boom)

        agent = ScriptedAgent([])
        agent.bridge.ask = agent.answers_with(lambda kind, payload: {
            "answers": {"operation": {"choice": "FLY", "probabilities": {"CLICK": 1.0},
                                      "confidence": 1.0}}})
        result = run_agent(tmp_path, [snapshot("a")], agent,
                           ui_agent_supervisor=False)

        assert result.status in ("error", "aborted")
        assert "两次决策均不合规" in result.detail

    def test_decide_invalid_escalates_to_the_agent_supervisor(self, tmp_path):
        """两次答歪 → 一次 supervise ask；监督给 HINT 后继续跑。"""
        agent = ScriptedAgent([("DONE", None)],
                              supervise=[{"ruling": "HINT", "hint": "别急，先看页面"}])
        bad = {"answers": {"operation": {"choice": "FLY", "probabilities": {"CLICK": 1.0},
                                         "confidence": 1.0}}}
        calls = {"n": 0}
        real = agent._ask

        def factory(kind: str, payload: dict):
            if kind != "decide":
                return real(kind, payload)[0]
            calls["n"] += 1
            if calls["n"] <= 2:
                return bad          # 前两次答歪（Phase 1 用满两次）
            return real(kind, payload)[0]

        agent.bridge.ask = agent.answers_with(factory)
        result = run_agent(tmp_path, [snapshot("a"), snapshot("a", text="查询完成")], agent)

        # real() 自己也会记一笔，故这里只数「答歪之后仍然继续」这件事
        kinds = [ask["kind"] for ask in agent.bridge.asks]
        assert "supervise" in kinds, "两次答歪后要升级到 agent 监督"
        assert result.status == "ok", "HINT 之后继续跑到断言通过"

    def test_no_supervisor_raises_instead_of_silently_continuing(self, tmp_path):
        agent = ScriptedAgent([])
        agent.bridge.ask = agent.answers_with(lambda kind, payload: {
            "answers": {"operation": {"choice": "FLY", "probabilities": {"CLICK": 1.0},
                                      "confidence": 1.0}}})
        result = run_agent(tmp_path, [snapshot("a")], agent, ui_agent_supervisor=False)

        assert result.status == "error" and "两次决策均不合规" in result.detail


def text_field_snapshot(fingerprint: str, text: str = "请输入帐户名") -> dict:
    """带可输入文本框的快照：TYPE_TEXT 的目标候选只在 operations 里有时才存在。"""
    return {"page": {"url": "http://x/", "title": "T", "text": text, "viewport": {}, "scroll": {}},
            "elements": [{"index": 1, "role": "textbox", "label": "账号", "rect": [0, 0, 10, 10],
                          "in_viewport": True, "occluded_by": "", "disabled": False,
                          "operations": ["CLICK", "TYPE_TEXT"]}],
            "fingerprint": fingerprint, "omitted": 0}


class TestTextRecording:
    def test_agent_text_values_are_recorded_for_zero_model_replay(self, tmp_path):
        """agent 给的 TYPE_TEXT 值必须进录像，否则零模型回放缺字段值。"""
        from ui_agent.replay.cache import CacheStore, case_identity
        from ui_agent.replay.session import open_session

        settings = agent_settings(tmp_path, ui_agent_cache=True)
        session = open_session(settings, case_hash="probe", base_url="http://x/", plan_name="t")
        driver = FakeDriver([text_field_snapshot("a"), snapshot("b", text="查询完成"),
                             snapshot("b", text="查询完成")])
        agent = ScriptedAgent([("TYPE_TEXT", "1"), ("DONE", None)], text="sysadmin")
        runner = Runner(settings, "登录", "http://x/",
                        checks=[parse_check(CHECKS[0])], task="t", driver=driver,
                        bridge=agent.bridge, verbose=False, cache=session)
        result = runner.run()
        session.run_id = result.run_dir.name
        session.finalize()

        assert result.status == "ok"
        trace = read_trace(result.run_dir)
        typed = next(row for row in trace if row["operation"] == "TYPE_TEXT")
        assert typed["text_model"]["source"] == "agent"

        entry = CacheStore(settings.cache_dir).load(session.entry.key)
        assert any(rec.text == "sysadmin" for rec in entry.records), "agent 值要落进录像"
        _ = case_identity

    def test_var_values_are_not_recorded(self, tmp_path):
        from ui_agent.replay.cache import CacheStore
        from ui_agent.replay.session import open_session

        settings = agent_settings(tmp_path, ui_agent_cache=True)
        session = open_session(settings, case_hash="probe2", base_url="http://x/", plan_name="t")
        driver = FakeDriver([text_field_snapshot("a"), snapshot("b", text="查询完成"),
                             snapshot("b", text="查询完成")])
        agent = ScriptedAgent([("TYPE_TEXT", "1"), ("DONE", None)])
        runner = Runner(settings, "登录", "http://x/", checks=[parse_check(CHECKS[0])],
                        task="t", driver=driver, bridge=agent.bridge, verbose=False,
                        vars={"账号": "sysadmin"}, cache=session)
        result = runner.run()
        session.run_id = result.run_dir.name
        session.finalize()

        trace = read_trace(result.run_dir)
        typed = next(row for row in trace if row["operation"] == "TYPE_TEXT")
        assert typed["text_model"]["source"] == "var"
        entry = CacheStore(settings.cache_dir).load(session.entry.key)
        assert all(rec.text == "" for rec in entry.records), "--var 的值不落盘"


class TestSupervisionVerdicts:
    def test_recover_ruling_runs_through_the_normal_execution_path(self, tmp_path):
        agent = ScriptedAgent([("BLOCKED", None), ("DONE", None)],
                              supervise=[{"ruling": "RECOVER", "reason": "点查询",
                                          "action": {"operation": "CLICK", "element": 1}}])
        result = run_agent(tmp_path, [snapshot("a", labels=["查询"]),
                                      snapshot("a", labels=["查询"]),
                                      snapshot("b", text="查询完成")], agent)

        assert result.status == "ok"
        trace = read_trace(result.run_dir)
        assert any(row["supervisor"] and row["supervisor"]["ruling"] == "RECOVER" for row in trace)
        assert any(row["operation"] == "CLICK" and row["model"] == "supervisor" for row in trace)

    def test_supervisor_unavailable_stops_like_m0(self, tmp_path):
        """桥超时 → unavailable → 与「没有监督模型」完全一致的停机结果。"""
        agent = ScriptedAgent([("BLOCKED", None)], supervise=[])
        result = run_agent(tmp_path, [snapshot("a")], agent)

        trace = read_trace(result.run_dir)
        verdict = next(row["supervisor"] for row in trace if row["supervisor"])
        assert "bridge timeout" in verdict["error"]
        assert result.status == "blocked", \
            "监督不可用时按 M0 语义停机：BLOCKED 就是 blocked，不该变成 error"

    def test_supervisor_row_carries_via_agent(self, tmp_path):
        agent = ScriptedAgent([("BLOCKED", None), ("DONE", None)],
                              supervise=[{"ruling": "HINT", "hint": "先看页面"}])
        result = run_agent(tmp_path, [snapshot("a"), snapshot("a", text="查询完成")], agent)
        trace = read_trace(result.run_dir)
        verdict = next(row["supervisor"] for row in trace if row["supervisor"])
        assert verdict["meta"]["via"] == "agent"


class TestAssemblyGuards:
    def test_missing_bridge_raises_at_assembly_time(self, tmp_path):
        """缺桥是装配错误：工厂直接抛，不要等到 run() 里才炸。"""
        settings = agent_settings(tmp_path)
        driver = FakeDriver([snapshot("a")])
        with pytest.raises(RuntimeError, match="需要 Agent 桥"):
            Runner(settings, "查询", "http://x/", checks=[], task="t", driver=driver,
                   verbose=False)

    def test_supervisor_mode_agent_without_bridge_also_raises(self, tmp_path):
        settings = agent_settings(tmp_path, ui_agent_decider="jev")
        driver = FakeDriver([snapshot("a")])
        with pytest.raises(RuntimeError, match="需要 Agent 桥"):
            Runner(settings, "查询", "http://x/", checks=[], task="t", driver=driver,
                   verbose=False)

    def test_off_decider_requires_no_bridge(self, tmp_path):
        settings = settings_for(tmp_path, ui_agent_decider="off", ui_agent_supervisor=False,
                                ui_agent_cache=False)
        driver = FakeDriver([snapshot("a")])
        runner = Runner(settings, "目标", "http://x/", checks=[], task="t", driver=driver,
                        verbose=False)
        assert runner.run().status == "error"   # OffEngine 抛"已关闭"，与桥无关


class TestBridgeTimeoutIsHardStop:
    def test_decide_timeout_ends_the_run_with_the_bridge_wording(self, tmp_path):
        agent = ScriptedAgent([])
        agent.bridge.ask = lambda kind, payload: (_ for _ in ()).throw(  # type: ignore[method-assign]
            BridgeTimeout("bridge timeout: kind=decide id=1 after 180.0s"))
        result = run_agent(tmp_path, [snapshot("a")], agent)

        assert result.status == "error"
        assert "bridge timeout" in result.detail
