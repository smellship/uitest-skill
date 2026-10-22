"""AgentSupervisor：合法裁决过 parse_ruling、坏裁决 → RuntimeError、预算扣减、new_step、available 恒 True。"""

from __future__ import annotations

import pytest
from fakes import FakeBridge, snapshot

from ui_agent.bridge.protocol import BridgeTimeout
from ui_agent.config import Settings
from ui_agent.llm.supervisor import (
    AgentSupervisor,
    NoBridgeSupervisor,
    Supervisor,
    make_supervisor,
)
from ui_agent.observe.snapshot import build_state
from ui_agent.schema.supervisor import parse_ruling


def state_of(raw: dict):
    return build_state(raw, [], {}, 250, 6000)


def supervisor_for(bridge: FakeBridge, **overrides) -> AgentSupervisor:
    return AgentSupervisor(Settings(**overrides), bridge)


class TestInterfaceParity:
    def test_available_is_always_true(self):
        sup = supervisor_for(FakeBridge())
        assert sup.available is True, "桥在启动时已校验过可写；没有 key 这回事"

    def test_has_the_same_budget_shape_as_the_http_supervisor(self):
        sup = supervisor_for(FakeBridge(), supervisor_per_step=3, supervisor_per_run=9)
        assert sup.budget.per_step == 3 and sup.budget.per_run == 9

    def test_new_step_resets_the_per_step_counter(self):
        sup = supervisor_for(FakeBridge(), supervisor_per_step=2)
        sup.budget.take()
        sup.budget.take()
        assert sup.budget.exhausted
        sup.new_step()
        assert not sup.budget.exhausted, "换步后每步预算重新计算"


class TestRulings:
    def test_valid_hint_passes_parse_ruling(self):
        bridge = FakeBridge({"supervise": [{"ruling": "HINT", "hint": "先展开高级查询再选行政区"}]})
        ruling, meta = supervisor_for(bridge).ask(state_of(snapshot("a")), "查询", "blocked",
                                                  "决策模型判定无法推进")
        assert ruling.ruling == "HINT" and "高级查询" in ruling.hint
        assert meta["via"] == "agent"

    def test_payload_matches_the_http_supervision_context(self):
        bridge = FakeBridge({"supervise": [{"ruling": "HINT", "hint": "x"}]})
        supervisor_for(bridge).ask(state_of(snapshot("a")), "查询", "blocked", "细节",
                                   remaining=["第二步"])
        payload = bridge.asks[0]["payload"]
        assert payload["stuck"] == {"trigger": "blocked", "detail": "细节"}
        assert payload["current_goal"] == "查询" and payload["remaining_goals"] == ["第二步"]
        assert "state" in payload

    def test_attachments_stay_empty_in_v1(self):
        """v1 监督只看文本：截图永不进模型输入；payload.attachments 恒空（未来留口）。"""
        bridge = FakeBridge({"supervise": [{"ruling": "HINT", "hint": "x"}]})
        supervisor_for(bridge).ask(state_of(snapshot("a")), "查询", "blocked", "细节")
        assert "attachments" not in bridge.asks[0]["payload"]

    def test_recover_ruling_is_validated_against_the_snapshot(self):
        bridge = FakeBridge({"supervise": [{"ruling": "RECOVER", "reason": "点查询",
                                            "action": {"operation": "CLICK", "element": 1}}]})
        ruling, _ = supervisor_for(bridge).ask(state_of(snapshot("a", labels=["查询"])),
                                               "查询", "stuck", "细节")
        assert ruling.ruling == "RECOVER" and ruling.element == 1

    def test_recover_to_an_index_not_in_the_snapshot_is_rejected(self):
        bridge = FakeBridge({"supervise": [{"ruling": "RECOVER", "reason": "点它",
                                            "action": {"operation": "CLICK", "element": 99}}]})
        with pytest.raises(RuntimeError, match="不在当次快照里"):
            supervisor_for(bridge).ask(state_of(snapshot("a")), "查询", "stuck", "细节")


