"""console script 入口：只做「按 argv 选模块」的薄分发。

存在的理由：`ui-agent bridge wait` 会被宿主反复调用（一次运行几十上百次），
而 `ui_agent.cli` 连带 playwright，实测冷启动 0.78s。这里看到 `bridge` 就只加载
`ui_agent.bridge_cli`（约 0.1s），其余子命令才进 `ui_agent.cli`。
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "bridge":
        from ui_agent.bridge_cli import main as bridge_main
        return bridge_main(args[1:])
    from ui_agent.cli import main as cli_main
    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
