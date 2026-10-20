"""跑全套冒烟（九套既有 + 两套新增），逐套给出通过/失败与耗时。

用法：
    uv run python scripts/run_all_smoke.py            # 全跑
    uv run python scripts/run_all_smoke.py --only plan replay
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SUITES = [
    ("cli", "scripts/smoke_cli.py"),
    ("snapshot", "scripts/smoke_snapshot.py"),
    ("plan", "scripts/smoke_plan.py"),
    ("supervisor", "scripts/smoke_supervisor.py"),
    ("upload", "scripts/smoke_upload.py"),
    ("m3", "scripts/smoke_m3.py"),
    ("assert", "scripts/smoke_assert.py"),
    ("replay", "scripts/smoke_replay.py"),
    ("browser", "scripts/smoke_browser.py"),
    ("bridge", "scripts/smoke_bridge.py"),
    ("explore", "scripts/smoke_explore.py"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", help="只跑这几套（按名字）")
    args = parser.parse_args()
    chosen = [item for item in SUITES if not args.only or item[0] in args.only]

    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    failures: list[tuple[str, int]] = []
    for name, rel in chosen:
        started = time.perf_counter()
        print(f"\n{'=' * 70}\n=== {name} ({rel})\n{'=' * 70}", flush=True)
        proc = subprocess.run([sys.executable, rel], cwd=PROJECT_ROOT, env=env)
        elapsed = time.perf_counter() - started
        mark = "✓" if proc.returncode == 0 else "✗"
        print(f"{mark} {name}: 退出码 {proc.returncode} · {elapsed:.1f}s", flush=True)
        if proc.returncode != 0:
            failures.append((name, proc.returncode))

    print(f"\n{'=' * 70}")
    total = len(chosen)
    if failures:
        print(f"冒烟未全绿：{total - len(failures)}/{total} 通过")
        for name, code in failures:
            print(f"  ✗ {name}（退出码 {code}）")
        return 1
    print(f"全部 {total} 套冒烟通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
