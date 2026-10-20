"""宿主对话大模型代班 Jev：入参/出参与 Jev 请求同构（state + questions → answers），只换答题人。

与 `LlmEngine` 的区别只有三点：
1. **不走 HTTP、不看 TEXT_MODEL_API_KEY**——问题写成 ask 文件，等宿主写回 ans；
2. **不做 Phase 2**——对话模型没有"快/慢路径"之分，runner 两次失败后直接走 decide_invalid 升级；
3. `model="agent"`，落点校验仍与 Jev 完全共用（`assemble_decision`），答歪了照样拒。

答复仍是模型输出：`{"error": "..."}` 是主动中止，`{"answers": {...}}` 才是决策。
"""

from __future__ import annotations

from ui_agent.bridge.client import AgentBridge
from ui_agent.bridge.protocol import BridgeError, BridgeTimeout
from ui_agent.config import Settings
from ui_agent.decide.delegate import AbortDecision
from ui_agent.decide.jev import assemble_decision, build_questions, to_jev_state
from ui_agent.schema.decision import Decision
from ui_agent.schema.state import State


class AgentEngine:
    def __init__(self, settings: Settings, bridge: AgentBridge):
        self.s = settings
        self.bridge = bridge

    def decide(self, state: State, goal: str, rules: list[str] | None = None,
               error_context: str | None = None) -> Decision:
        questions, _ = build_questions(state, goal, rules)
        payload: dict = {"state": to_jev_state(state), "questions": questions}
        if error_context:
            payload["previous_error"] = error_context
        try:
            data, meta = self.bridge.ask("decide", payload)
        except BridgeTimeout:
            # 到点没人答：硬停（runner 不当成可重试的校验错）
            raise
        except BridgeError as exc:
            # 坏答复 / id 不匹配 → 转成校验错，runner 会带 previous_error 重问一次
            raise ValueError(f"Agent 桥答复不可用：{exc}") from exc
        if isinstance(data.get("error"), str) and data["error"].strip():
            raise AbortDecision(data["error"].strip())
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise ValueError("agent 未返回 answers 对象；未执行任何动作。")
        return assemble_decision(state, questions, answers,
                                 model="agent", usage=meta.get("usage", {}),
                                 latency_ms=int(meta.get("latency_ms", 0)))
