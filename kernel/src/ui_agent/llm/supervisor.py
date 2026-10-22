"""监督模型缝：Jev 卡住时才调用，一次调用换一条裁决，且裁决必须过 schema 校验。

它和 Jev 看同一份 state（无坐标、无选择器、无 dom_index），区别只在问题与输出：
Jev 选操作 + 目标，监督模型给 HINT/RECOVER/REPLAN/ABORT，落点由代码校验而不是由它保证。

两条实现共用同一个接口（`available` / `new_step` / `ask` / `budget`），便于 `escalate` 鸭子类型：
  Supervisor       —— HTTP 文本模型（平台四档的 `--supervisor on`）
  AgentSupervisor  —— 宿主对话大模型经文件桥（skill 形态的 `--supervisor agent`）
"""

from __future__ import annotations

from ui_agent.bridge.client import AgentBridge
from ui_agent.config import Settings
from ui_agent.decide.jev import to_jev_state
from ui_agent.decide.prompts import SUPERVISOR
from ui_agent.llm.client import chat_json
from ui_agent.schema.state import State
from ui_agent.schema.supervisor import Ruling, SupervisorBudget, parse_ruling


def supervision_context(state: State, goal: str, trigger: str, detail: str,
                        remaining: list[str] | None = None) -> dict:
    return {
        "stuck": {"trigger": trigger, "detail": detail},
        "current_goal": goal,
        "remaining_goals": list(remaining or []),
        "state": to_jev_state(state),
    }


class Supervisor:
    def __init__(self, settings: Settings, budget: SupervisorBudget | None = None):
        self.s = settings
        self.budget = budget or SupervisorBudget(settings.supervisor_per_step, settings.supervisor_per_run)

    @property
    def available(self) -> bool:
        return bool(self.s.text_model_api_key)

    @property
    def unavailable_reason(self) -> str:
        return "缺少 TEXT_MODEL_API_KEY；监督模型不可用"

    def new_step(self) -> None:
        self.budget.new_step()

    def ask(self, state: State, goal: str, trigger: str, detail: str,
            remaining: list[str] | None = None) -> tuple[Ruling, dict]:
        """返回 (通过校验的裁决, 调用元信息)；没有 key、预算用尽、响应不合规都抛 RuntimeError。"""
        if not self.available:
            raise RuntimeError("缺少 TEXT_MODEL_API_KEY；监督模型不可用。")
        if self.budget.exhausted:
            raise RuntimeError(f"监督预算已用完（{self.budget.detail}）；不再咨询监督模型。")
        self.budget.take()
        data, meta = chat_json(self.s, SUPERVISOR, supervision_context(state, goal, trigger, detail, remaining),
                               max_tokens=1200)
        return parse_ruling(data, state), meta


class AgentSupervisor:
    """把监督缝换成「问宿主对话大模型」：同一份 supervision_context 走桥，裁决仍过 parse_ruling。"""

    def __init__(self, settings: Settings, bridge: AgentBridge,
                 budget: SupervisorBudget | None = None):
        self.s = settings
        self.bridge = bridge
        self.budget = budget or SupervisorBudget(settings.supervisor_per_step, settings.supervisor_per_run)

    @property
    def available(self) -> bool:
        # 桥在启动时已校验可写；没有 key 这回事
        return True

    def new_step(self) -> None:
        self.budget.new_step()

    def ask(self, state: State, goal: str, trigger: str, detail: str,
            remaining: list[str] | None = None) -> tuple[Ruling, dict]:
        """桥超时/不可用/裁决不合规一律 RuntimeError → escalate 转 unavailable → M0 停机。"""
        if self.budget.exhausted:
            raise RuntimeError(f"监督预算已用完（{self.budget.detail}）；不再咨询监督模型。")
        self.budget.take()
        payload = supervision_context(state, goal, trigger, detail, remaining)
        data, meta = self.bridge.ask("supervise", payload)
        return parse_ruling(data, state), meta


class NoBridgeSupervisor:
    """默认档：监督缝留给宿主，但这次没装桥（没给 --run-dir 或没装配 AgentBridge）。

    不抛异常、也不悄悄退回 HTTP：jev 卡住时由 escalate 按 M0 停机语义收尾，
    trace 里如实写「没有宿主监督通道」，不把一次运行拖崩。
    """

    def __init__(self, settings: Settings):
        self.s = settings
        self.budget = SupervisorBudget(settings.supervisor_per_step, settings.supervisor_per_run)

    @property
    def available(self) -> bool:
        return False

    @property
    def unavailable_reason(self) -> str:
        return "没有可用的宿主监督通道（没给 --run-dir 或未装配 Agent 桥）；jev 卡住时按 M0 停机"

    def new_step(self) -> None:
        self.budget.new_step()

    def ask(self, state: State, goal: str, trigger: str, detail: str,
            remaining: list[str] | None = None) -> tuple[Ruling, dict]:
        raise RuntimeError(self.unavailable_reason)


def make_supervisor(settings: Settings, bridge: AgentBridge | None = None):
    """总闸优先：ui_agent_supervisor=False 时一律 None，忽略 mode。

    mode 默认 agent（问宿主）：没装配桥时给 NoBridgeSupervisor 而不是抛错——
    裸 CLI 跑 `ui-agent run` 不该因为没有宿主就起不来。
    """
    if not settings.ui_agent_supervisor:
        return None
    if settings.ui_agent_supervisor_mode == "agent":
        return AgentSupervisor(settings, bridge) if bridge is not None else NoBridgeSupervisor(settings)
    return Supervisor(settings)
