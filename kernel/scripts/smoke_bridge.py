"""真桥冒烟：真 CLI + 真 Chromium + 假宿主 agent，跑通 agent 决策 → agent 监督 → 零模型回放。

全程零外网、零付费 API：
  · 清空 TEXT_MODEL_API_KEY / TYPESAFE_API_KEY（真进程环境里也清）
  · 把两个 base URL 指向不可达的本地端口作为保险
  · 宿主由本脚本内的线程扮演：循环 `bridge wait` → 读 payload → `bridge answer`

四段：
  [1] agent 决策全链路（--decider agent --supervisor agent）：跑通、退出码 0、via=agent
  [2] 人为阻断 → 监督往返：RECOVER/HINT 真的被执行，trace 有 via=agent 的监督行
  [3] 零模型回放：--decider off --supervisor off 全绿，且不产生任何 bridge ask
  [4] 负例：桥超时无人应答 → 监督 unavailable → 按 M0 停机，status 正常收尾

用法：
    uv run python scripts/smoke_bridge.py [--headed]
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
UNREACHABLE = "http://127.0.0.1:9/v1"      # 9=discard：确保任何 HTTP 尝试都会立刻失败


def unique_goal(tag: str) -> str:
    """每个用例换一个目标文本 ⇒ 换一个缓存键。

    否则 [2]/[4] 会直接命中 [1] 录下的录像（goal_case_hash 只认目标文本 + 断言），
    根本走不到"要问桥"的分支。
    """
    return f"{GOAL}（{tag}）"


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
    """子进程环境：清空两个 key，base URL 指向不可达端口（保险）。"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    env["TEXT_MODEL_API_KEY"] = ""
    env["TYPESAFE_API_KEY"] = ""
    env["TEXT_MODEL_BASE_URL"] = UNREACHABLE
    env["TYPESAFE_BASE_URL"] = UNREACHABLE
    env["UI_AGENT_BRIDGE_TIMEOUT_S"] = "120"
    return env


def spawn_cli(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "ui_agent.cli", *args], cwd=PROJECT_ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=clean_env())


