"""探索冒烟：真 CLI + 真 Chromium + 假宿主，跑通「探索 → 出 YAML → 录制跑 → 零 token 回放」。

全程零外网、零付费 API（清空两个 key，base URL 指向不可达端口）。

五段：
  [1] 探索：agent 自己走页面，**不写任何录像**，产物落在 <run-dir>/explore/
  [2] 骨架：explored.yaml 带 id 与 goal，但 checks 为空 → case compile 必须拒绝
  [3] 写用例：把开关里的 checks 补上（扮演 agent 写断言）→ case compile 通过
  [4] 录制跑：跑这份 YAML，断言通过后录像落盘
  [5] 零 token：--decider off --supervisor off 全命中，模型调用行零条

用法：
    uv run python scripts/smoke_explore.py [--headed]
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ui_agent.bridge.client import AgentBridge  # noqa: E402
from ui_agent.bridge.protocol import read_json  # noqa: E402

EXAMPLES = PROJECT_ROOT / "examples"
FAILURES: list[str] = []

GOAL = "清空全部模拟行并核对结果"
CHECK = "element_count=模拟行|0"
# YAML 里断言写成「类型: 值」，与 CLI 的 --check 写法不同——这是 agent 写用例时的
# 头号坑，冒烟里显式演示一遍。
CHECK_YAML = "element_count: 模拟行|0"
UNREACHABLE = "http://127.0.0.1:9/v1"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}{'' if ok else '  ← ' + detail}")
    if not ok:
        FAILURES.append(f"{name} {detail}".strip())


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


def serve(directory: Path) -> tuple[str, socketserver.TCPServer]:
    handler = functools.partial(QuietHandler, directory=str(directory))
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd


def clean_env() -> dict:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    env["TEXT_MODEL_API_KEY"] = ""
    env["TYPESAFE_API_KEY"] = ""
    env["TEXT_MODEL_BASE_URL"] = UNREACHABLE
    env["TYPESAFE_BASE_URL"] = UNREACHABLE
    env["UI_AGENT_BRIDGE_TIMEOUT_S"] = "120"
    return env


def run_cli(args: list[str], out_path: Path, err_path: Path,
            timeout: int = 240) -> tuple[int, str, str]:
    """输出走文件：内核的进度行很多，PIPE 写满会把子进程卡死。"""
    with out_path.open("wb") as out_file, err_path.open("wb") as err_file:
        proc = subprocess.Popen([sys.executable, "-m", "ui_agent.cli", *args],
                                cwd=PROJECT_ROOT, stdout=out_file, stderr=err_file,
                                env=clean_env())
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            print("    ⚠ CLI 超时被杀")
    return (proc.returncode,
            out_path.read_text(encoding="utf-8", errors="replace"),
            err_path.read_text(encoding="utf-8", errors="replace"))


def load_json(text: str, where: str) -> dict:
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{where} 的 stdout 不是纯 JSON：{exc}；开头 {text[:200]!r}") from None
    if not isinstance(data, dict):
        raise AssertionError(f"{where} 的 JSON 顶层不是对象")
    return data


def read_rows(run_dir: Path) -> list[dict]:
    path = run_dir / "trace.jsonl"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8").strip()
    return [json.loads(line) for line in text.splitlines()] if text else []


class HostAgent:
    """扮演宿主：决定 CLICK「清空全部」然后 DONE。

    探索没有断言，所以最后一定停在 done_unverified——这正是探索的语义。
    """

    def __init__(self, run_dir: Path, plan: list[tuple[str, str | None]]):
        self.bridge = AgentBridge(Path(run_dir) / "bridge", timeout_s=120)
        self.plan = list(plan)
        self.stopped = False
        self.error = ""
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float = 30) -> None:
        self.thread.join(timeout)

    def _loop(self) -> None:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline and not self.stopped:
            try:
                event = self.bridge.wait(timeout_s=1.0)
            except Exception as exc:
                self.error = f"{exc.__class__.__name__}: {exc}"
                return
            if event.get("event") == "finished":
                return
            if event.get("event") != "ask":
                continue
            ask = event["ask"]
            if ask["kind"] != "decide":
                self.bridge.answer(ask["id"], {"ruling": "HINT", "hint": "继续"})
                continue
            if not self.plan:
                self.bridge.answer(ask["id"], {"error": "探索剧本用完了"})
                continue
            operation, label = self.plan.pop(0)
            questions = ask["payload"]["questions"]
            target = pick_target(questions, operation.lower() + "_target", label) if label else None
            self.bridge.answer(ask["id"], {"answers": sealed(questions, operation, target)})


def pick_target(questions: dict, head: str, needle: str) -> str:
    criteria = questions[head]["criteria"]
    for key, payload in criteria.items():
        body = payload or {}
        text = f"{body.get('element') or ''} {body.get('option') or ''}"
        if needle in text:
            return str(key)
    listing = {k: (v or {}).get("element") for k, v in criteria.items()}
    raise AssertionError(f"候选里没有含『{needle}』的目标：{listing}")


def sealed(questions: dict, operation: str, target: str | None = None) -> dict:
    def one(criteria: list[str], choice: str) -> dict:
        rest = [key for key in criteria if key != choice]
        share = round(0.5 / len(rest), 4) if rest else 0.0
        probabilities = {key: share for key in rest}
        probabilities[choice] = round(1 - share * len(rest), 4)
        return {"choice": choice, "probabilities": probabilities,
                "confidence": probabilities[choice]}

    answers = {"operation": one(list(questions["operation"]["criteria"]), operation)}
    head = operation.lower() + "_target"
    if target is not None and head in questions:
        answers[head] = one(list(questions[head]["criteria"]), target)
    return answers


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true", help="可视化跑一遍（会弹窗）")
    args = parser.parse_args()
    ui_mode = "debug" if args.headed else "ci"

    with tempfile.TemporaryDirectory(prefix="ui-agent-explore-smoke-") as tmp:
        root = Path(tmp)
        cache_dir = root / "cache"
        os.environ["UI_AGENT_CACHE_DIR"] = str(cache_dir)
        base, httpd = serve(EXAMPLES)
        url = f"{base}/local_fixture.html"
        print(f"[0] 夹具 {url} · 模式 {'可视化' if args.headed else '无头'}")
        print(f"    模型 key 已清空、base URL 指向 {UNREACHABLE}（确保零外网）")
        try:
            # ---------- [1] 探索 ----------
            print("\n[1] 探索：agent 自己走页面，产物落 explore/，不写任何录像")
            run1 = root / "run-explore"
            cli_args = ["explore", "--goal", GOAL, "--url", url, "--decider", "agent",
                        "--supervisor", "agent", "--run-dir", str(run1),
                        "--mode", ui_mode, "--json"]
            with (root / "explore.stdout").open("wb") as out_file, \
                    (root / "explore.stderr").open("wb") as err_file:
                proc = subprocess.Popen([sys.executable, "-m", "ui_agent.cli", *cli_args],
                                        cwd=PROJECT_ROOT, stdout=out_file, stderr=err_file,
                                        env=clean_env())
                host = HostAgent(run1, [("CLICK", "清空全部"), ("DONE", None)])
                host.start()
                try:
                    proc.wait(timeout=240)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    print("    ⚠ CLI 超时被杀")
                host.stopped = True
                host.join(timeout=10)
            if host.error:
                print(f"    ⚠ 宿主中止：{host.error}")
            text = (root / "explore.stdout").read_text(encoding="utf-8", errors="replace")
            err = (root / "explore.stderr").read_text(encoding="utf-8", errors="replace")
            data = load_json(text, "explore") if text.strip() else {}

            check("[1] 退出码 0 且 ok=true", proc.returncode == 0 and data.get("ok") is True,
                  f"code={proc.returncode} {err[-400:]}")
            check("[1] status=done_unverified（没有断言，不算成功）",
                  data.get("status") == "done_unverified", str(data.get("status")))
            folder = run1 / "explore"
            check("[1] 轨迹与骨架都写了",
                  (folder / "trajectory.md").is_file()
                  and (folder / "explored.yaml").is_file()
                  and (folder / "goal.txt").is_file(), str(folder))
            check("[1] 探索没有写任何录像",
                  not cache_dir.exists() or not list(cache_dir.glob("*.json")),
                  str(list(cache_dir.glob("*.json")) if cache_dir.exists() else []))
            traj = (folder / "trajectory.md").read_text(encoding="utf-8")
            check("[1] 轨迹里能看到清空动作", "清空全部" in traj, traj[:200])
            status = read_json(run1 / "bridge" / "status.json") or {}
            check("[1] 桥终态 finished", status.get("state") == "finished",
                  json.dumps(status, ensure_ascii=False))

            # ---------- [2] 骨架必须被拒 ----------
            print("\n[2] 骨架：checks 为空 → case compile 必须拒绝（逼 agent 自己写断言）")
            skeleton = folder / "explored.yaml"
            code2, out2, err2 = run_cli(
                ["case", "compile", "--in", str(skeleton), "--json"],
                root / "c2.stdout", root / "c2.stderr")
            compiled = load_json(out2, "case compile(骨架)") if out2.strip() else {}
            check("[2] 退出码 1 且 ok=false", code2 == 1 and compiled.get("ok") is False,
                  f"code={code2}")
            codes = {item["code"] for item in compiled.get("errors") or []}
            check("[2] 错误码是 checks_empty", codes == {"checks_empty"}, json.dumps(sorted(codes)))
            check("[2] plan 为 null", compiled.get("plan") is None, str(type(compiled.get("plan"))))

            # ---------- [3] 补断言 → 编译通过 ----------
            print("\n[3] 补上断言（扮演 agent 写 checks）→ case compile 通过")
            raw = skeleton.read_text(encoding="utf-8")
            filled = raw.replace("    checks: []",
                                 f"    checks:\n      - {CHECK_YAML}")
            case_path = root / "case.yaml"
            case_path.write_text(filled, encoding="utf-8")
            code3, out3, err3 = run_cli(
                ["case", "compile", "--in", str(case_path), "--json"],
                root / "c3.stdout", root / "c3.stderr")
            compiled3 = load_json(out3, "case compile(补断言)") if out3.strip() else {}
            check("[3] 退出码 0 且 ok=true", code3 == 0 and compiled3.get("ok") is True,
                  f"code={code3} {json.dumps(compiled3.get('errors'), ensure_ascii=False)[:300]}")
            check("[3] plan 里保留了 id",
                  bool((compiled3.get("plan") or {}).get("case_id")),
                  json.dumps((compiled3.get("plan") or {}).get("case_id"), ensure_ascii=False))

            # ---------- [4] 录制跑 ----------
            print("\n[4] 录制跑：跑这份 YAML，断言通过后录像落盘")
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(compiled3["plan"]), encoding="utf-8")
            run_record = root / "run-record"
            with (root / "r4.stdout").open("wb") as out_file, \
                    (root / "r4.stderr").open("wb") as err_file:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "ui_agent.cli", "run", "--plan-file", str(plan_path),
                     "--url", url, "--decider", "agent", "--supervisor", "agent",
                     "--run-dir", str(run_record), "--mode", ui_mode, "--json"],
                    cwd=PROJECT_ROOT, stdout=out_file, stderr=err_file, env=clean_env())
                host = HostAgent(run_record, [("CLICK", "清空全部"), ("DONE", None)])
                host.start()
                try:
                    proc.wait(timeout=240)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    print("    ⚠ CLI 超时被杀")
                host.stopped = True
                host.join(timeout=10)
            if host.error:
                print(f"    ⚠ 宿主中止：{host.error}")
            out4 = (root / "r4.stdout").read_text(encoding="utf-8", errors="replace")
            err4 = (root / "r4.stderr").read_text(encoding="utf-8", errors="replace")
            recorded = load_json(out4, "run(record)") if out4.strip() else {}
            check("[4] 退出码 0 且 ok=true", proc.returncode == 0 and recorded.get("ok") is True,
                  f"code={proc.returncode} {err4[-400:]}")
            check("[4] 录像已落盘", cache_dir.is_dir() and bool(list(cache_dir.glob("*.json"))),
                  str(list(cache_dir.glob("*.json")) if cache_dir.is_dir() else "无缓存目录"))
            record_rows = read_rows(Path(str(recorded.get("run_dir") or "")))
            decision_rows = [row for row in record_rows
                             if row.get("kind", "decision") == "decision"]
            check("[4] 决策行带 via=agent", bool(decision_rows)
                  and any(row.get("via") == "agent" for row in decision_rows),
                  json.dumps([row.get("via") for row in decision_rows]))

            # ---------- [5] 零 token 回放 ----------
            print("\n[5] 零 token：--decider off --supervisor off，全命中、不问任何模型")
            code5, out5, err5 = run_cli(
                ["run", "--plan-file", str(plan_path), "--url", url,
                 "--decider", "off", "--supervisor", "off",
                 "--run-dir", str(root / "run-replay"), "--mode", ui_mode, "--json"],
                root / "r5.stdout", root / "r5.stderr")
            replay = load_json(out5, "run(replay)") if out5.strip() else {}
            check("[5] 退出码 0 且 ok=true", code5 == 0 and replay.get("ok") is True,
                  f"code={code5} {err5[-400:]}")
            replay_rows = read_rows(Path(str(replay.get("run_dir") or "")))
            replay_decisions = [row for row in replay_rows
                                if row.get("kind", "decision") == "decision"]
            check("[5] 决策全部来自录像",
                  bool(replay_decisions)
                  and all(row.get("via") == "cache" for row in replay_decisions),
                  json.dumps([row.get("via") for row in replay_decisions]))
            check("[5] 模型调用行零条",
                  not [row for row in replay_rows if row.get("kind") == "model"])
            check("[5] 零模型运行没有桥目录",
                  not (root / "run-replay" / "bridge").exists())
        finally:
            httpd.shutdown()

    print()
    if FAILURES:
        print(f"冒烟失败：{len(FAILURES)} 项")
        for item in FAILURES:
            print(f"  ✗ {item}")
        return 1
    print("冒烟通过：探索 → 骨架被拒 → 补断言 → 录制 → 零 token 回放 全链路符合预期（零模型调用）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
