"""Agent 桥客户端：内核侧（ask 阻塞等答复）与 CLI 侧（wait / answer）共用一份实现。

内核侧：
    bridge.start()                      —— run 开始前建目录、写 status=running
    data, meta = bridge.ask("decide", payload)   —— 写 ask、轮询 ans、到点抛 BridgeTimeout
    bridge.finish_from_result(result) / crashed(detail)   —— 终态（归 CLI，不归 Runner）

CLI 侧（无状态，任何 shell 里都能调）：
    bridge.wait(timeout_s)              —— 拿到「最早一条没答的 ask」或终态或 heartbeat
    bridge.answer(ask_id, answer)       —— 原子写 ans，晚到/重复一律 stale

两个不变量：
1. **绝不静默降级**：目录不可写、答复结构不合法、id 不匹配都抛 BridgeError 家族，
   由各缝自己决定是重问还是按 M0 停机。
2. **轮询可注入时钟**：单测不真睡（clock/sleeper 都是构造参数）。
"""

from __future__ import annotations

import time
from pathlib import Path

from ui_agent.bridge.protocol import (
    ANSWER_RETRY_CYCLES,
    DEFAULT_POLL_S,
    DEFAULT_TIMEOUT_S,
    RESULT_FILE,
    STATUS_FILE,
    SUMMARY_FILE,
    Answer,
    BridgeError,
    BridgeStatus,
    BridgeTimeout,
    BridgeUnavailable,
    ans_name,
    ask_name,
    now_iso,
    parse_answer,
    parse_ask,
    parse_status,
    read_json,
    write_json,
)