def run_cli(args: list[str], timeout: int = 180) -> tuple[int, str, str]:
    proc = subprocess.run([sys.executable, "-m", "ui_agent.cli", *args], cwd=PROJECT_ROOT,
                          capture_output=True, timeout=timeout, env=clean_env())
    return (proc.returncode, proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"))


def run_cli_to_files(args: list[str], out_path: Path, err_path: Path,
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
    text = (run_dir / "trace.jsonl").read_text(encoding="utf-8").strip()
    return [json.loads(line) for line in text.splitlines()] if text else []


# --- 假宿主 agent ---

class HostAgent:
    """扮演宿主对话大模型：轮询 bridge wait，按剧本作答，直到运行结束。

    剧本按 kind 排队；每次 decide 都按当次 questions 现造一份合法 answers
    （probabilities 全键覆盖、和为 1、choice 最大）——与 Jev 的校验口径一致。
    """

    def __init__(self, run_dir: Path, script: dict[str, list], headed: bool = False):
        self.bridge = AgentBridge(Path(run_dir) / "bridge", timeout_s=120)
        self.script = {kind: list(items) for kind, items in script.items()}
        self.answers: list[dict] = []
        self.stopped = False
        self.error = ""
        self.thread = threading.Thread(target=self._loop, daemon=True)
        _ = headed

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float = 180) -> None:
        self.thread.join(timeout)

    def _loop(self) -> None:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline and not self.stopped:
            try:
                event = self.bridge.wait(timeout_s=1.0)
            except Exception as exc:          # 桥文件坏了就直接记下来
                self.error = f"{exc.__class__.__name__}: {exc}"
                return
            kind = event.get("event")
            if kind == "finished":
                return
            if kind != "ask":
                continue
            ask = event["ask"]
            try:
                payload = self._reply(ask)
            except Exception as exc:
                self.error = f"{exc.__class__.__name__}: {exc}（ask kind={ask['kind']}）"
                return
            self.bridge.answer(ask["id"], payload)
            self.answers.append({"id": ask["id"], "kind": ask["kind"], "answer": payload})

    def _reply(self, ask: dict) -> dict:
        queue = self.script.get(ask["kind"])
        if not queue:
            raise AssertionError(f"剧本里没有更多的 {ask['kind']} 了（id={ask['id']}）")
        item = queue.pop(0)
        if ask["kind"] == "decide":
            operation, label = item
            questions = ask["payload"]["questions"]
            target = None
            if label is not None:
                target = pick_target(questions, operation.lower() + "_target", label)
            return {"answers": sealed(questions, operation, target)}
        if ask["kind"] == "supervise":
            return _resolve_ruling(item, ask["payload"].get("state") or {})
        return item


def _resolve_ruling(item: dict, state: dict) -> dict:
    """监督裁决里的 element 也要按标签找索引——快照索引每次运行都可能不同。"""
    if "action" not in item or "element_label" not in item["action"]:
        return item
    label = item["action"]["element_label"]
    for element in state.get("elements") or []:
        if label in str(element.get("label") or ""):
            action = {k: v for k, v in item["action"].items() if k != "element_label"}
            action["element"] = element["index"]
            return {**item, "action": action}
    raise AssertionError(f"快照里没有标签含『{label}』的元素："
                         f"{[e.get('label') for e in state.get('elements') or []]}")


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


def pick_target(questions: dict, head: str, needle: str) -> str:
    """从候选里挑一个标签包含 needle 的目标。

    候选形状是 {元素索引: {"element": "[7] button 清空全部", ...}}（SELECT 是选项 key），
    所以要在 payload 的 element/option 文本里找，不是在键里找。
    """
    criteria = questions[head]["criteria"]
    for key, payload in criteria.items():
        body = payload or {}
        text = f"{body.get('element') or ''} {body.get('option') or ''}"
        if needle in text:
            return str(key)
    listing = {k: (v or {}).get("element") or (v or {}).get("option") for k, v in criteria.items()}
    raise AssertionError(f"候选里没有含『{needle}』的目标：{listing}")


def run_agent_case(run_dir: Path, url: str, ui_mode: str, script: dict[str, list],
                   goal: str, extra: list[str] | None = None) -> tuple[int, dict, str, list[dict]]:
    """后台起真 CLI + 假宿主，等到结束，返回 (退出码, JSON, stderr, trace 行)。

    输出走临时文件而不是 PIPE：内核会往 stderr 写大量进度行，
    PIPE 写满缓冲区就会把子进程卡死（Windows 上尤其明显）。
    """
    args = ["run", "--goal", goal, "--url", url, "--check", CHECK,
            "--task", "bridge-smoke", "--decider", "agent", "--supervisor", "agent",
            "--run-dir", str(run_dir), "--mode", ui_mode, "--json", *(extra or [])]
    out_path = run_dir.parent / f"{run_dir.name}.stdout"
    err_path = run_dir.parent / f"{run_dir.name}.stderr"
    with out_path.open("wb") as out_file, err_path.open("wb") as err_file:
        proc = subprocess.Popen([sys.executable, "-m", "ui_agent.cli", *args],
                                cwd=PROJECT_ROOT, stdout=out_file, stderr=err_file,
                                env=clean_env())
        host = HostAgent(run_dir, script)
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
    text = out_path.read_text(encoding="utf-8", errors="replace")
    err = err_path.read_text(encoding="utf-8", errors="replace")
    data = load_json(text, "run(agent)") if text.strip() else {}
    rows = read_rows(run_dir) if (run_dir / "trace.jsonl").is_file() else []
    return proc.returncode, data, err, rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true", help="可视化跑一遍（会弹窗）")
    args = parser.parse_args()
    ui_mode = "debug" if args.headed else "ci"

    with tempfile.TemporaryDirectory(prefix="ui-agent-bridge-smoke-") as tmp:
        root = Path(tmp)
        cache_dir = root / "cache"
        os.environ["UI_AGENT_CACHE_DIR"] = str(cache_dir)
        base, httpd = serve(EXAMPLES)
        url = f"{base}/local_fixture.html"
        print(f"[0] 夹具 {url} · 模式 {'可视化' if args.headed else '无头'}")
        print(f"    模型 key 已清空、base URL 指向 {UNREACHABLE}（确保零外网）")
        try:
            # ---------- [1] agent 决策全链路 ----------
            print("\n[1] agent 决策：真 CLI + 真 Chromium + 假宿主作答")
            run1 = root / "run-agent"
            code, data, err, rows = run_agent_case(
                run1, url, ui_mode,
                {"decide": [("CLICK", "清空全部"), ("DONE", None)]}, unique_goal("agent"))
            check("[1] 退出码 0 且 ok=true", code == 0 and data.get("ok") is True,
                  f"code={code} {err[-300:]}")
            check("[1] status=ok（断言真的过了）", data.get("status") == "ok",
                  str(data.get("status")))
            decisions = [row for row in rows if row.get("kind", "decision") == "decision"]
            check("[1] 决策行 via=agent 且 model=agent",
                  bool(decisions) and all(row.get("via") == "agent" and row.get("model") == "agent"
                                          for row in decisions),
                  json.dumps([(row.get("via"), row.get("model")) for row in decisions]))
            status = read_json(run1 / "bridge" / "status.json") or {}
            check("[1] 桥终态 finished", status.get("state") == "finished",
                  json.dumps(status, ensure_ascii=False))
            start = rows[0] if rows else {}
            check("[1] run_start 记了桥配置",
                  start.get("decider") == "agent" and start.get("supervisor_mode") == "agent"
                  and start.get("bridge_dir"),
                  json.dumps({k: start.get(k) for k in ("decider", "supervisor_mode")},
                             ensure_ascii=False))
            asks = sorted((run1 / "bridge").glob("ask-*.json"))
            # 步内预检生效后，CLICK 一旦让断言成立就不再有第二个 decide（以前固定 2 条：
            # CLICK + DONE），所以这里不卡条数，只验桥留下了可审计的连续往返记录。
            check("[1] 桥留了 ask 审计文件",
                  bool(asks) and [p.name for p in asks] ==
                  [f"ask-{i:04d}.json" for i in range(1, len(asks) + 1)],
                  f"{len(asks)} 条：{[p.name for p in asks]}")

            # ---------- [2] 监督往返 ----------
            print("\n[2] 人为阻断 → 监督往返（RECOVER 真的被执行）")
            run2 = root / "run-supervise"
            code, data, err, rows = run_agent_case(
                run2, url, ui_mode,
                {"decide": [("BLOCKED", None), ("DONE", None)],
                 "supervise": [{"ruling": "RECOVER", "reason": "先清空再核对",
                                "action": {"operation": "CLICK",
                                           "element_label": "清空全部"}}]},
                unique_goal("supervise"))
            check("[2] 退出码 0 且最终 ok", code == 0 and data.get("status") == "ok",
                  f"code={code} status={data.get('status')} {err[-300:]}")
            # supervisor 字段：run_start 行是布尔（有没有监督），决策行才是裁决对象
            sup_rows = [row for row in rows
                        if isinstance(row.get("supervisor"), dict)]
            check("[2] trace 有监督行且 via=agent",
                  bool(sup_rows) and sup_rows[0]["supervisor"]["meta"]["via"] == "agent",
                  json.dumps(sup_rows[:1], ensure_ascii=False)[:300])
            recovered = [row for row in rows
                         if row.get("model") == "supervisor"
                         and row.get("operation") == "CLICK"]
            check("[2] RECOVER 走的普通执行路径（决策行里能看到）",
                  bool(recovered), json.dumps([r.get("operation") for r in rows],
                                              ensure_ascii=False))
            kinds = [json.loads(p.read_text(encoding="utf-8"))["kind"]
                     for p in sorted((run2 / "bridge").glob("ask-*.json"))]
            check("[2] ask 序列 = decide → supervise → decide",
                  kinds == ["decide", "supervise", "decide"],
                  json.dumps(kinds))

            # ---------- [3] 零模型回放 ----------
            print("\n[3] 零模型回放：--decider off --supervisor off，全命中、不产生任何 ask")
            before = {p.name for p in run2.glob("bridge/ask-*.json")}
            code, out, err = run_cli_to_files(
                ["run", "--goal", unique_goal("supervise"), "--url", url, "--check", CHECK,
                 "--task", "bridge-smoke-replay", "--decider", "off",
                 "--supervisor", "off", "--run-dir", str(root / "run-replay"),
                 "--mode", ui_mode, "--json"],
                root / "replay.stdout", root / "replay.stderr")
            data = load_json(out, "run(replay)")
            check("[3] 退出码 0 且 ok=true", code == 0 and data.get("ok") is True,
                  f"code={code} {err[-300:]}")
            replay_rows = read_rows(Path(str(data.get("run_dir") or "")))
            decision_replay = [row for row in replay_rows
                               if row.get("kind", "decision") == "decision"]
            check("[3] 决策全部来自录像（via=cache / model=cache）",
                  bool(decision_replay) and all(row.get("via") == "cache"
                                                and row.get("model") == "cache"
                                                for row in decision_replay),
                  json.dumps([(r.get("via"), r.get("model")) for r in decision_replay]))
            check("[3] 模型调用行零条",
                  not [row for row in replay_rows if row.get("kind") == "model"])
            replay_bridge = Path(str(data.get("run_dir"))) / "bridge"
            check("[3] 零模型运行没有创建桥目录", not replay_bridge.exists(), str(replay_bridge))
            after = {p.name for p in (root / "run-supervise").glob("bridge/ask-*.json")}
            check("[3] 上次运行的 ask 文件没被追加", before == after,
                  f"{sorted(before)} -> {sorted(after)}")

            # ---------- [4] 负例：无人应答 ----------
            print("\n[4] 负例：桥超时无人应答 → 监督 unavailable → 按 M0 停机")
            run4 = root / "run-timeout"
            args4 = ["run", "--goal", unique_goal("timeout"), "--url", url, "--check", CHECK,
                     "--task", "bridge-smoke-timeout", "--decider", "agent",
                     "--supervisor", "agent", "--run-dir", str(run4), "--mode", ui_mode,
                     "--json", "--bridge-timeout", "2"]
            out_path4 = root / "timeout.stdout"
            err_path4 = root / "timeout.stderr"
            code4, text, err4 = run_cli_to_files(args4, out_path4, err_path4, timeout=180)
            data = load_json(text, "run(timeout)") if text.strip() else {}
            check("[4] 内核在超时后正常收尾（不挂死）", data.get("status") in
                  ("blocked", "error", "aborted", "check_failed", "stuck"),
                  f"code={code4} status={data.get('status')} {err4[-200:]}")
            timeout_rows = read_rows(run4) if (run4 / "trace.jsonl").is_file() else []
            mentions = json.dumps([row for row in timeout_rows
                                   if row.get("kind") in ("model", "decision")],
                                  ensure_ascii=False)
            check("[4] 错误里点了 bridge timeout", "bridge timeout" in mentions,
                  mentions[-300:])
            status4 = read_json(run4 / "bridge" / "status.json") or {}
            check("[4] status.json 仍是正常终态（不是 crashed）",
                  status4.get("state") == "finished", json.dumps(status4, ensure_ascii=False))
        finally:
            httpd.shutdown()

    print()
    if FAILURES:
        print(f"冒烟失败：{len(FAILURES)} 项")
        for item in FAILURES:
            print(f"  ✗ {item}")
        return 1
    print("冒烟通过：agent 决策 / 监督往返 / 零模型回放 / 超时停机 全部符合预期（零模型调用）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
