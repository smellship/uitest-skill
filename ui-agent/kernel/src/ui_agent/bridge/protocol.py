"""Agent 桥的文件契约：ask / answer / status 三个 pydantic 信封 + 原子读写。

命名与路径约定（一个桥目录只服务一次运行，不做多路 claim）：
  ask-{id:04d}.json   内核写，宿主读
  ans-{id:04d}.json   宿主写，内核读
  status.json         内核的运行态（running / finished / crashed）

所有写都是「同目录 *.tmp → os.replace」，避免宿主读到半截 JSON。
所有读写一律 UTF-8，`ensure_ascii=False`（中文 Windows 下 GBK 会崩）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

KINDS = ("supervise", "decide", "text")
STATES = ("running", "finished", "crashed")

STATUS_FILE = "status.json"
RESULT_FILE = "result.json"
SUMMARY_FILE = "summary.json"

DEFAULT_TIMEOUT_S = 180.0   # 对话模型答一次通常秒级；留足思考余量，又不至于白等十分钟
DEFAULT_POLL_S = 0.25

# 宿主手写 ans 时可能被读到半截：解析失败先重试几个轮询周期，再判不可用
ANSWER_RETRY_CYCLES = 2


class BridgeError(RuntimeError):
    """桥自身的错（文件坏、格式不符）：调用方按「答复不可用」处理。"""


class BridgeUnavailable(BridgeError):
    """桥不可用：目录不可写、答复结构不合法、答非所问。"""


class BridgeTimeout(BridgeError):
    """等到 deadline 也没等到答复：硬停，错误文案必须带 bridge timeout。"""


class Ask(BaseModel):
    id: int
    kind: str
    at: str = ""
    deadline_s: float = 180.0
    payload: dict = Field(default_factory=dict)


class Answer(BaseModel):
    id: int
    answer: dict
    answered_by: str = "agent"
    note: str = ""


class BridgeStatus(BaseModel):
    state: str = "running"
    seq: int = 0
    detail: str = ""
    updated_at: str = ""


def now_iso() -> str:
    """桥是本机文件，用本机时区的毫秒时间即可。"""
    return datetime.now().isoformat(timespec="milliseconds")


def ask_name(ask_id: int) -> str:
    return f"ask-{ask_id:04d}.json"


def ans_name(ask_id: int) -> str:
    return f"ans-{ask_id:04d}.json"


def read_json(path: Path) -> dict | None:
    """文件不存在返回 None；JSON 坏 / 顶层不是对象抛 BridgeError。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BridgeError(f"桥文件读不出来：{path}（{exc}）") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise BridgeError(f"桥文件不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(data, dict):
        raise BridgeError(f"桥文件顶层必须是对象：{path}")
    return data


def write_json(path: Path, data: dict) -> None:
    """原子写：同目录 tmp → os.replace；失败按不可用处理（调用方不得静默降级）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise BridgeUnavailable(f"桥目录不可写：{path.parent}（{exc}）") from exc


def parse_ask(data: dict, path: Path) -> Ask:
    try:
        ask = Ask.model_validate(data)
    except ValidationError as exc:
        raise BridgeError(f"ask 结构不合法：{path}（{_brief(exc)}）") from exc
    if ask.kind not in KINDS:
        raise BridgeError(f"ask 的 kind 不在 {KINDS} 里：{ask.kind!r}（{path}）")
    return ask


def parse_answer(data: dict, path: Path) -> Answer:
    try:
        answer = Answer.model_validate(data)
    except ValidationError as exc:
        raise BridgeError(f"ans 结构不合法：{path}（{_brief(exc)}）") from exc
    return answer


def parse_status(data: dict, path: Path) -> BridgeStatus:
    try:
        status = BridgeStatus.model_validate(data)
    except ValidationError as exc:
        raise BridgeError(f"status 结构不合法：{path}（{_brief(exc)}）") from exc
    return status


def _brief(exc: Any, limit: int = 200) -> str:
    text = str(exc).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"
