"""运行目录命名：并发时不能让两个进程抢同一个目录。

时间戳只到秒，同一秒起两个同名任务就会撞到同一个目录，后起的把先起的产物覆盖掉
（trace/results 互相踩）。这里在撞到时依次试 `-2`、`-3`……并用**原子 mkdir** 抢位置：
`Path.mkdir(exist_ok=False)` 在目录已存在时抛 FileExistsError，两个进程同时抢只有一个能成功。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

MAX_SLUG = 60


def slugify(name: str) -> str:
    """任务名 → 目录名片段：保留中英文数字与连字符，其余压成下划线。"""
    slug = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", name).strip("_")
    return (slug or "task")[:MAX_SLUG]


def claim_dir(base: Path) -> Path:
    """占住 base；被占了就 base-2、base-3……返回真正占到的那个。"""
    for attempt in range(1, 100):
        path = base if attempt == 1 else base.with_name(f"{base.name}-{attempt}")
        try:
            path.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return path
    raise RuntimeError(f"连续 99 次都没占到目录：{base}（同名并发过多或有残留目录）")


def make_run_dir(runs_dir: Path, slug: str) -> Path:
    """<runs_dir>/<slug>-<时间戳>，撞名自动加序号。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return claim_dir(Path(runs_dir) / f"{slugify(slug)}-{stamp}")