class AgentBridge:
    def __init__(self, bridge_dir: Path, timeout_s: float = DEFAULT_TIMEOUT_S,
                 poll_s: float = DEFAULT_POLL_S, clock=time.monotonic, sleeper=time.sleep):
        self.dir = Path(bridge_dir)
        self.timeout_s = float(timeout_s)
        self.poll_s = float(poll_s)
        self._clock = clock
        self._sleep = sleeper
        self._next_id = 0

    # --- 路径 ---

    def ask_path(self, ask_id: int) -> Path:
        return self.dir / ask_name(ask_id)

    def ans_path(self, ask_id: int) -> Path:
        return self.dir / ans_name(ask_id)

    @property
    def status_path(self) -> Path:
        return self.dir / STATUS_FILE

    # --- 内核侧 ---

    def start(self) -> None:
        """建目录并写 status=running；不可写立刻抛（装配错误，不静默降级）。"""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BridgeUnavailable(f"桥目录建不出来：{self.dir}（{exc}）") from exc
        self._next_id = self._scan_max_id()
        self._write_status(BridgeStatus(state="running", seq=self._next_id, updated_at=now_iso()))

    def ask(self, kind: str, payload: dict) -> tuple[dict, dict]:
        """写一条 ask 并阻塞等答复；返回 (answer, meta)。

        超时抛 BridgeTimeout；答复坏抛 BridgeUnavailable（上层可转成 ValueError 触发重问）。
        """
        if self._next_id <= 0:
            self._next_id = self._scan_max_id()
        self._next_id += 1
        ask_id = self._next_id
        ask = {
            "id": ask_id,
            "kind": kind,
            "at": now_iso(),
            "deadline_s": self.timeout_s,
            "payload": dict(payload),
        }
        write_json(self.ask_path(ask_id), ask)
        self._write_status(BridgeStatus(state="running", seq=ask_id, detail=f"ask {kind}",
                                        updated_at=now_iso()))
        started = self._clock()
        while True:
            answer = self._try_read_answer(ask_id)
            if answer is not None:
                latency_ms = int((self._clock() - started) * 1000)
                meta = {"via": "agent", "ask_id": ask_id, "answered_by": answer.answered_by,
                        "latency_ms": latency_ms}
                return dict(answer.answer), meta
            if self._clock() - started >= self.timeout_s:
                raise BridgeTimeout(
                    f"bridge timeout: kind={kind} id={ask_id} after {self.timeout_s:.1f}s"
                    f"（宿主未在 {self.ans_path(ask_id).name} 作答）")
            self._sleep(self.poll_s)

    def finish(self, detail: str = "") -> None:
        status = self._read_status_or_default()
        self._write_status(BridgeStatus(state="finished", seq=status.seq, detail=detail,
                                        updated_at=now_iso()))

    def crashed(self, detail: str) -> None:
        status = self._read_status_or_default()
        self._write_status(BridgeStatus(state="crashed", seq=status.seq, detail=detail,
                                        updated_at=now_iso()))

    def finish_from_result(self, result: dict | None = None) -> None:
        """正常收尾（含 result.status="error"）：终态一律 finished，细节进 detail。"""
        state = ""
        if isinstance(result, dict):
            state = str(result.get("status") or "")
        self.finish(detail=f"status={state}" if state else "")

    # --- CLI 侧 ---

    def wait(self, timeout_s: float = 240.0) -> dict:
        """无状态轮询：先看终态，再看最早一条没答的 ask，都没有就 heartbeat。"""
        started = self._clock()
        while True:
            event = self._poll_once()
            if event is not None:
                return event
            if self._clock() - started >= timeout_s:
                return {"ok": True, "event": "heartbeat"}
            self._sleep(self.poll_s)

    def answer(self, ask_id: int, answer: dict, note: str = "",
               answered_by: str = "agent") -> dict:
        """原子写 ans；桥不存在 / ask 不存在 / 已终态 / 已答过一律 stale（不报错）。"""
        if not isinstance(answer, dict):
            raise BridgeError("answer 必须是对象")
        if not self.dir.is_dir():
            return {"ok": True, "accepted": False, "reason": "stale"}
        status = self._read_status_or_default()
        if status.state in ("finished", "crashed"):
            return {"ok": True, "accepted": False, "reason": "stale"}
        if not self.ask_path(ask_id).is_file():
            return {"ok": True, "accepted": False, "reason": "stale"}
        if self.ans_path(ask_id).is_file():
            return {"ok": True, "accepted": False, "reason": "stale"}
        write_json(self.ans_path(ask_id),
                   Answer(id=ask_id, answer=dict(answer), answered_by=answered_by,
                          note=note).model_dump())
        return {"ok": True, "accepted": True, "id": ask_id}

    # --- 内部 ---

    def _poll_once(self) -> dict | None:
        status = self._status_or_running()
        if status.state in ("finished", "crashed"):
            return {"ok": True, "event": "finished", "state": status.state,
                    "detail": status.detail, "result": self._read_result()}
        ask_id = self._earliest_unanswered()
        if ask_id is None:
            return None
        data = read_json(self.ask_path(ask_id))
        if data is None:
            return None
        ask = parse_ask(data, self.ask_path(ask_id))
        return {"ok": True, "event": "ask", "kind": ask.kind, "ask": ask.model_dump()}

    def _read_result(self) -> dict | None:
        """终态时附带的产物：目标模式读 result.json，计划模式退回 summary.json。"""
        for name in (RESULT_FILE, SUMMARY_FILE):
            try:
                data = read_json(self.dir.parent / name)
            except BridgeError:
                return None
            if data is not None:
                return data
        return None

    def _earliest_unanswered(self) -> int | None:
        ids = sorted(self._ask_ids())
        for ask_id in ids:
            if not self.ans_path(ask_id).is_file():
                return ask_id
        return None

    def _ask_ids(self) -> list[int]:
        try:
            names = list(self.dir.glob("ask-*.json"))
        except OSError:
            return []
        ids: list[int] = []
        for path in names:
            stem = path.stem                      # ask-0007
            _, _, number = stem.partition("-")
            if number.isdigit():
                ids.append(int(number))
        return ids

    def _scan_max_id(self) -> int:
        """从目录扫描出当前最大编号，防止 CLI 重启撞号。"""
        top = max(self._ask_ids(), default=0)
        status = self._read_status_or_default()
        return max(top, status.seq)

    def _try_read_answer(self, ask_id: int) -> Answer | None:
        """读 ans：不存在返回 None；解析失败先重试几个周期（防半截文件），仍坏则抛不可用。"""
        path = self.ans_path(ask_id)
        for attempt in range(ANSWER_RETRY_CYCLES + 1):
            try:
                data = read_json(path)
            except BridgeError:
                data = None
                if attempt < ANSWER_RETRY_CYCLES:
                    self._sleep(self.poll_s)
                    continue
                raise BridgeUnavailable(f"答复文件无法解析：{path}") from None
            if data is None:
                return None
            try:
                answer = parse_answer(data, path)
            except BridgeError as exc:
                # 内核侧读答复失败一律是"这条答复不可用"（上层可转成校验错触发重问）
                raise BridgeUnavailable(str(exc)) from exc
            if answer.id != ask_id:
                raise BridgeUnavailable(
                    f"答复的 id 与 ask 不匹配：文件 {path.name} 里写的是 {answer.id}；未采用该答复。")
            return answer
        return None

    def _read_status_or_default(self) -> BridgeStatus:
        """status 缺失按 running 处理（启动竞态），不要误报 finished；坏文件也不当终态。"""
        try:
            data = read_json(self.status_path)
        except BridgeError:
            return BridgeStatus()
        if data is None:
            return BridgeStatus()
        try:
            return parse_status(data, self.status_path)
        except BridgeError:
            return BridgeStatus()

    def _status_or_running(self) -> BridgeStatus:
        return self._read_status_or_default()

    def _write_status(self, status: BridgeStatus) -> None:
        write_json(self.status_path, status.model_dump())
