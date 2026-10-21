#!/usr/bin/env python
"""并行跑多个用例：每个用例一个进程、一个浏览器、一个 profile 目录。

单进程跑一个用例用 `host_loop.py`；要同时跑几个，就用这个把多个 `host_loop.py`
一起拉起来——**不要在同一个内核进程里串行跑**，也不要让两路共用一个 profile：
Chromium 对 user-data-dir 加单实例锁，共用会让后起的那路直接起不来。

做法上刻意保持笨：本脚本不 import 内核，也不解析桥，只负责
  ① 给每一路分配 <输出根>/<名字>/ 与 <输出根>/.profiles/<名字>/ ；
  ② 并发起 N 个 `host_loop.py` 子进程；
  ③ 收尾时按 status 汇总，全 ok 才算过。

用法（清单文件最省事，见 references/parallel.md）：

    python scripts/parallel_run.py --manifest jobs.json

    python scripts/parallel_run.py --out /tmp/runs --max-parallel 2 \
        --job local=../cases/本地夹具/others/local-fixture.plan.json \
        --url file:///.../local_fixture.html --var 账号=a --var 密码=b \
        --job bing=../cases/大肥鱼/others/bing-search-paging.plan.json \
        --url https://cn.bing.com --var 输入搜索词=大肥鱼
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

DEFAULT_KERNEL_ENTRY = ("-m", "ui_agent.entry")
PROFILE_DIRNAME = ".profiles"


# ---------------------------------------------------------------- 小工具

def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def say(message: str = "") -> None:
    print(message, flush=True)


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise SystemExit(f"读不到 {path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} 不是合法 JSON：{exc}") from exc


# ---------------------------------------------------------------- 作业定义

class Job:
    """一路：一个用例 = 一个 run 目录 = 一个 profile 目录。"""

    def __init__(self, name: str, spec: dict, root: Path, defaults: dict) -> None:
        self.name = name
        merged = {**defaults, **{k: v for k, v in spec.items() if v is not None}}
        self.plan_file = merged.get("plan_file")
        self.goal = merged.get("goal")
        self.url = merged.get("url")
        self.checks = list(merged.get("checks") or [])
        self.vars = dict(merged.get("vars") or {})
        self.policy = merged.get("policy") or "fixture"
        self.decider = merged.get("decider") or "agent"
        self.supervisor = merged.get("supervisor") or "off"
        self.relearn = bool(merged.get("relearn"))
        self.cache = merged.get("cache")
        self.mode = merged.get("mode")
        self.bridge_timeout = merged.get("bridge_timeout")
        self.extra = list(merged.get("extra") or [])
        self.root = root
        self.run_dir = root / name
        self.profile_dir = root / PROFILE_DIRNAME / name
        self.exit_code: int | None = None
        self.status: str = "pending"
        self.detail: str = ""
        self.elapsed = 0.0

    def validate(self) -> None:
        if not self.plan_file and not self.goal:
            raise SystemExit(f"作业 {self.name}：plan_file 与 goal 至少给一个")
        if self.goal and not self.url:
            raise SystemExit(f"作业 {self.name}：goal 模式必须给 url")
        if self.plan_file and not Path(self.plan_file).is_file():
            raise SystemExit(f"作业 {self.name}：plan 文件不存在 {self.plan_file}")

    def command(self, script: Path, python: str) -> list[str]:
        cmd = [
            python, str(script),
            "--run-dir", str(self.run_dir),
            "--profile-dir", str(self.profile_dir),
            "--decider", self.decider,
            "--supervisor", self.supervisor,
            "--policy", self.policy,
        ]
        if self.plan_file:
            cmd += ["--plan-file", str(Path(self.plan_file).resolve())]
        if self.goal:
            cmd += ["--goal", self.goal]
        if self.url:
            cmd += ["--url", self.url]
        for check in self.checks:
            cmd += ["--check", check]
        for key, value in self.vars.items():
            cmd += ["--var", f"{key}={value}"]
        if self.relearn:
            cmd += ["--relearn"]
        if self.cache:
            cmd += ["--cache", str(self.cache)]
        if self.mode:
            cmd += ["--mode", str(self.mode)]
        if self.bridge_timeout is not None:
            cmd += ["--bridge-timeout", str(self.bridge_timeout)]
        cmd += self.extra
        return cmd


def jobs_from_manifest(path: Path, root: Path) -> tuple[list[Job], dict]:
    """清单形状：{"jobs": [{name, plan_file|goal, url, vars, ...}], "defaults": {...}}。"""
    data = _read_json(path)
    if not isinstance(data, dict):
        raise SystemExit(f"{path} 顶层必须是对象，例如 {{\"jobs\": [...]}}")
    entries = data.get("jobs")
    if not isinstance(entries, list) or not entries:
        raise SystemExit(f"{path} 里的 jobs 必须是非空数组")
    defaults = dict(data.get("defaults") or {})
    jobs: list[Job] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise SystemExit(f"{path} 里第 {index} 个作业不是对象")
        name = str(entry.get("name") or f"job{index}")
        if name in seen:
            raise SystemExit(f"作业名重复：{name}（每个作业一个产物目录，名字必须唯一）")
        seen.add(name)
        # 清单里的相对路径按清单所在目录解析，不按当前 cwd
        spec = dict(entry)
        if spec.get("plan_file") and not Path(str(spec["plan_file"])).is_absolute():
            spec["plan_file"] = str((path.parent / str(spec["plan_file"])).resolve())
        jobs.append(Job(name, spec, root, defaults))
    return jobs, data


def jobs_from_cli(args, root: Path) -> list[Job]:
    """命令行形状：--job 名字=plan.json 可以重复；--url/--var 之类是所有作业的公共默认。"""
    defaults = {
        "url": args.url,
        "vars": _parse_vars(args.var),
        "policy": args.policy,
        "decider": args.decider,
        "supervisor": args.supervisor,
        "relearn": args.relearn,
        "cache": args.cache,
        "mode": args.mode,
        "bridge_timeout": args.bridge_timeout,
        "checks": args.check,
        "extra": args.extra,
    }
    jobs: list[Job] = []
    seen: set[str] = set()
    for item in args.job:
        name, _, target = item.partition("=")
        name, target = name.strip(), target.strip()
        if not name or not target:
            raise SystemExit(f"--job 要写成 名字=plan.json，收到 {item!r}")
        if name in seen:
            raise SystemExit(f"作业名重复：{name}")
        seen.add(name)
        jobs.append(Job(name, {"plan_file": target}, root, defaults))
    return jobs


def _parse_vars(items: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in items:
        key, _, value = item.partition("=")
        if not key.strip():
            raise SystemExit(f"--var 要写成 键=值，收到 {item!r}")
        parsed[key.strip()] = value
    return parsed


# ---------------------------------------------------------------- 并发执行

class Runner:
    def __init__(self, jobs: list[Job], kernel: Path, script: Path, python: str,
                 max_parallel: int, log_root: Path) -> None:
        self.jobs = jobs
        self.kernel = kernel
        self.script = script
        self.python = python
        self.max_parallel = max_parallel
        self.log_root = log_root
        self.procs: dict[str, subprocess.Popen] = {}
        self.pending = list(jobs)
        self.lock = threading.Lock()

    # -- 单个作业
    def _launch(self, job: Job) -> None:
        job.run_dir.mkdir(parents=True, exist_ok=True)
        job.profile_dir.mkdir(parents=True, exist_ok=True)
        log_path = job.run_dir / "parallel.stdout.log"
        cmd = job.command(self.script, self.python)
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        log = log_path.open("w", encoding="utf-8")
        started = time.monotonic()
        proc = subprocess.Popen(cmd, cwd=str(self.kernel), env=env,
                                stdout=log, stderr=subprocess.STDOUT)
        with self.lock:
            self.procs[job.name] = proc
        say(f"▶ {job.name} 起跑：pid={proc.pid} 产物={job.run_dir}")
        code = proc.wait()
        log.close()
        job.exit_code = code
        job.elapsed = time.monotonic() - started
        job.status, job.detail = self._read_outcome(job)
        mark = "✓" if job.status == "ok" else "✗"
        say(f"{mark} {job.name} 结束：status={job.status} exit={code} {job.elapsed:.1f}s — {job.detail}")

    def _read_outcome(self, job: Job) -> tuple[str, str]:
        for name in ("result.json", "summary.json"):
            path = job.run_dir / name
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                return "error", f"{name} 读不出来：{exc}"
            status = str(data.get("status") or "")
            detail = str(data.get("detail") or "")
            if status:
                return status, detail
        return "error", "内核跑完了但没留下 result.json / summary.json"

    # -- 调度
    def run(self) -> int:
        queue = list(self.pending)
        threads: list[threading.Thread] = []
        running: list[threading.Thread] = []
        try:
            while queue or running:
                while queue and len(running) < self.max_parallel:
                    job = queue.pop(0)
                    # 先占目录再起进程：两路抢同一个 run-dir 会互相覆盖产物
                    thread = threading.Thread(target=self._launch, args=(job,), daemon=True)
                    thread.start()
                    threads.append(thread)
                    running.append(thread)
                time.sleep(0.2)
                running = [t for t in running if t.is_alive()]
        except KeyboardInterrupt:
            say("已中断，正在收掉子进程…")
            for proc in list(self.procs.values()):
                if proc.poll() is None:
                    proc.terminate()
            raise
        finally:
            for thread in threads:
                thread.join(timeout=5)
        return self._report()

    # -- 汇总
    def _report(self) -> int:
        say()
        say("=" * 78)
        say(f"并行结果（{len(self.jobs)} 路，最多同时 {self.max_parallel} 路）")
        say("=" * 78)
        width = max(len(job.name) for job in self.jobs)
        for job in self.jobs:
            mark = "✓" if job.status == "ok" else "✗"
            say(f"  {mark} {job.name.ljust(width)}  {job.status.ljust(12)} "
                f"exit={job.exit_code}  {job.elapsed:5.1f}s  {job.detail}")
        failed = [job for job in self.jobs if job.status != "ok"]
        say()
        if failed:
            say(f"没过：{len(failed)}/{len(self.jobs)} → " + "、".join(job.name for job in failed))
            say("每一步的明细在各路产物目录的 trace.jsonl 与 report.html 里")
            return 1
        say(f"全部 {len(self.jobs)} 路通过")
        return 0


# ---------------------------------------------------------------- 入口

def _kernel_python(kernel: Path) -> tuple[str, bool]:
    for candidate in (kernel / ".venv" / "Scripts" / "python.exe", kernel / ".venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate), True
    return sys.executable, False


def _warn_if_no_browser(kernel: Path) -> None:
    """浏览器没装的话每一路都会以同一句「Executable doesn't exist」失败，先提一句省得查日志。"""
    browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or (kernel / ".browsers"))
    if browsers.is_dir() and any(browsers.glob("chromium-*")):
        return
    say(f"！没找到 Chromium：{browsers}")
    say("  先跑 scripts/bootstrap.ps1（或 .sh）装一次；或用 PLAYWRIGHT_BROWSERS_PATH 指向已有的 .browsers")
    say()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="parallel_run.py",
        description="并行跑多个用例：每路一个进程、一个浏览器、一个 profile 目录",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", help="作业清单 JSON（{\"jobs\": [...]}），相对路径按清单所在目录解析")
    source.add_argument("--job", action="append", default=[], metavar="名字=plan.json",
                        help="一路作业，可重复；与 --manifest 二选一")
    parser.add_argument("--out", help="输出根目录（默认 <本包>/.runs/<时间戳>）；每路一个子目录")
    parser.add_argument("--kernel", help="内核目录，默认 <本包>/kernel")
    parser.add_argument("--max-parallel", type=int, default=0,
                        help="最多同时跑几路（默认 0=不限，全部一起来）；别超过宿主给的子 agent 上限")
    parser.add_argument("--url", help="--job 模式下所有作业的公共入口地址")
    parser.add_argument("--check", action="append", default=[], help="断言（kind=值），可重复；仅 goal 模式")
    parser.add_argument("--var", action="append", default=[], metavar="键=值", help="字段值，可重复")
    parser.add_argument("--policy", default="fixture", help="宿主策略：fixture 或你的 .py 路径")
    parser.add_argument("--decider", default="agent", choices=["jev", "llm", "agent", "off"])
    parser.add_argument("--supervisor", default="off", choices=["on", "off", "agent"])
    parser.add_argument("--bridge-timeout", type=float)
    parser.add_argument("--relearn", action="store_true")
    parser.add_argument("--cache", choices=["on", "off"])
    parser.add_argument("--mode", choices=["debug", "ci"])
    parser.add_argument("--extra", action="append", default=[], help="原样传给内核的参数，可重复")
    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = build_parser()
    args = parser.parse_args(argv)

    skill = Path(__file__).resolve().parents[1]
    kernel = Path(args.kernel).resolve() if args.kernel else skill / "kernel"
    if not (kernel / "pyproject.toml").is_file():
        say(f"✗ 找不到内核：{kernel}（用 --kernel 指定）")
        return 2
    script = Path(__file__).resolve().with_name("host_loop.py")
    if not script.is_file():
        say(f"✗ 找不到 host_loop.py：{script}")
        return 2

    out_root = Path(args.out).resolve() if args.out else skill / ".runs" / _stamp()
    out_root.mkdir(parents=True, exist_ok=True)

    if args.manifest:
        jobs, meta = jobs_from_manifest(Path(args.manifest).resolve(), out_root)
        max_parallel = args.max_parallel or int(meta.get("max_parallel") or 0) or len(jobs)
    else:
        jobs = jobs_from_cli(args, out_root)
        max_parallel = args.max_parallel or len(jobs)
    for job in jobs:
        job.validate()
    max_parallel = max(1, min(max_parallel, len(jobs)))

    python, in_venv = _kernel_python(kernel)
    if not in_venv:
        python = sys.executable
        say(f"！{kernel} 下没有 .venv，用当前解释器；先跑 scripts/bootstrap 装环境更稳")
    _warn_if_no_browser(kernel)

    say(f"输出根目录：{out_root}")
    say(f"并发上限：{max_parallel} 路 · 作业数：{len(jobs)}")
    say(f"每路自带 profile：{out_root / PROFILE_DIRNAME / '<名字>'}")
    say()

    runner = Runner(jobs, kernel, script, python, max_parallel, out_root)
    try:
        return runner.run()
    except KeyboardInterrupt:
        say("已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
