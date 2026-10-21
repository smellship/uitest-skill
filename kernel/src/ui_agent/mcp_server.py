"""MCP 适配器：把 Agent 桥的 ask/answer 语义包成 MCP 工具，零内核改动。

桥是协议，MCP 只是另一种传输壳。这里不 import 任何决策/执行代码，只复用
`AgentBridge`（`wait` / `answer`）与 `subprocess` 起内核，和 skill 形态走同一套语义：

    ui_agent_start(plan_file|goal, url, ...) -> {run_id, run_dir}
    ui_agent_next(run_id, timeout_s)         -> ask | heartbeat | finished
    ui_agent_answer(run_id, ask_id, answer)  -> accepted | stale
    ui_agent_status(run_id)                  -> status.json
    ui_agent_abort(run_id)                   -> 终止子进程树

为什么要它：某些宿主只能调工具、不能跑后台命令 + 阻塞等待。桥本身仍需先有，
MCP 只是让这些宿主也能用同一套内核。

**注册方式**（宿主配置文件里，任选其一）：

    {
      "mcpServers": {
        "ui-agent": {
          "command": "uv",
          "args": ["run", "--directory", "<repo>", "python", "-m", "ui_agent.mcp_server"],
          "env": {"TYPESAFE_API_KEY": "..."}
        }
      }
    }

依赖：只要求 `mcp` 包（pip/uv 安装）。没装时本模块 import 会失败——这是有意的，
不让内核常态依赖它。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from ui_agent.bridge.client import AgentBridge
from ui_agent.bridge.protocol import read_json
from ui_agent.config import Settings

DEFAULT_NEXT_TIMEOUT_S = 240.0    # 与 `bridge wait` 同口径：内核冷启动到首个 ask 可能要 60-90s
MAX_NEXT_TIMEOUT_S = 300.0

# 这些开关决定桥本身怎么接线，不许从 extra_args 传（传了会绕开 server 的记账）
FORBIDDEN_EXTRA = {"--run-dir", "--decider", "--supervisor", "--json",
                   "--plan-file", "--goal", "--goal-file", "--bridge-timeout"}


@dataclass
class RunHandle:
    """一个受管运行：子进程 + 桥目录 + 起跑参数。"""

    run_id: str
    run_dir: Path
    argv: list[str]
    proc: subprocess.Popen | None = None
    bridge: AgentBridge | None = None
    started_at: float = field(default_factory=time.time)

    @property
    def bridge_dir(self) -> Path:
        return Settings().bridge_path(self.run_dir)


class RunRegistry:
    """进程内登记表：MCP server 常驻，一个 server 管多个 run。"""

    def __init__(self) -> None:
        self._runs: dict[str, RunHandle] = {}

    def add(self, handle: RunHandle) -> None:
        self._runs[handle.run_id] = handle

    def get(self, run_id: str) -> RunHandle | None:
        return self._runs.get(run_id)

    def all(self) -> list[RunHandle]:
        return list(self._runs.values())

    def drop(self, run_id: str) -> None:
        self._runs.pop(run_id, None)


REGISTRY = RunRegistry()


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


def _argv_for_start(settings: Settings, *, plan_file: str = "", goal: str = "",
                    url: str = "", run_dir: Path, decider: str = "agent",
                    supervisor: str = "agent", vars_: dict[str, str] | None = None,
                    mode: str = "ci", bridge_timeout: float | None = None,
                    extra_args: list[str] | None = None) -> list[str]:
    """组装内核 argv：与 CLI 完全一致，不发明新参数。"""
    argv = ["run", "--run-dir", str(run_dir), "--decider", decider,
            "--supervisor", supervisor, "--mode", mode, "--json"]
    if plan_file:
        argv += ["--plan-file", plan_file]
    else:
        argv += ["--goal", goal, "--url", url]
    if bridge_timeout is not None:
        argv += ["--bridge-timeout", str(bridge_timeout)]
    for key, value in (vars_ or {}).items():
        argv += ["--var", f"{key}={value}"]
    argv += _clean_extra(extra_args)
    return argv


def _clean_extra(extra_args: list[str] | None) -> list[str]:
    """原样追加，但拒绝会破坏桥接线的开关。

    `run` 的其余开关（`--relearn`、`--cache`、`--mask-vars`、五个开销旋钮、
    `--check`、`--task`、`--upload-mode`、`--dialog-policy` …）都允许透传。
    """
    extra = [str(item) for item in (extra_args or [])]
    for index, item in enumerate(extra):
        if item.startswith("-") and item.split("=", 1)[0] in FORBIDDEN_EXTRA:
            raise ValueError(f"extra_args 不许包含 {item.split('=', 1)[0]}："
                             f"该开关由 server 自己装配（会绕开 run 登记与桥接线）")
        if not item.startswith("-") and index == 0:
            raise ValueError(f"extra_args 必须以 `--` 开关开头，收到 {item!r}")
    return extra



def _kernel_command() -> list[str]:
    """内核怎么起：优先 `ui-agent`（已装），否则 `python -m ui_agent.entry`。"""
    override = os.environ.get("UI_AGENT_MCP_KERNEL_CMD", "").strip()
    if override:
        return override.split()
    return [sys.executable, "-m", "ui_agent.entry"]


def _child_env() -> dict:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    return env


def start_run(*, plan_file: str = "", goal: str = "", url: str = "",
              run_dir: str = "", decider: str = "agent", supervisor: str = "agent",
              vars_: dict[str, str] | None = None, mode: str = "ci",
              bridge_timeout: float | None = None,
              extra_args: list[str] | None = None) -> dict:
    """起一次内核运行（后台），返回 run_id 与桥目录；不阻塞。"""
    settings = _settings()
    if not run_dir:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = settings.runs_dir / f"mcp-{stamp}"
    else:
        target = Path(run_dir)
    target.mkdir(parents=True, exist_ok=True)
    argv = _argv_for_start(settings, plan_file=plan_file, goal=goal, url=url,
                           run_dir=target, decider=decider, supervisor=supervisor,
                           vars_=vars_, mode=mode, bridge_timeout=bridge_timeout,
                           extra_args=extra_args)
    out_path = target / "kernel.stdout"
    err_path = target / "kernel.stderr"
    proc = subprocess.Popen([*_kernel_command(), *argv],
                            stdout=out_path.open("wb"), stderr=err_path.open("wb"),
                            env=_child_env(), cwd=str(Path.cwd()))
    handle = RunHandle(run_id=target.name, run_dir=target, argv=argv, proc=proc)
    handle.bridge = AgentBridge(handle.bridge_dir, timeout_s=settings.ui_agent_bridge_timeout_s)
    REGISTRY.add(handle)
    return {"run_id": handle.run_id, "run_dir": str(target), "argv": argv,
            "bridge_dir": str(handle.bridge_dir)}


def next_event(*, run_id: str, timeout_s: float = DEFAULT_NEXT_TIMEOUT_S) -> dict:
    """等下一个事件：ask / heartbeat / finished（与 `bridge wait` 同一契约）。"""
    handle = _require(run_id)
    wait = max(0.0, min(float(timeout_s), MAX_NEXT_TIMEOUT_S))
    if handle.bridge is None:
        handle.bridge = AgentBridge(handle.bridge_dir,
                                    timeout_s=_settings().ui_agent_bridge_timeout_s)
    if not handle.bridge_dir.is_dir():
        return {"ok": True, "event": "heartbeat", "detail": "桥目录尚未出现（内核启动中）"}
    event = handle.bridge.wait(timeout_s=wait)
    event["run_id"] = run_id
    return event


def answer(*, run_id: str, ask_id: int, answer: dict) -> dict:
    handle = _require(run_id)
    if handle.bridge is None:
        handle.bridge = AgentBridge(handle.bridge_dir,
                                    timeout_s=_settings().ui_agent_bridge_timeout_s)
    return handle.bridge.answer(ask_id, answer)


def status(*, run_id: str) -> dict:
    handle = _require(run_id)
    data = read_json(handle.bridge_dir / "status.json") or {}
    proc = handle.proc
    data["run_id"] = run_id
    data["process_alive"] = bool(proc is not None and proc.poll() is None)
    data["exit_code"] = None if proc is None else proc.poll()
    return data


def abort(*, run_id: str) -> dict:
    """终止子进程树（Windows 用 taskkill /T，POSIX 用进程组）。"""
    handle = _require(run_id)
    proc = handle.proc
    if proc is None:
        return {"ok": True, "aborted": False, "detail": "该 run 不是本 server 起的"}
    if proc.poll() is not None:
        return {"ok": True, "aborted": False, "detail": f"进程已退出（{proc.returncode}）"}
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception as exc:                     # 终止失败不该把 server 拖崩
        return {"ok": False, "aborted": False, "detail": f"{exc.__class__.__name__}: {exc}"}
    handle.bridge and handle.bridge.crashed("MCP abort")
    return {"ok": True, "aborted": True, "pid": proc.pid}


def _require(run_id: str) -> RunHandle:
    handle = REGISTRY.get(run_id)
    if handle is None:
        raise KeyError(f"未知的 run_id：{run_id}（本 server 只管自己起的运行；"
                       f"已有：{[h.run_id for h in REGISTRY.all()]}）")
    return handle


# --- MCP 工具面 ---

TOOLS = [
    {"name": "ui_agent_start",
     "description": "起一次 ui-agent 内核运行（后台，不阻塞）。给 plan_file 或 goal+url。"
                    "其余 run 开关（--relearn / --cache / --mask-vars / 开销旋钮 / --check …）"
                    "用 extra_args 原样透传。",
     "inputSchema": {"type": "object", "properties": {
         "plan_file": {"type": "string", "description": "plan.json 路径（与 goal 二选一）"},
         "goal": {"type": "string", "description": "自然语言目标（与 plan_file 二选一）"},
         "url": {"type": "string", "description": "入口地址；给了 goal 时必填"},
         "run_dir": {"type": "string", "description": "产物目录；不给则自动按时间戳生成"},
         "decider": {"type": "string", "enum": ["jev", "agent", "llm", "off"]},
         "supervisor": {"type": "string", "enum": ["on", "off", "agent"]},
         "vars": {"type": "object", "additionalProperties": {"type": "string"}},
         "mode": {"type": "string", "enum": ["debug", "ci"]},
         "bridge_timeout": {"type": "number"},
         "extra_args": {"type": "array", "items": {"type": "string"},
                        "description": "追加的 run 开关，如 [\"--relearn\",\"--settle-ms\",\"1500\"]。"
                                       "不许含 --run-dir/--decider/--supervisor/--plan-file/--goal/"
                                       "--goal-file/--bridge-timeout/--json"}}}},
    {"name": "ui_agent_next",
     "description": "等下一个事件：ask / heartbeat / finished。到点没事件返回 heartbeat，续等即可。",
     "inputSchema": {"type": "object", "required": ["run_id"], "properties": {
         "run_id": {"type": "string"},
         "timeout_s": {"type": "number", "description": "单次等待秒数（默认 240，上限 300）"}}}},
    {"name": "ui_agent_answer",
     "description": "答复一条 ask（原子写入；晚到/重复返回 accepted=false, reason=stale）。",
     "inputSchema": {"type": "object", "required": ["run_id", "ask_id", "answer"],
                     "properties": {
                         "run_id": {"type": "string"},
                         "ask_id": {"type": "integer"},
                         "answer": {"type": "object",
                                    "description": "supervise→{ruling,...}；decide→{answers}；text→{text}"}}}},
    {"name": "ui_agent_status",
     "description": "读运行状态：status.json 内容 + 进程是否还活着 + 退出码。",
     "inputSchema": {"type": "object", "required": ["run_id"],
                     "properties": {"run_id": {"type": "string"}}}},
    {"name": "ui_agent_abort",
     "description": "终止这次运行的子进程树（产物可能不完整）。",
     "inputSchema": {"type": "object", "required": ["run_id"],
                     "properties": {"run_id": {"type": "string"}}}},
]

DISPATCH = {
    "ui_agent_start": start_run,
    "ui_agent_next": next_event,
    "ui_agent_answer": answer,
    "ui_agent_status": status,
    "ui_agent_abort": abort,
}


def call_tool(name: str, arguments: dict) -> dict:
    """工具分发：未知工具名抛 KeyError，参数错误抛 TypeError（MCP 层转成 tool error）。"""
    handler = DISPATCH.get(name)
    if handler is None:
        raise KeyError(f"未知工具：{name}；可用：{'、'.join(DISPATCH)}")
    args = dict(arguments or {})
    if name == "ui_agent_start":
        args["vars_"] = args.pop("vars", None)
    try:
        return handler(**args)
    except KeyError as exc:
        # run_id 不认识：原样抛给 MCP 层当 tool error，不要变成 server 崩溃
        raise KeyError(str(exc)) from exc


def serve() -> int:
    """启动 MCP server（stdio）。没装 mcp 包时给出明确的安装提示。"""
    try:
        import mcp.server.stdio
        from mcp.server import Server
        from mcp.types import TextContent, Tool
    except ImportError:
        print("缺少 mcp 包：uv add mcp（或 pip install mcp）后再启动 MCP server。\n"
              "内核本身不依赖它；skill 形态用 bridge wait/answer 即可，无需本模块。",
              file=sys.stderr)
        return 2

    server = Server("ui-agent")

    @server.list_tools()
    async def list_tools() -> list:
        return [Tool(**item) for item in TOOLS]

    @server.call_tool()
    async def handle_call(name: str, arguments: dict | None) -> list:
        try:
            result = call_tool(name, arguments or {})
        except Exception as exc:
            result = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    import asyncio

    async def main() -> None:
        async with mcp.server.stdio.stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(main())
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