class TestFailures:
    def test_unknown_ruling_becomes_runtime_error(self):
        """答歪 = 不可用：escalate 会把它转成 unavailable → M0 停机。"""
        bridge = FakeBridge({"supervise": [{"ruling": "TELEPORT"}]})
        with pytest.raises(RuntimeError, match="不在"):
            supervisor_for(bridge).ask(state_of(snapshot("a")), "查询", "blocked", "细节")

    def test_hint_without_text_is_rejected(self):
        bridge = FakeBridge({"supervise": [{"ruling": "HINT"}]})
        with pytest.raises(RuntimeError, match="没给 hint"):
            supervisor_for(bridge).ask(state_of(snapshot("a")), "查询", "blocked", "细节")

    def test_bridge_timeout_propagates(self):
        bridge = FakeBridge({"supervise": []})
        with pytest.raises(BridgeTimeout, match="bridge timeout"):
            supervisor_for(bridge).ask(state_of(snapshot("a")), "查询", "blocked", "细节")

    def test_exhausted_budget_refuses_before_asking(self):
        bridge = FakeBridge({"supervise": [{"ruling": "HINT", "hint": "x"},
                                           {"ruling": "HINT", "hint": "y"}]})
        sup = supervisor_for(bridge, supervisor_per_step=1, supervisor_per_run=5)
        sup.ask(state_of(snapshot("a")), "查询", "blocked", "细节")
        with pytest.raises(RuntimeError, match="监督预算已用完"):
            sup.ask(state_of(snapshot("a")), "查询", "blocked", "细节")
        assert len(bridge.asks) == 1, "预算用尽就不再问桥"


class TestBudget:
    def test_budget_is_taken_before_asking(self):
        """扣费在调用前：模型答歪也不退，升级次数有限 ⇒ 循环必然终止。"""
        bridge = FakeBridge({"supervise": [{"ruling": "TELEPORT"},
                                           {"ruling": "TELEPORT"}]})
        sup = supervisor_for(bridge, supervisor_per_step=2, supervisor_per_run=6)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                sup.ask(state_of(snapshot("a")), "查询", "blocked", "细节")
        assert sup.budget.exhausted, "两次答歪把每步预算用光"

    def test_run_budget_spans_steps(self):
        bridge = FakeBridge({"supervise": [{"ruling": "HINT", "hint": str(i)} for i in range(3)]})
        sup = supervisor_for(bridge, supervisor_per_step=5, supervisor_per_run=2)
        sup.ask(state_of(snapshot("a")), "查询", "blocked", "细节")
        sup.new_step()
        sup.ask(state_of(snapshot("a")), "查询", "blocked", "细节")
        sup.new_step()
        assert sup.budget.exhausted, "整轮预算不因换步重置"


class TestFactory:
    def test_agent_mode_without_bridge_degrades_instead_of_crashing(self):
        """默认档就是 agent：裸 CLI 没装桥不该起不来，退化成"不可用"按 M0 停机。"""
        sup = make_supervisor(Settings(ui_agent_supervisor_mode="agent"))
        assert isinstance(sup, NoBridgeSupervisor)
        assert sup.available is False
        assert "宿主监督通道" in sup.unavailable_reason

    def test_agent_mode_returns_the_bridge_supervisor(self):
        bridge = FakeBridge()
        sup = make_supervisor(Settings(ui_agent_supervisor_mode="agent"), bridge=bridge)
        assert isinstance(sup, AgentSupervisor) and sup.bridge is bridge

    def test_model_mode_still_returns_the_http_supervisor(self):
        sup = make_supervisor(Settings(ui_agent_supervisor_mode="model"))
        assert isinstance(sup, Supervisor), "HTTP 双轨不许变"

    def test_master_switch_wins_over_the_mode(self):
        """总闸关着就一律 None，忽略 mode（即便配了 agent 也不装配桥监督）。"""
        sup = make_supervisor(Settings(ui_agent_supervisor=False,
                                       ui_agent_supervisor_mode="agent"), bridge=FakeBridge())
        assert sup is None


class TestParseRulingIsShared:
    def test_the_same_ruling_parses_identically_either_way(self):
        """两条缝共用 parse_ruling：合法裁决的解析结果必须一致。"""
        state = state_of(snapshot("a", labels=["查询"]))
        data = {"ruling": "RECOVER", "reason": "点查询",
                "action": {"operation": "CLICK", "element": 1}}
        assert parse_ruling(data, state) == parse_ruling(data, state)
