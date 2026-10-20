"""TYPE_TEXT 取值的唯一来源：显式声明的测试数据（vars）优先，其次小模型或宿主 agent；都没有就拒填。

三条路（优先级从高到低）：
1. `--var` 命中 → 原样使用，值不落盘、不进模型、不落桥（人给的测试数据）。
2. `supervisor_mode=agent` → 走文件桥问宿主对话大模型（kind=text）；secret 字段一律拒绝。
3. 否则 → HTTP 文本模型（平台四档的 `--supervisor on`，行为完全不变）。

无论走哪条，答复都要过同一套校验（非空、≤2000、无多余键）；答歪就不输入。
"""

from __future__ import annotations

from ui_agent.bridge.client import AgentBridge
from ui_agent.bridge.protocol import BridgeError
from ui_agent.config import Settings
from ui_agent.decide.prompts import TEXT_VALUE
from ui_agent.llm.client import chat_json
from ui_agent.schema.state import Element, State

MAX_TEXT = 2000


def field_context(goal: str, element: Element, state: State) -> dict:
    return {
        "goal": goal,
        "field": {"label": element.label, "role": element.role, "current_value": element.value},
        "page": {"url": state.page.url, "title": state.page.title, "text": state.page.text},
        "recent_actions": [a.model_dump(exclude_none=True) for a in state.recent_actions[-6:]],
    }


def match_var(vars: dict[str, str] | None, label: str) -> tuple[str, str] | None:
    """控件标签包含某个键就用它（取最长的键，避免"账号"抢"确认账号"）。"""
    if not vars:
        return None
    needle = label.strip().lower()
    hits = [(key, value) for key, value in vars.items() if key.strip() and key.strip().lower() in needle]
    if not hits:
        return None
    key, value = max(hits, key=lambda item: len(item[0]))
    return key, value


def _unmatched_vars_hint(vars: dict[str, str] | None, label: str) -> str:
    """没被用上的 --var 是这类失败的头号嫌疑：把键名和控件标签摆在一起给人看。"""
    keys = [str(key) for key in (vars or {}) if str(key).strip()]
    if not keys:
        return ""
    return f"控件标签「{label}」不含任何现有 --var 键（{'、'.join(keys)}）——键名需为控件标签的子串"


def _bad_value(suffix: str, who: str = "文本模型") -> RuntimeError:
    return RuntimeError(f"{who}未给出可用的字段值（可能缺少依据）{suffix}；未执行任何输入。")


def resolve_text(settings: Settings, goal: str, element: Element, state: State,
                 vars: dict[str, str] | None = None,
                 bridge: AgentBridge | None = None) -> tuple[str, dict]:
    """vars 是人给的测试数据，不进任何模型请求；没有匹配才回落到文本模型或桥。"""
    hit = match_var(vars, element.label)
    if hit is not None:
        key, value = hit
        return value, {"source": "var", "key": key, "model": "-", "latency_ms": 0}

    hint = _unmatched_vars_hint(vars, element.label)
    suffix = f"；{hint}" if hint else ""
    if not settings.ui_agent_supervisor:
        raise RuntimeError(
            f"大模型总闸已关（UI_AGENT_SUPERVISOR=false）：没有匹配 --var 的字段值{suffix}；未执行任何输入。")

    if settings.ui_agent_supervisor_mode == "agent":
        # secret 字段绝不经桥：密码等只能由 --var 提供，agent 也看不到值
        if element.secret:
            raise RuntimeError(
                f"敏感字段「{element.label}」只能通过 --var 提供；未向 agent 询问{suffix}。")
        if bridge is None:
            raise RuntimeError("supervisor=agent 需要 Agent 桥，但未装配 bridge。")
        payload = {**field_context(goal, element, state), "hint": hint, "attachments": []}
        try:
            data, meta = bridge.ask("text", payload)
        except BridgeError as exc:
            raise RuntimeError(f"Agent 桥未给出字段值（{exc}）{suffix}；未执行任何输入。") from exc
        value = data.get("text")
        if set(data) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
            raise _bad_value(suffix, who="agent")
        return value, {"source": "agent", "model": "agent", "via": "agent",
                       "ask_id": meta.get("ask_id"), "latency_ms": meta.get("latency_ms", 0)}

    data, meta = chat_json(settings, TEXT_VALUE, field_context(goal, element, state))
    value = data.get("text")
    if set(data) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
        raise _bad_value(suffix)
    return value, {"source": "text_model", **meta}
