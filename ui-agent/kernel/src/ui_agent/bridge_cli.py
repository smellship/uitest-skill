"""`ui-agent bridge` 子命令：宿主侧等事件 / 作答。

单独成模块的唯一理由：宿主会反复调用它（一次运行几十上百次），
而 `ui_agent.cli` 连带 playwright（实测冷启动 0.78s）。这里只依赖 bridge 包与 settings，
冷启动约 0.1s。

契约与其它子命令一致：带 `--json` 时 stdout 只留最后一份 JSON，人类话术走 stderr；
退出码 0=正常（含 stale）、2=参数或格式错误。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from ui_agent.bridge.client import AgentBridge
from ui_agent.bridge.protocol import BridgeError
from ui_agent.config import Settings

DEFAULT_WAIT_S = 240.0
# 桥目录由 run 启动时创建，而宿主总是「先起 run，立刻 wait」——中间有几秒竞态。
# 这段宽限期让 wait 自己等目录出现，而不是把宿主循环打断。
DEFAULT_DIR_WAIT_S = 20.0


def _await_dir(path: Path, seconds: float) -> bool:
    """等目录出现（内核起浏览器要几秒）；seconds<=0 时只看一眼。"""
    deadline = time.monotonic() + max(seconds, 0.0)
    while True:
        if path.is_dir():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _bridge_for(args, *, wait_dir_s: float = 0.0) -> AgentBridge | None:
    """从 --run-dir 解析桥目录。

    目录迟迟不出现是参数错误（退出 2），但给 host 一个宽限期吸收启动竞态。
    返回 None 表示目录始终没出现（调用方按各自语义处置）。
    """
    settings = Settings()
    run_dir = Path(args.run_dir)
    bridge_dir = settings.bridge_path(run_dir)
    if not _await_dir(bridge_dir, wait_dir_s):
        return None
    timeout = args.bridge_timeout if args.bridge_timeout is not None else settings.ui_agent_bridge_timeout_s
    return AgentBridge(bridge_dir, timeout_s=timeout)


def _missing_dir(args, waited: float) -> str:
    settings = Settings()
    bridge_dir = settings.bridge_path(Path(args.run_dir))
    hint = "" if waited <= 0 else f"等了 {waited:.0f}s 仍未出现；"
    return (f"桥目录不存在：{bridge_dir}（{hint}先用 agent 模式启动一次 run，并显式给 --run-dir）")


def cmd_bridge_wait(args) -> int:
    waited = args.wait_dir
    bridge = _bridge_for(args, wait_dir_s=waited)
    if bridge is None:
        raise RuntimeError(_missing_dir(args, waited))
    event = bridge.wait(timeout_s=args.timeout)
    if args.json:
        print(json.dumps(event, ensure_ascii=False))
        return 0
    kind = event.get("event")
    if kind == "ask":
        ask = event["ask"]
        print(f"待答：{ask['kind']}（id={ask['id']}，deadline={ask['deadline_s']}s）")
    elif kind == "finished":
        print(f"运行已结束：{event.get('state')} {event.get('detail') or ''}".strip())
    else:
        print("没有新事件（heartbeat），继续等")
    return 0


def cmd_bridge_answer(args) -> int:
    # 桥目录没了（run 已收尾并清理）等价于 stale，不打断宿主循环。
    bridge = _bridge_for(args, wait_dir_s=min(args.wait_dir, 5.0))
    if bridge is None:
        result = {"ok": True, "accepted": False, "reason": "stale"}
    else:
        payload = _load_payload(args)
        result = bridge.answer(args.id, payload["answer"], note=payload.get("note", ""),
                               answered_by=payload.get("answered_by", "agent"))
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if result.get("accepted"):
        print(f"已答复 id={args.id}")
    else:
        print(f"未采用（{result.get('reason')}）：该 ask 已过期、已答过，或运行已结束")
    return 0


def _load_payload(args) -> dict:
    """读 --file / --data，校验是对象且含 answer 对象（可带 answered_by / note）。"""
    if args.file:
        try:
            raw = Path(args.file).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise RuntimeError(f"读不到答复文件 {args.file}：{exc}") from exc
    else:
        raw = args.data
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"答复不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("答复顶层必须是对象，例如 {\"answer\": {...}}")
    answer = data.get("answer")
    if not isinstance(answer, dict):
        raise RuntimeError("答复必须是 {\"answer\": {...}} 形式：answer 必须是对象")
    out = {"answer": answer, "note": str(data.get("note") or "")}
    if "answered_by" in data:
        out["answered_by"] = str(data["answered_by"])
    return out


def add_parsers(sub) -> None:
    """在已有的子命令组上挂 wait / answer（main 与 ui_agent.cli 共用同一份定义）。"""
    wait = sub.add_parser("wait", help="等下一个事件：ask / heartbeat / finished")
    wait.add_argument("--run-dir", required=True, help="运行目录（桥默认在其下的 bridge/）")
    wait.add_argument("--timeout", type=float, default=DEFAULT_WAIT_S,
                      help=f"单次调用阻塞时长（默认 {DEFAULT_WAIT_S:.0f}s；到点返回 heartbeat）")
    wait.add_argument("--wait-dir", type=float, default=DEFAULT_DIR_WAIT_S,
                      help=f"桥目录还没出现时最多等多久（默认 {DEFAULT_DIR_WAIT_S:.0f}s；吸收 run 启动竞态）")
    wait.add_argument("--bridge-timeout", type=float, default=None,
                      help="覆盖 UI_AGENT_BRIDGE_TIMEOUT_S（仅用于探测）")
    wait.add_argument("--json", action="store_true", help="stdout 只输出一份 JSON")
    wait.set_defaults(func=cmd_bridge_wait)

    answer = sub.add_parser("answer", help="答复一条 ask（原子写入；晚到/重复返回 stale）")
    answer.add_argument("--run-dir", required=True, help="运行目录（桥默认在其下的 bridge/）")
    answer.add_argument("--id", type=int, required=True, help="ask 的 id")
    source = answer.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="答复 JSON 文件")
    source.add_argument("--data", help="答复 JSON 文本，如 '{\"answer\": {...}}'")
    answer.add_argument("--note", default="", help="备注（写进 ans 的 note 字段）")
    answer.add_argument("--wait-dir", type=float, default=5.0,
                        help="桥目录还没出现时最多等多久（默认 5s；run 已收尾时直接返回 stale）")
    answer.add_argument("--bridge-timeout", type=float, default=None)
    answer.add_argument("--json", action="store_true", help="stdout 只输出一份 JSON")
    answer.set_defaults(func=cmd_bridge_answer)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ui-agent bridge", description="Agent 桥的宿主侧命令")
    sub = parser.add_subparsers(dest="group", required=True, metavar="{wait,answer}")
    add_parsers(sub)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except (RuntimeError, ValueError, BridgeError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 2
