#!/usr/bin/env python3
"""宿主循环的参考实现：起一次 run，然后 wait → 作答 → answer，直到内核收尾。

为什么需要它：`--decider agent` 一敲下去，内核就不再自己决策了，它会写
`<run-dir>/bridge/ask-0001.json` 然后**阻塞等** `ans-0001.json`。没有这个循环，
命令会一直挂到 `--bridge-timeout`（默认 180s）超时。

它只做机制，不做判断：
  - 起内核子进程（stdout/stderr 落文件，不用管道，避免死锁）
  - 等桥目录出现（内核启动有 1–2s 竞态，别自己 sleep）
  - 轮询 status.json 与 ask-*.json，挑最早未答的那个
  - 交给 policy 算出 answer，原子写 ans-*.json
  - 内核终态 / 进程退出 / 到上限 → 收尾并打印结果

policy 是**你**（宿主模型）的位置。内置 `fixture` 只对本包自带的离线夹具成立，
用来证明「循环通了」，不是通用策略。真实任务用 `--policy <你的脚本.py>`，
里面写一个 `answer(ask, ctx) -> dict`（或分别写 decide/supervise/text 三个函数）。

用法（在 ui-agent/ 包根目录）：

    # 1) 录制：宿主自己答（不花付费 API）
    python scripts/host_loop.py --run-dir .out/rec --policy fixture \
      --goal "在「账号」「密码」里填入给定值，点「确 定」，然后点「查询」" \
      --url "file:///<绝对路径>/kernel/examples/local_fixture.html" \
      --check "element_exists=查询" --check "text_contains=查询完成" \
      --var 账号=demo --var 密码=demo

    # 2) 回放：不需要这个脚本，直接 --decider off
    uv run --project kernel ui-agent run --goal ... --decider off --supervisor off

退出码：0 = 内核 status=ok；1 = 内核跑了但没通过；2 = 循环/内核本身出错。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

POLL_S = 0.25
DIR_WAIT_S = 20.0          # 等桥目录出现（内核启动竞态）
DEFAULT_WAIT_S = 240.0     # 单次 wait 的上限，与 `ui-agent bridge wait` 默认一致
DEFAULT_MAX_ASKS = 200     # 安全阀：正常一次运行几十次
DEFAULT_MAX_SECONDS = 1800.0

KERNEL_ENTRY = ("-m", "ui_agent.entry")


# ---------------------------------------------------------------- 控制台

def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower() not in ("utf-8", "utf8"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def say(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 桥（只用文件，不依赖内核包）

def _read_json(path: Path) -> dict | None:
    """文件不存在 → None；坏 JSON → None（内核自己也只在写时原子，读到半截就是没写完）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict) -> None:
    """与内核同款原子写：同目录 tmp → os.replace。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class Bridge:
    """宿主侧只读/写三类文件：status.json / ask-*.json / ans-*.json。"""

    def __init__(self, bridge_dir: Path, run_dir: Path) -> None:
        self.dir = bridge_dir
        self.run_dir = run_dir

    def status(self, since: float | None = None) -> dict:
        """读运行态。`since` 用来忽略上一轮遗留在同一 run-dir 里的终态。"""
        path = self.dir / "status.json"
        if since is not None:
            try:
                if path.stat().st_mtime < since:
                    return {"state": "running", "detail": "（上一轮遗留的终态，忽略）"}
            except OSError:
                pass
        return _read_json(path) or {"state": "running", "detail": ""}

    def result(self) -> dict | None:
        """终态时的结果：计划模式是 summary.json，单目标模式是 result.json。"""
        return _read_json(self.run_dir / "result.json") or _read_json(self.run_dir / "summary.json")

    def pending(self) -> tuple[int, dict] | None:
        """最早一个「有 ask 没 ans」的问答。"""
        if not self.dir.is_dir():
            return None
        ids = sorted(
            int(path.stem.split("-")[1])
            for path in self.dir.glob("ask-*.json")
            if path.stem.split("-")[1].isdigit()
        )
        for ask_id in ids:
            answer_path = self.dir / f"ans-{ask_id:04d}.json"
            ask_path = self.dir / f"ask-{ask_id:04d}.json"
            try:
                if answer_path.stat().st_mtime >= ask_path.stat().st_mtime:
                    continue  # 同一轮里答过的
            except OSError:
                pass
            ask = _read_json(ask_path)
            if ask is not None:
                return ask_id, ask
        return None

    def answer(self, ask_id: int, answer: dict, note: str = "") -> None:
        _write_json(
            self.dir / f"ans-{ask_id:04d}.json",
            {"id": ask_id, "answer": answer, "answered_by": "host_loop", "note": note},
        )

    def wait_dir(self, seconds: float = DIR_WAIT_S) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.dir.is_dir():
                return True
            time.sleep(0.1)
        return self.dir.is_dir()


# ---------------------------------------------------------------- 作答工具

def pick(criteria: dict, chosen: str, confidence: float = 0.9) -> dict:
    """造一份能过内核校验的选择：覆盖全部 criteria、和=1、被选中的概率最大。"""
    keys = list(criteria)
    if chosen not in keys:
        raise SystemExit(f"内部错误：{chosen!r} 不在候选 {keys} 里")
    others = [k for k in keys if k != chosen]
    rest = (1.0 - confidence) / len(others) if others else 0.0
    probabilities = {k: rest for k in others}
    probabilities[chosen] = confidence if others else 1.0
    return {"choice": chosen, "probabilities": probabilities, "confidence": confidence}


def wrap_decide(answers: dict) -> dict:
    """decide 的答复要再包一层 `answers`——这是最容易搞错的一步。"""
    return {"answers": answers}


def _instructions(questions: dict, head: str) -> dict:
    return (questions.get(head) or {}).get("instructions") or {}


# ---------------------------------------------------------------- 内置 fixture 策略

class FixturePolicy:
    """只对 `kernel/examples/local_fixture.html` 成立的一套固定走法。

    目的是**验证循环**：填两个字段 → 点「确 定」→ 等「查询」出现 → DONE。
    换成任何真实页面都得自己写 policy——这正是宿主模型该干的活。
    """

    name = "fixture"

    def __init__(self, variables: dict[str, str]) -> None:
        self.vars = variables

    # -- 小工具
    def _find(self, elements: list[dict], needle: str) -> dict | None:
        for el in elements:
            if needle in (el.get("label") or ""):
                return el
        return None

    def _value_for(self, element: dict) -> str | None:
        label = element.get("label") or ""
        best = None
        for key, value in self.vars.items():
            if key in label and (best is None or len(key) > len(best[0])):
                best = (key, value)
        return best[1] if best else None

    # -- policy 入口
    def answer(self, ask: dict, ctx: dict) -> dict:
        kind = ask.get("kind")
        payload = ask.get("payload") or {}
        if kind == "decide":
            return wrap_decide(self._decide(payload))
        if kind == "text":
            label = ((payload.get("field") or {}).get("label")) or ""
            value = self._value_for({"label": label})
            if value is None:
                raise SystemExit(f"fixture 策略不知道「{label}」该填什么；用 --var 给它一个值")
            return {"text": value}
        if kind == "supervise":
            say("  ⚠ 收到 supervise ask；fixture 策略只会给一条 HINT")
            return {"ruling": "HINT", "hint": "宿主没有更好的判断，先按当前目标继续。"}
        raise SystemExit(f"未知 ask kind：{kind}")

    def _decide(self, payload: dict) -> dict:
        state = payload.get("state") or {}
        questions = payload.get("questions") or {}
        elements = state.get("elements") or []
        text = ((state.get("page") or {}).get("text")) or ""

        def has(element: dict | None, op: str) -> bool:
            return bool(element) and op in (element.get("operations") or [])

        def do(operation: str, element: dict | None = None) -> dict:
            answers = {"operation": pick(questions["operation"]["criteria"], operation)}
            if element is not None:
                head = f"{operation.lower()}_target"
                criteria = (questions.get(head) or {}).get("criteria") or {}
                key = str(element.get("index"))
                if key not in criteria:
                    raise SystemExit(f"内部错误：元素 {key} 不在 {head} 的候选里（{list(criteria)}）")
                answers[head] = pick(criteria, key)
            return answers

        query = self._find(elements, "查询")
        account = self._find(elements, "账号")
        password = self._find(elements, "密码")
        confirm = self._find(elements, "确 定") or self._find(elements, "确定")

        # 走法：填两个字段 → 点确定（页面出现「已登录」）→ 点查询（出现「查询完成」）→ DONE
        if "查询完成" in text:
            return do("DONE")
        if "已登录" in text:
            if query and has(query, "CLICK"):
                return do("CLICK", query)
            return do("DONE")
        for field in (account, password):
            if field and not field.get("value") and has(field, "TYPE_TEXT"):
                return do("TYPE_TEXT", field)
        if confirm and has(confirm, "CLICK"):
            return do("CLICK", confirm)
        raise SystemExit("fixture 策略走不下去了；页面结构和夹具不一致")


# ---------------------------------------------------------------- 自定义策略

def load_policy(spec: str, variables: dict[str, str]):
    if spec == "fixture":
        return FixturePolicy(variables)
    path = Path(spec)
    if not path.is_file():
        raise SystemExit(f"policy 文件不存在：{path}")
    module_spec = importlib.util.spec_from_file_location("host_policy", path)
    if module_spec is None or module_spec.loader is None:
        raise SystemExit(f"policy 加载失败：{path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    if hasattr(module, "answer"):
        return module
    if not any(hasattr(module, name) for name in ("decide", "supervise", "text")):
        raise SystemExit(f"{path} 里既没有 answer(ask, ctx)，也没有 decide/supervise/text")
    return module


def answer_with(policy, ask: dict, ctx: dict) -> dict:
    if hasattr(policy, "answer"):
        return policy.answer(ask, ctx)
    handler = getattr(policy, ask["kind"], None)
    if handler is None:
        raise SystemExit(f"policy 没有实现 {ask['kind']}，但内核问了这一类")
    return handler(ask.get("payload") or {}, ctx)


# ---------------------------------------------------------------- 起内核

def kernel_python(kernel: Path) -> tuple[str, bool]:
    """优先用 skill 包自带 venv 的 python；否则退回当前解释器（靠 PYTHONPATH 找包）。"""
    for candidate in (
        kernel / ".venv" / "Scripts" / "python.exe",
        kernel / ".venv" / "bin" / "python",
    ):
        if candidate.exists():
            return str(candidate), True
    return sys.executable, False


def build_command(args, kernel: Path) -> list[str]:
    python, in_venv = kernel_python(kernel)
    cmd = [python, *KERNEL_ENTRY, "run", "--run-dir", str(Path(args.run_dir).resolve()), "--json"]
    cmd += ["--decider", args.decider, "--supervisor", args.supervisor]
    if args.plan_file:
        # 子进程的 cwd 是 kernel/，相对路径按调用方的 cwd 解析才不会错
        cmd += ["--plan-file", str(Path(args.plan_file).resolve())]
    if args.goal:
        cmd += ["--goal", args.goal]
    if args.url:
        cmd += ["--url", args.url]
    for check in args.check:
        cmd += ["--check", check]
    for item in args.var:
        cmd += ["--var", item]
    if args.relearn:
        cmd += ["--relearn"]
    if args.cache is not None:
        cmd += ["--cache", args.cache]
    if args.mode:
        cmd += ["--mode", args.mode]
    if args.bridge_timeout is not None:
        cmd += ["--bridge-timeout", str(args.bridge_timeout)]
    cmd += list(args.extra)
    if not in_venv:
        cmd[1:1] = ["-X", "utf8"]
    return cmd


def kernel_env(kernel: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    src = kernel / "src"
    if src.is_dir():
        env["PYTHONPATH"] = str(src) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    browsers = kernel / ".browsers"
    if browsers.is_dir():
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    return env


# ---------------------------------------------------------------- 主循环

def run(args) -> int:
    skill = Path(__file__).resolve().parents[1]
    kernel = Path(args.kernel).resolve() if args.kernel else skill / "kernel"
    if not (kernel / "pyproject.toml").is_file():
        say(f"✗ 找不到内核：{kernel}（用 --kernel 指定）")
        return 2

    run_dir = Path(args.run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    bridge = Bridge(run_dir / "bridge", run_dir)

    variables = {}
    for item in args.var:
        key, _, value = item.partition("=")
        variables[key.strip()] = value.strip()
    policy = load_policy(args.policy, variables)

    cmd = build_command(args, kernel)
    log_path = run_dir / "host_loop.stdout.log"
    say(f"→ 起内核：{' '.join(cmd[:6])} …")
    say(f"  产物目录：{run_dir}")
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            cmd, cwd=str(kernel), env=kernel_env(kernel), stdout=log, stderr=subprocess.STDOUT
        )

    if not needs_bridge(args):
        # --decider off / jev 时不建桥：没有 ask 可答，等进程收尾就行
        try:
            return _wait_without_bridge(proc, bridge, args, log_path)
        finally:
            if proc.poll() is None:
                proc.kill()

    if not bridge.wait_dir(args.dir_wait):
        proc.kill()
        say(f"✗ 等了 {args.dir_wait:.0f}s 桥目录还没出现：{bridge.dir}")
        say(f"  内核输出见 {log_path}")
        return 2

    started = time.monotonic()
    started_wall = time.time()          # 用来忽略同一 run-dir 里上一轮遗留的 status/ask
    answered = 0
    exit_code = 2
    try:
        while True:
            if time.monotonic() - started > args.max_seconds:
                say(f"✗ 超过总时限 {args.max_seconds:.0f}s，停")
                proc.kill()
                return 2

            status = bridge.status(since=started_wall)
            if status.get("state") in ("finished", "crashed"):
                exit_code = _report(bridge, status, answered)
                return exit_code

            item = bridge.pending()
            if item is not None:
                ask_id, ask = item
                if answered >= args.max_asks:
                    say(f"✗ 已答 {answered} 次，达到 --max-asks，停")
                    proc.kill()
                    return 2
                ctx = {"run_dir": run_dir, "bridge_dir": bridge.dir, "vars": variables, "goal": args.goal}
                try:
                    answer = answer_with(policy, ask, ctx)
                except SystemExit:
                    raise
                except Exception as exc:  # policy 自己写崩了，不要吞掉
                    say(f"✗ policy 处理 ask#{ask_id}（{ask.get('kind')}）时出错：{exc.__class__.__name__}: {exc}")
                    proc.kill()
                    return 2
                bridge.answer(ask_id, answer)
                answered += 1
                if not args.quiet:
                    say(f"  ✓ ask#{ask_id} {ask.get('kind')} 已答（累计 {answered}）")
                continue

            if proc.poll() is not None:
                # 进程已退出但 status 还没读到终态：给它一次读盘机会
                time.sleep(POLL_S)
                status = bridge.status(since=started_wall)
                if status.get("state") in ("finished", "crashed"):
                    return _report(bridge, status, answered)
                say(f"✗ 内核进程退出（code={proc.returncode}），但没写终态")
                say(f"  内核输出见 {log_path}")
                return 2

            time.sleep(POLL_S)
    finally:
        if proc.poll() is None:
            proc.kill()


def needs_bridge(args) -> bool:
    """这次运行会不会走桥。

    `--decider off`（纯回放）与 `--decider jev`（HTTP 决策）都不建桥目录，
    此时不该等 `bridge/`，只等进程收尾。
    """
    return args.decider == "agent" or args.supervisor == "agent"


def _wait_without_bridge(proc, bridge: Bridge, args, log_path: Path) -> int:
    """没有桥的运行（`--decider off` / `jev`）：内核不建 bridge/，只等进程收尾。

    这时没有 status.json 可读，终态直接从 `result.json` / `summary.json` 拿。
    """
    started = time.monotonic()
    say("  （这次不走桥：等内核自己跑完）")
    while time.monotonic() - started <= args.max_seconds:
        if proc.poll() is not None:
            time.sleep(POLL_S)          # 让内核把结果写完
            result = bridge.result()
            if result is None:
                say(f"✗ 内核进程退出（code={proc.returncode}），也没写出结果文件")
                say(f"  内核输出见 {log_path}")
                return 2
            # 退出码 1 是「用例没过」，不是内核崩：状态一律按 finished 报，
            # 成败由 result.json 的 status 决定（与有桥路径同口径）。
            status = {"state": "finished", "detail": f"exit={proc.returncode}"}
            return _report(bridge, status, 0)
        time.sleep(POLL_S)
    proc.kill()
    say(f"✗ 超过总时限 {args.max_seconds:.0f}s，停")
    return 2


def _report(bridge: Bridge, status: dict, answered: int) -> int:
    state = status.get("state")
    detail = status.get("detail") or ""
    result = bridge.result() or {}
    # 注意：result.json / summary.json **没有** `ok` 字段（那是 CLI 的 --json 才拼的），
    # 判成功只能看 status。
    ok = result.get("status") == "ok"
    say("")
    say(f"内核终态：{state}（{detail}）")
    if result:
        say(f"  status      : {result.get('status')}")
        say(f"  status_label: {result.get('status_label')}")
        say(f"  detail      : {result.get('detail')}")
        report = result.get("report")
        if report:
            say(f"  报告         : {report}")
        steps = result.get("steps")
        if isinstance(steps, list):
            for step in steps:
                if isinstance(step, dict) and "index" in step:
                    say(f"  第 {step.get('index')} 步 {step.get('status')}：{step.get('detail')}")
        checks = result.get("checks")
        if isinstance(checks, list):
            for item in checks:
                if isinstance(item, dict):
                    mark = "✓" if item.get("ok") else "✗"
                    say(f"  {mark} {item.get('kind')}『{item.get('expected')}』"
                        f"{'' if item.get('ok') else ' — ' + str(item.get('evidence') or '无证据')}")
        defects = result.get("defect_candidates")
        if isinstance(defects, list) and defects:
            say("  可用性缺陷候选：")
            for item in defects:
                say(f"    ⚠ {item}")
    say(f"宿主共作答 {answered} 次")
    if state == "crashed":
        return 2
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(
        prog="host_loop.py",
        description="宿主循环参考实现：起 run → bridge wait → 作答 → answer（--decider agent 必备）",
    )
    parser.add_argument("--run-dir", required=True, help="产物目录（内核必须显式知道桥在哪）")
    parser.add_argument("--kernel", help="内核目录，默认 <本包>/kernel")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--plan-file", help="plan.json 路径")
    target.add_argument("--goal", help="自然语言目标（单目标模式）")
    parser.add_argument("--url", help="入口地址；单目标必填")
    parser.add_argument("--check", action="append", default=[], help="验收断言（kind=值），可重复")
    parser.add_argument("--var", action="append", default=[], metavar="键=值", help="字段值，可重复")
    parser.add_argument("--decider", default="agent", choices=["jev", "llm", "agent", "off"])
    parser.add_argument("--supervisor", default="off", choices=["on", "off", "agent"])
    parser.add_argument("--bridge-timeout", type=float, help="覆盖 UI_AGENT_BRIDGE_TIMEOUT_S")
    parser.add_argument("--relearn", action="store_true", help="忽略已有录像，本次重新录制")
    parser.add_argument("--cache", choices=["on", "off"], help="回放缓存开关（透传内核）")
    parser.add_argument("--mode", choices=["debug", "ci"], help="覆盖 UI_AGENT_MODE")
    parser.add_argument("--policy", default="fixture",
                        help="fixture（内置，只对离线夹具成立）或你的 policy .py 路径")
    parser.add_argument("--max-asks", type=int, default=DEFAULT_MAX_ASKS, help="安全阀：最多答几次")
    parser.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS, help="整体墙钟上限")
    parser.add_argument("--dir-wait", type=float, default=DIR_WAIT_S, help="等桥目录出现的秒数")
    parser.add_argument("--extra", action="append", default=[], help="原样追加给内核的参数，可重复")
    parser.add_argument("--quiet", action="store_true", help="只打结果")
    args = parser.parse_args(argv)
    if args.goal and not args.url:
        parser.error("单目标模式必须给 --url")
    try:
        return run(args)
    except KeyboardInterrupt:
        say("已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
