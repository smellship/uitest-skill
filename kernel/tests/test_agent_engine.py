"""AgentEngine：合法答复 → Decision（model=agent）、缺 answers → ValueError、
{"error"} → AbortDecision、超时 → BridgeTimeout、坏答复 → ValueError、绝不碰 HTTP。"""

from __future__ import annotations

import pytest
from fakes import FakeBridge, sealed_answers, snapshot

from ui_agent.bridge.protocol import BridgeTimeout, BridgeUnavailable
from ui_agent.config import Settings
from ui_agent.decide import agent_engine as agent_engine_mod
from ui_agent.decide import delegate as delegate_mod
from ui_agent.decide.agent_engine import AgentEngine
from ui_agent.decide.delegate import AbortDecision
from ui_agent.observe.snapshot import build_state


def state_of(raw: dict):
    return build_state(raw, [], {}, 250, 6000)


def engine_with(bridge: FakeBridge) -> AgentEngine:
    return AgentEngine(Settings(ui_agent_decider="agent"), bridge)


class TestValidAnswers:
    def test_valid_answers_become_a_decision(self):
        bridge = FakeBridge()
        engine = engine_with(bridge)
        state = state_of(snapshot("a", labels=["查询"]))

        def reply(kind: str, payload: dict) -> tuple[dict, dict]:
            answers = sealed_answers(payload["questions"], "CLICK", "1")
            return {"answers": answers}, {"via": "agent", "ask_id": 1, "latency_ms": 7}

        bridge.ask = reply  # type: ignore[method-assign]
        decision = engine.decide(state, "点查询")

        assert decision.operation == "CLICK" and decision.target_element == 1
        assert decision.model == "agent", "决策来源要标 agent，trace 的 via 靠它"
        assert decision.latency_ms == 7

    def test_payload_is_isomorphic_to_the_jev_request(self):
        bridge = FakeBridge()
        engine = engine_with(bridge)
        seen: dict = {}

        def reply(kind: str, payload: dict) -> tuple[dict, dict]:
            seen.update(payload)
            return {"answers": sealed_answers(payload["questions"], "CLICK", "1")}, {}

        bridge.ask = reply  # type: ignore[method-assign]
        engine.decide(state_of(snapshot("a", labels=["查询"])), "点查询")

        assert set(seen) == {"state", "questions"}, "与 Jev 请求同构（没有 previous_error 时不带它）"
        assert seen["state"]["elements"][0]["label"] == "查询"
        assert "click_target" in seen["questions"]
        assert bridge.asks == [], "自定义 ask 覆盖了记录，这里只验 payload 形状"

    def test_error_context_goes_in_as_previous_error(self):
        bridge = FakeBridge()
        engine = engine_with(bridge)
        seen: dict = {}

        def reply(kind: str, payload: dict) -> tuple[dict, dict]:
            seen.update(payload)
            return {"answers": sealed_answers(payload["questions"], "CLICK", "1")}, {}

        bridge.ask = reply  # type: ignore[method-assign]
        engine.decide(state_of(snapshot("a", labels=["查询"])), "点查询",
                      error_context="ValueError: 上一轮答歪了")

        assert seen["previous_error"] == "ValueError: 上一轮答歪了"


class TestFailures:
    def test_missing_answers_is_a_value_error(self):
        bridge = FakeBridge({"decide": [{}]})
        with pytest.raises(ValueError, match="answers"):
            engine_with(bridge).decide(state_of(snapshot("a")), "目标")

    def test_answers_must_be_an_object(self):
        bridge = FakeBridge({"decide": [{"answers": ["not", "a", "dict"]}]})
        with pytest.raises(ValueError, match="answers"):
            engine_with(bridge).decide(state_of(snapshot("a")), "目标")

    def test_error_field_becomes_abort_decision(self):
        bridge = FakeBridge({"decide": [{"error": "页面上没有目标元素"}]})
        with pytest.raises(AbortDecision, match="页面上没有目标元素"):
            engine_with(bridge).decide(state_of(snapshot("a")), "目标")

    def test_blank_error_string_is_not_an_abort(self):
        bridge = FakeBridge({"decide": [{"error": "   ", "answers": {"operation": {}}}]})
        with pytest.raises(ValueError, match="未通过校验"):
            engine_with(bridge).decide(state_of(snapshot("a")), "目标")

    def test_timeout_propagates_as_bridge_timeout(self):
        """超时是硬停：不许被转成可重试的 ValueError。"""
        bridge = FakeBridge({"decide": []})
        with pytest.raises(BridgeTimeout, match="bridge timeout"):
            engine_with(bridge).decide(state_of(snapshot("a")), "目标")

    def test_unavailable_bridge_becomes_value_error(self):
        """首问即坏答复（重问由 runner 负责）：桥不可用要能被 runner 当作校验错重问。"""
        bridge = FakeBridge({"decide": [BridgeUnavailable("答复文件无法解析")]})
        with pytest.raises(ValueError, match="Agent 桥答复不可用"):
            engine_with(bridge).decide(state_of(snapshot("a")), "目标")

    def test_bad_answers_go_through_the_same_validation_as_jev(self):
        bridge = FakeBridge({"decide": [{"answers": {
            "operation": {"choice": "FLY", "probabilities": {"CLICK": 1.0}, "confidence": 1.0}}}]})
        with pytest.raises(ValueError, match="未通过校验"):
            engine_with(bridge).decide(state_of(snapshot("a")), "目标")


class TestNoHttp:
    def test_never_touches_the_text_model_transport(self, monkeypatch):
        """agent 引擎不看 TEXT_MODEL_API_KEY，也不会发任何 HTTP 请求。"""
        def boom(*args, **kwargs):
            raise AssertionError("agent 引擎不该调用 HTTP 文本模型")

        monkeypatch.setattr(delegate_mod, "chat_json", boom)
        monkeypatch.setattr(agent_engine_mod, "build_questions",
                            agent_engine_mod.build_questions)  # 保持原型，只为可读

        bridge = FakeBridge()
        engine = AgentEngine(Settings(ui_agent_decider="agent", text_model_api_key=""),
                             bridge)

        def reply(kind: str, payload: dict) -> tuple[dict, dict]:
            return {"answers": sealed_answers(payload["questions"], "DONE")}, {}

        bridge.ask = reply  # type: ignore[method-assign]
        decision = engine.decide(state_of(snapshot("a")), "目标")
        assert decision.operation == "DONE"

    def test_no_text_model_key_is_required(self):
        bridge = FakeBridge()

        def reply(kind: str, payload: dict) -> tuple[dict, dict]:
            return {"answers": sealed_answers(payload["questions"], "DONE")}, {}

        bridge.ask = reply  # type: ignore[method-assign]
        engine = AgentEngine(Settings(ui_agent_decider="agent", text_model_api_key=""), bridge)
        assert engine.decide(state_of(snapshot("a")), "目标").operation == "DONE"

    def test_decide_does_not_write_attachments_in_v1(self):
        bridge = FakeBridge()
        seen: dict = {}

        def reply(kind: str, payload: dict) -> tuple[dict, dict]:
            seen.update(payload)
            return {"answers": sealed_answers(payload["questions"], "DONE")}, {}

        bridge.ask = reply  # type: ignore[method-assign]
        engine_with(bridge).decide(state_of(snapshot("a")), "目标")
        assert "attachments" not in seen, "v1 监督只看文本；decide 也不需要附附件"
