"""探索模式：只给一句目标，让 agent 自己走一遍页面，再把「走过什么」导出成可编辑的证据。

它**不是**新的决策引擎——探索就是一次「没有断言的单目标运行」：
  · checks=[]        → 模型给 DONE 时自然走 done_unverified（"它说探索完了，但没有断言能证明"）
  · cache=None       → 一步都不录制（探索会点开用不到的页面，录进去回放时会重放垃圾动作）
  · decide/supervise → 复用 M-A 的桥与 Jev（explore --decider jev|agent 都能用）

产物落在 <run-dir>/explore/ 下，不动 run --run-dir 的既有布局：
  goal.txt          本次探索目标
  trajectory.md     逐步轨迹（给 agent 读，比原始 actions.json 省 context）
  explored.yaml     用例骨架：goal 已填、checks 留空——故意过不了 case compile，
                    逼 agent 自己写断言（不替它编，否则等于把模型自称当证据）
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ui_agent.config import Settings
from ui_agent.run.runner import Runner, RunResult

EXPLORE_DIR = "explore"
TYPE_TEXT_SOURCE = {"var": "值来自 --var（不落盘、不进模型）", "agent": "值来自宿主 agent",
                    "text_model": "值来自文本模型"}
OMIT_ACTIONS = {"PAGE"}


def explore(settings: Settings, goal: str, url: str, run_dir: Path,
            variables: dict[str, str] | None = None, bridge=None,
            json_mode: bool = False, verbose: bool = True) -> RunResult:
    """跑一次探索并导出证据；返回的 RunResult 与普通运行同构。"""
    runner = Runner(settings, goal, url, checks=[], task=run_dir.name, vars=variables,
                    run_dir=run_dir, bridge=bridge, cache=None,
                    json_mode=json_mode, verbose=verbose)
    result = runner.run()
    write_artifacts(run_dir, goal, result, settings)
    return result


def write_artifacts(run_dir: Path, goal: str, result: RunResult, settings: Settings) -> dict:
    """把轨迹与用例骨架写到 <run-dir>/explore/；返回写出的路径。"""
    folder = Path(run_dir) / EXPLORE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "goal.txt").write_text(goal + "\n", encoding="utf-8")
    trajectory = build_trajectory(run_dir, goal)
    (folder / "trajectory.md").write_text(trajectory, encoding="utf-8")
    skeleton = build_skeleton(goal, result)
    (folder / "explored.yaml").write_text(skeleton, encoding="utf-8")
    asks = count_asks(run_dir)
    summary = {
        "goal": goal,
        "status": result.status,
        "status_label": result.label,
        "steps_explored": result.steps,
        "bridge_asks": asks,
        "trajectory": str(folder / "trajectory.md"),
        "case_skeleton": str(folder / "explored.yaml"),
        "decider": settings.ui_agent_decider,
    }
    (folder / "explore.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    return summary


def count_asks(run_dir: Path) -> int:
    """桥问答次数：agent 模式的 token 花在宿主循环里，内核只能数这个。"""
    bridge_dir = Path(run_dir) / "bridge"
    if not bridge_dir.is_dir():
        return 0
    return len(list(bridge_dir.glob("ask-*.json")))


def build_trajectory(run_dir: Path, goal: str) -> str:
    """从产物（trace + actions）重建人/agent 可读的轨迹；不回读模型输入。

    注意：`actions.json` 里 TYPE_TEXT 行的 `text` 对**非敏感**字段是明文真值（既有行为）。
    轨迹一律只写「值从哪来」，绝不把该值抄进来——探索产物要能安全地给人和 agent 看。
    """
    trace = _trace_index(run_dir)
    actions = _read_actions(run_dir)
    lines = [f"# 探索轨迹：{goal}", ""]
    if not actions:
        lines.append("（这次探索没有产生任何动作）")
        return "\n".join(lines) + "\n"
    lines.append(f"共 {len(actions)} 个动作；下面按页面分组，序号是动作顺序。")
    lines.append("")
    current = None
    for order, row in enumerate(actions, start=1):
        info = trace.get(row.get("step"), {})
        url = info.get("url", "")
        title = info.get("title", "")
        if url != current:
            current = url
            head = f"## {url}" if url else "## （未知页面）"
            if title:
                head += f"「{title}」"
            lines.append("")
            lines.append(head)
            lines.append("")
        lines.append(f" {order:02d}. {_describe(row, info)}")
    lines.append("")
    lines.append("> 写用例提示：只用上面出现过的控件标签写 goal；断言要能证明业务结果"
                 "（text_contains / element_value / element_count / element_attribute），"
                 "只证明控件还在会被编译器判 weak_final_check。")
    return "\n".join(lines) + "\n"


def _trace_index(run_dir: Path) -> dict:
    """step → {url, title, text_source}：决策行里有页面身份与字段值来源。"""
    out: dict = {}
    for row in _read_trace(run_dir):
        if row.get("kind", "decision") != "decision":
            continue
        step = row.get("step")
        if step is None:
            continue
        meta = row.get("text_model") or {}
        out[step] = {"url": row.get("url") or "", "title": row.get("title") or "",
                     "text_source": str(meta.get("source") or "")}
    return out


def _read_trace(run_dir: Path) -> list[dict]:
    rows = []
    path = Path(run_dir) / "trace.jsonl"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            rows.append(data)
    return rows


def _read_actions(run_dir: Path) -> list[dict]:
    path = Path(run_dir) / "actions.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    return [row for row in rows
            if isinstance(row, dict) and row.get("action") not in OMIT_ACTIONS]


def _describe(row: dict, info: dict) -> str:
    """一行动作说明：绝不写 --var 的值，secret 只留掩码，其它也只说来源。"""
    action = str(row.get("action") or "")
    text = str(row.get("text") or "")
    level = row.get("level")
    mark = f"（{level}）" if level else ""
    changed = " → 页面有变化" if row.get("page_changed") else ""
    if action == "TYPE_TEXT":
        secret = text.strip() == "•" * 6
        source = "敏感字段（已掩码）" if secret else _text_source(info)
        return f"TYPE_TEXT「{row.get('kind') or '字段'}」← {source}{mark}{changed}"
    if action in ("CLICK", "CLICK_FORCE", "HOVER", "SCROLL_TO", "SELECT"):
        body = text or str(row.get("kind") or "")
        return f"{action}「{body}」{mark}{changed}"
    if action == "UPLOAD":
        return f"UPLOAD「{row.get('kind') or ''}」← 文件名 {text}{mark}{changed}"
    if action == "CHECK_FAILED":
        return f"断言未通过：{text}"
    body = text or str(row.get("kind") or "")
    return f"{action} {body}{mark}{changed}".strip()


def _text_source(info: dict) -> str:
    """trace 里的 text_model.source 决定这行值的来路。"""
    return TYPE_TEXT_SOURCE.get(str(info.get("text_source") or ""), "值来自模型")


def build_skeleton(goal: str, result: RunResult) -> str:
    """用例骨架：goal 已填、checks 留空。

    checks 为空是**故意的**：case compile 会以 checks_empty 拒绝，逼 agent 读轨迹自己写断言。
    explore 不替它编——模型自称的结论不是证据。

    id 用一段稳定的短标识（默认 explore-case），由 agent 改成有意义的用例编号；
    id 是缓存身份，改 goal 措辞不会让它失效。
    """
    title = _title_of(goal)
    body = [
        f"# 探索产物骨架（status={result.status}）：checks 必须由 agent 依据轨迹补全",
        "# 编译前请确认每一步都有能证明业务结果的断言，否则 case compile 会拒绝",
        "# id 是缓存身份：定了就别改，改了等于换一份录像",
        "id: explore-case",
        f"title: {title}",
        "steps:",
        "  - goal: |",
        f"      {goal.strip()}",
        "    checks: []",
        "",
    ]
    return "\n".join(body)


def _title_of(goal: str) -> str:
    flat = re.sub(r"\s+", " ", goal.strip())
    flat = flat.replace('"', "")
    return flat[:30] or "探索用例"
