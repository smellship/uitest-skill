#!/usr/bin/env python3
"""探索用的通用宿主策略：点一次「最像提交」的按钮，看清结果就收。

探索不计断言，目标是**把页面走一遍、看清控件和结果长什么样**，不是把业务跑完。
所以这里只做一件很笨的事：

1. 页面里找一个像「执行/提交/查询/搜索/登录/确定」的按钮或链接，点它一次；
2. 点过之后（不管页面变没变）直接 DONE。

拿它当起点比空白 policy 好：绝大多数系统的第一屏都是「填几个输入 → 点一个按钮」，
这一下就能把提交后的页面（结果表格、日志、详情）暴露出来。真要走多步，就在
`candidates` 之后按页面正文自己加分支。

用法：

    python scripts/host_loop.py --explore \
      --run-dir "$OUT" --profile-dir "$OUT/.profile" \
      --goal-file "$GOAL_FILE" --url "$URL" \
      --policy scripts/explore_policy.py \
      --decider agent --supervisor off

接口契约（见 references/running.md）：`answer(ask, ctx) -> dict`，decide 的返回值
必须包一层 `{"answers": {...}}`。
"""

from __future__ import annotations

# 按「越靠前越像提交」排序。都是小写子串匹配，控件标签里含它就命中。
SUBMIT_WORDS = (
    "run query",
    "execute",
    "submit",
    "查询",
    "搜索",
    "登录",
    "登陆",
    "确定",
    "确 定",
    "提交",
    "发送",
    "search",
    "go",
)

# 这些不是「提交」，别点：它们会把页面导向无关的地方，或者删掉东西。
AVOID_WORDS = ("reset", "clear", "cancel", "取消", "重置", "清空", "删除", "delete", "remove", "close", "关闭")


def _pick(criteria: dict, chosen: str, confidence: float = 0.9) -> dict:
    """造一份能过 validate_choice 的答复：覆盖全部候选键、和=1、被选概率最大。"""
    keys = list(criteria)
    rest = (1.0 - confidence) / max(1, len(keys) - 1) if len(keys) > 1 else 0.0
    return {
        "choice": chosen,
        "probabilities": {k: (confidence if k == chosen else rest) for k in keys},
        "confidence": confidence,
    }


def _do(questions: dict, operation: str, element: dict | None = None) -> dict:
    answers = {"operation": _pick(questions["operation"]["criteria"], operation)}
    if element is not None:
        head = f"{operation.lower()}_target"
        criteria = (questions.get(head) or {}).get("criteria") or {}
        key = str(element.get("index"))
        if key not in criteria:
            raise SystemExit(f"内部错误：元素 {key} 不在 {head} 的候选里")
        answers[head] = _pick(criteria, key)
    return answers


def _rank(element: dict) -> int:
    label = (element.get("label") or "").lower()
    for score, word in enumerate(SUBMIT_WORDS):
        if word in label:
            return score
    return len(SUBMIT_WORDS)


def _candidates(elements: list[dict]) -> list[dict]:
    usable = []
    for element in elements:
        label = (element.get("label") or "").lower()
        if not label or any(word in label for word in AVOID_WORDS):
            continue
        operations = element.get("operations") or []
        if "CLICK" not in operations:
            continue
        usable.append(element)
    usable.sort(key=lambda e: (_rank(e), e.get("index", 0)))
    return usable


def answer(ask: dict, ctx: dict) -> dict:
    kind = ask.get("kind")
    if kind == "text":
        raise SystemExit("探索里不该问 text：要么把值写进 --var，要么先手工跑一遍")
    if kind == "supervise":
        return {"ruling": "HINT", "hint": "按当前目标继续：找到执行查询的按钮点一次，看清结果就收。"}

    payload = ask.get("payload") or {}
    state = payload.get("state") or {}
    questions = payload.get("questions") or {}
    elements = state.get("elements") or []
    recent = state.get("recent_actions") or []

    # RecentAction 的字段是 action / kind（元素 index）/ text（标签）/ level / page_changed。
    # 判「点过没有」要看 text，不是看 action。
    clicked = any((a.get("action") or "") == "CLICK" for a in recent)

    if not clicked:
        for element in _candidates(elements):
            head = "click_target"
            criteria = (questions.get(head) or {}).get("criteria") or {}
            if str(element.get("index")) in criteria:
                return {"answers": _do(questions, "CLICK", element)}

    return {"answers": _do(questions, "DONE")}
