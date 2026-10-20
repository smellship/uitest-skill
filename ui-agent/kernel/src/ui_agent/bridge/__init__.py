"""Agent 桥：内核与宿主对话大模型之间的文件往返协议。

对话大模型不能被 subprocess 同步调用，但任何 agent 都能跑后台命令、阻塞等待、读写文件，
所以内核把「问一次模型」变成「写一个 ask 文件，等一个 ans 文件」：

    <run-dir>/bridge/
      status.json     {"state": "running|finished|crashed", "seq": 7, ...}
      ask-0007.json   内核写（原子：tmp + os.replace）
      ans-0007.json   宿主写（deadline 内越早越好）

协议只定义文件形状与读写，不做策略：答复怎么校验、答歪了怎么降级，仍由各个缝自己收口
（supervise 过 parse_ruling，decide 过 assemble_decision，text 过 textfill 校验）。
桥答复一律当成模型输出，与 HTTP 缝同等不信任。
"""

from __future__ import annotations

from ui_agent.bridge.client import AgentBridge
from ui_agent.bridge.protocol import (
    KINDS,
    STATES,
    Answer,
    Ask,
    BridgeError,
    BridgeStatus,
    BridgeTimeout,
    BridgeUnavailable,
    read_json,
    write_json,
)

__all__ = [
    "KINDS",
    "STATES",
    "AgentBridge",
    "Answer",
    "Ask",
    "BridgeError",
    "BridgeStatus",
    "BridgeTimeout",
    "BridgeUnavailable",
    "read_json",
    "write_json",
]
