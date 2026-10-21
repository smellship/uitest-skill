#!/usr/bin/env python3
"""桥问答的紧凑助手：把 ask-*.json 压成几百 token 的摘要，并按契约写 ans-*.json。

为什么需要它：一个 decide ask 的 payload 里带着整页元素表，实测单条 320 KB（约 8 万 token）。
宿主模型如果直接 `cat` 这个文件，几次就把上下文烧光。这个脚本只打该看的那几行，
并且保证写出的答复能过内核的 `validate_choice`（probabilities 覆盖全部候选、和≈1、被选最大）。

它**不 import 内核**，只认文件协议：`<run-dir>/bridge/ask-NNNN.json` 与 `ans-NNNN.json`。

用法（在 ui-agent/ 包根目录）：
    # 看当前待答的是什么
    python scripts/ask.py show --run-dir .out/rec

    # 作答：点第 34 个元素
    python scripts/ask.py answer --run-dir .out/rec --id 3 --op CLICK --target 34

    # 其它形态
    python scripts/ask.py answer --run-dir .out/rec --id 3 --done
    python scripts/ask.py answer --run-dir .out/rec --id 3 --wait
    python scripts/ask.py answer --run-dir .out/rec --id 3 --text "要填的值"
    python scripts/ask.py answer --run-dir .out/rec --id 3 --error "为什么推进不了"

退出码：0 成功；1 没有待答的 ask / 参数与 ask 不匹配；2 读文件出错。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_SHOW = 40          # show 默认列多少个候选元素
TEXT_LIMIT = 600           # show 打印页面正文的字符数


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _read(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"✗ {path.name} 不是合法 JSON：{exc}") from exc
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def bridge_dir(run_dir: Path) -> Path:
    return run_dir / "bridge"


def pending(run_dir: Path) -> tuple[int, dict] | None:
    """最早一个「有 ask 没 ans」的问答。"""
    folder = bridge_dir(run_dir)
    if not folder.is_dir():
        return None
    asks = sorted(folder.glob("ask-*.json"))
    for path in asks:
        try:
            ask_id = int(path.stem.split("-")[1])
        except (IndexError, ValueError):
            continue
        if not (folder / f"ans-{ask_id:04d}.json").exists():
            data = _read(path)
            if data is not None:
                return ask_id, data
    return None


# ---------------------------------------------------------------- show

def _elements(payload: dict) -> list[dict]:
    state = payload.get("state") or {}
    elements = state.get("elements")
    return elements if isinstance(elements, list) else []


def _criteria(questions: dict, head: str) -> dict:
    spec = questions.get(head)
    if isinstance(spec, dict):
        found = spec.get("criteria")
        if isinstance(found, dict):
            return found
    return {}


def show(args) -> int:
    found = pending(Path(args.run_dir))
    if found is None:
        print("没有待答的 ask（要么还没写出来，要么已经答完了）")
        return 1
    ask_id, ask = found
    kind = ask.get("kind")
    payload = ask.get("payload") or {}
    print(f"ask #{ask_id}  kind={kind}  deadline={ask.get('deadline_s')}s")

    if kind == "text":
        field = payload.get("field") or {}
        page = payload.get("page") or {}
        print(f"目标：{payload.get('goal') or ''}")
        print(f"页面：{page.get('title') or ''}  {page.get('url') or ''}")
        print(f"控件：{field.get('label')}  role={field.get('role')}  当前值={field.get('current_value')!r}")
        hint = payload.get("hint")
        if hint:
            print(f"提示：{hint}")
        print("作答：python scripts/ask.py answer --run-dir <dir> --id %d --text \"值\"" % ask_id)
        return 0

    if kind == "supervise":
        stuck = payload.get("stuck") or {}
        state = payload.get("state") or {}
        print(f"卡住原因：{stuck.get('trigger')} / {stuck.get('detail')}")
        print(f"当前目标：{payload.get('current_goal')}")
        remaining = payload.get("remaining_goals") or []
        if remaining:
            print("后续目标：" + " | ".join(str(x) for x in remaining))
        _print_elements(state.get("elements") or [], args.limit)
        print("作答：RULING 见 references/supervising.md（HINT/RECOVER/REPLAN/ABORT）")
        return 0

    # decide
    questions = payload.get("questions") or {}
    state = payload.get("state") or {}
    page = state.get("page") or {}
    print(f"目标：{(questions.get('operation') or {}).get('instructions', {}).get('goal', '')}")
    url = page.get("url") or ""
    if not args.full_url and len(url) > 120:
        url = url[:117] + "…"
    print(f"页面：{page.get('title') or ''}  {url}")
    recent = state.get("recent_actions") or []
    if recent:
        print("刚做过：" + " → ".join(_brief_action(item) for item in recent[-4:]))
    text = (page.get("text") or "").strip()
    if text:
        print(f"正文前 {TEXT_LIMIT} 字：{text[:TEXT_LIMIT]}")
    operations = _criteria(questions, "operation")
    print(f"可选操作：{'、'.join(operations)}")
    error = payload.get("previous_error")
    if error:
        print(f"上次被拒：{error}")

    head = None
    for name in questions:
        if name != "operation" and isinstance(questions.get(name), dict):
            head = name
            break
    elements = {item.get("index"): item for item in _elements(payload) if isinstance(item, dict)}
    print("")
    pattern = (args.grep or "").lower()
    if pattern:
        print(f"--- 候选（只列标签含「{args.grep}」的，上限 {args.limit}）---")
    else:
        print(f"--- 候选（上限 {args.limit} 个，键=行号，取元素 index 作答）---")
    for name, spec in questions.items():
        if name == "operation":
            continue
        crit = _criteria(questions, name)
        if not crit:
            continue
        matched = []
        for key in crit:
            element = _pick_element(crit[key], elements)
            label = crit[key] if element is None else _label(element)
            if pattern and pattern not in str(label).lower():
                continue
            matched.append((key, label))
        if pattern and not matched:
            continue
        print(f"[{name}] {len(matched)}/{len(crit)} 个候选")
        for key, label in matched[:args.limit]:
            print(f"  {key}  {label}")
        if len(matched) > args.limit:
            print(f"  …（还有 {len(matched) - args.limit} 个没列）")
    if pattern and not any(
        pattern in str(_label(_pick_element(spec, elements)) or spec).lower()
        for name, spec in questions.items() if name != "operation"
        for spec in _criteria(questions, name).values()
    ):
        print("（没有候选标签命中这个关键字；换个词，或去掉 --grep 看全部）")
    if head:
        print("")
        print("作答：python scripts/ask.py answer --run-dir <dir> --id %d --op CLICK --target <键>" % ask_id)
    return 0


def _pick_element(spec, elements: dict) -> dict | None:
    if not isinstance(spec, dict):
        return None
    raw = spec.get("element")
    if isinstance(raw, str):
        # 形如 "[34] button Run query"：取方括号里的 index
        if raw.startswith("[") and "]" in raw:
            try:
                return elements.get(int(raw[1:raw.index("]")]))
            except ValueError:
                return None
    return None


def _label(element: dict) -> str:
    return f"[{element.get('index')}] {element.get('role')} {element.get('label') or ''}".strip()


def _brief_action(item) -> str:
    if isinstance(item, dict):
        op = item.get("operation") or item.get("op") or "?"
        target = item.get("target")
        return f"{op}{(' ' + str(target)) if target else ''}"
    return str(item)


def _print_elements(elements: list[dict], limit: int) -> None:
    for element in elements[:limit]:
        if isinstance(element, dict):
            print(f"  {_label(element)}")


# ---------------------------------------------------------------- answer

def pick(criteria: dict, chosen: str, confidence: float = 0.9) -> dict:
    """造一个能过 validate_choice 的 choice：覆盖全部候选、和为 1、被选的最大。"""
    keys = list(criteria)
    if chosen not in keys:
        raise SystemExit(f"✗ {chosen!r} 不在候选里：{'、'.join(str(k) for k in keys)}")
    rest = [key for key in keys if key != chosen]
    confidence = min(max(float(confidence), 0.5), 0.999)
    if not rest:
        return {"choice": chosen, "probabilities": {chosen: 1.0}, "confidence": confidence}
    share = (1.0 - confidence) / len(rest)
    probabilities = {key: round(share, 4) for key in rest}
    probabilities[chosen] = round(1.0 - share * len(rest), 4)
    return {"choice": chosen, "probabilities": probabilities, "confidence": confidence}


def answer(args) -> int:
    run_dir = Path(args.run_dir)
    found = pending(run_dir)
    if found is None:
        print("没有待答的 ask")
        return 1
    ask_id, ask = found
    if args.id is not None and int(args.id) != ask_id:
        print(f"✗ --id {args.id} 与当前待答的 ask #{ask_id} 不一致（一次 ask 只能答一次，答错就是 stale）")
        return 1
    kind = ask.get("kind")
    payload = ask.get("payload") or {}

    if args.error:
        body: dict = {"error": args.error}
    elif args.text is not None:
        body = {"text": args.text}
    else:
        questions = payload.get("questions") or {}
        operations = _criteria(questions, "operation")
        operation = args.op or "DONE"
        if operation not in operations:
            raise SystemExit(f"✗ 操作 {operation!r} 不在本次候选里：{'、'.join(operations)}")
        answers = {"operation": pick(operations, operation, args.conf)}
        head = f"{operation.lower()}_target"
        criteria = _criteria(questions, head)
        if criteria:
            if args.target is None:
                raise SystemExit(f"✗ {operation} 需要 --target（候选：{'、'.join(criteria)}）")
            key = str(args.target)
            if key not in criteria:
                raise SystemExit(f"✗ --target {key} 不在 {head} 的候选里：{'、'.join(criteria)}")
            answers[head] = pick(criteria, key, args.conf)
        elif args.target is not None:
            raise SystemExit(f"✗ {operation} 没有目标候选，不该给 --target")
        body = {"answers": answers}

    if kind == "text" and "text" not in body:
        raise SystemExit("✗ text ask 只接受 --text")
    if kind == "decide" and "answers" not in body and "error" not in body:
        raise SystemExit("✗ decide ask 需要 --op（或 --error）")

    path = bridge_dir(run_dir) / f"ans-{ask_id:04d}.json"
    _write_json(path, {"id": ask_id, "answer": body, "answered_by": "ask.py", "note": args.note or ""})
    print(f"✓ 已答 ask #{ask_id}（{kind}）→ {path.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(
        prog="ask.py",
        description="桥问答助手：压缩显示待答 ask，并按契约作答（不 import 内核）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    viewer = sub.add_parser("show", help="显示当前待答的 ask（紧凑摘要）")
    viewer.add_argument("--run-dir", required=True)
    viewer.add_argument("--limit", type=int, default=DEFAULT_SHOW, help="最多列多少个候选")
    viewer.add_argument("--grep", help="只列标签含这个关键字的候选（候选动辄 250 个，先过滤再看）")
    viewer.add_argument("--full-url", action="store_true", help="打印完整 URL（默认截断）")

    responder = sub.add_parser("answer", help="作答并写 ans-*.json")
    responder.add_argument("--run-dir", required=True)
    responder.add_argument("--id", type=int, help="ask 编号（给了就校验，防答错轮次）")
    group = responder.add_mutually_exclusive_group()
    group.add_argument("--op", help="decide ask 的操作，如 CLICK / TYPE_TEXT / DONE")
    group.add_argument("--text", help="text ask 的字段值")
    group.add_argument("--error", help="decide ask 的放弃理由（内核会按 BLOCKED 收尾）")
    responder.add_argument("--target", help="目标键：元素 index，或 SELECT 的 idx:n")
    responder.add_argument("--conf", type=float, default=0.9, help="置信度（0.5–0.999）")
    responder.add_argument("--note", help="备注，写进 ans-*.json")
    responder.add_argument("--done", action="store_true", help="等价于 --op DONE")
    responder.add_argument("--wait", action="store_true", help="等价于 --op WAIT")

    args = parser.parse_args(argv)
    if getattr(args, "done", False):
        args.op = "DONE"
    if getattr(args, "wait", False):
        args.op = "WAIT"
    try:
        return show(args) if args.cmd == "show" else answer(args)
    except KeyboardInterrupt:
        print("已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
