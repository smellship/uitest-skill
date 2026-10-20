"""把内核同步进 skill 包（ui-agent/kernel），让「一个文件夹」自足可跑。

skill 包布局（与 kernel/ 平级，整体拷进宿主的 skills 目录即可）：

    ui-agent/
      SKILL.md                 宿主 agent 的入口
      references/              8 篇按需读的说明书
      scripts/                 bootstrap.ps1 / bootstrap.sh
      kernel/                  ← 本脚本生成：内核最小可运行集
      examples/cases/          ← 两个能直接跑的样例用例

为什么要有这一步：内核的开发树在 ``kernel/``，但它带 ``.venv`` / ``.artifacts`` /
``.browsers`` / ``runs`` / ``tests``，直接拷是几百 MB。这里只挑运行必需的文件，
结果是 1 MB 左右、可以整目录搬走。

用法::

    uv run python scripts/build_skill.py            # 同步
    uv run python scripts/build_skill.py --check    # 只校验有没有漂移（CI 用）
    uv run python scripts/build_skill.py --with-tests

``--check`` 发现漂移时退出码 1，并列出不一致的文件。``.venv`` / ``.browsers`` / ``.artifacts`` /
``runs`` 是用户跑过 bootstrap 之后才出现的本地环境（合计约 600 MB），同步时**原样保留**：
只删清单内的文件，不整目录 rmtree。
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROJECT_ROOT.parent / "ui-agent"
TARGET = SKILL_ROOT / "kernel"

# 运行必需：整目录搬
COPY_DIRS = ("src", "examples")
# 运行必需：单文件
COPY_FILES = ("pyproject.toml", "uv.lock", "README.md", ".env.example", ".gitignore")
# 自检用：setup.md 让用户跑这两条，所以得带上（约 2 KB）
SCRIPT_FILES = ("env.ps1", "smoke_browser.py")
# 可选：让宿主 agent 能自检（+1.7 MB）
OPTIONAL_DIRS = ("tests",)
# 样例用例：从仓库 cases/ 里挑这几份，目标目录自足。
# offline 那份指向包内 examples/local_fixture.html，不联网也能跑，用来自检；
# big-fish 那份是真实站点，需要网络。
SAMPLE_CASES = (
    ("本地夹具/local-fixture.yaml", "offline.yaml"),
    ("本地夹具/others/local-fixture.plan.json", "offline.plan.json"),
    ("大肥鱼/bing-search-paging.yaml", "big-fish.yaml"),
    ("大肥鱼/others/bing-search-paging.plan.json", "big-fish.plan.json"),
)
SAMPLE_DIR = SKILL_ROOT / "examples" / "cases"
CASES_ROOT = PROJECT_ROOT.parent / "cases"

# 不该进 skill 包的东西（万一被误放进 COPY_DIRS）
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache")

# 用户跑过 bootstrap 之后才会出现的子树：既不在清单里，也不许被当漂移或被删。
# .venv 约 156 MB，.browsers 约 435 MB，删掉就得重装。
LOCAL_DIRS = {".venv", ".browsers", ".artifacts", "runs"}


def _iter(root: Path):
    """列出 root 下所有文件（相对路径），跳过字节码、缓存与本地环境。"""
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in LOCAL_DIRS or part in {"__pycache__", ".pytest_cache", ".ruff_cache"} for part in rel.parts):
            continue
        if rel.suffix == ".pyc":
            continue
        yield rel


def _plan(with_tests: bool) -> list[tuple[Path, Path]]:
    """返回 [(源文件, 目标文件)] 的全量清单。"""
    pairs: list[tuple[Path, Path]] = []
    dirs = list(COPY_DIRS) + (list(OPTIONAL_DIRS) if with_tests else [])
    for name in dirs:
        src_dir = PROJECT_ROOT / name
        if not src_dir.is_dir():
            raise SystemExit(f"✗ 源目录不存在：{src_dir}")
        for rel in _iter(src_dir):
            pairs.append((src_dir / rel, TARGET / name / rel))
    for name in COPY_FILES:
        src = PROJECT_ROOT / name
        if not src.is_file():
            raise SystemExit(f"✗ 源文件不存在：{src}")
        pairs.append((src, TARGET / name))
    for name in SCRIPT_FILES:
        src = PROJECT_ROOT / "scripts" / name
        if not src.is_file():
            raise SystemExit(f"✗ 源文件不存在：{src}")
        pairs.append((src, TARGET / "scripts" / name))
    for src_rel, dst_name in SAMPLE_CASES:
        src = CASES_ROOT / src_rel
        if not src.is_file():
            continue  # 样例缺了不算错：别人的仓库未必带这两个用例
        pairs.append((src, SAMPLE_DIR / dst_name))
    return pairs


def _check(pairs: list[tuple[Path, Path]]) -> int:
    """只比较，不写。返回不一致的条目数。"""
    bad: list[str] = []
    for src, dst in pairs:
        if not dst.is_file():
            bad.append(f"缺  {dst.relative_to(SKILL_ROOT)}")
        elif not filecmp.cmp(src, dst, shallow=False):
            bad.append(f"变  {dst.relative_to(SKILL_ROOT)}")
    # 反向：目标里多出来的文件（源已删）
    if TARGET.is_dir():
        wanted = {dst.resolve() for _, dst in pairs}
        for rel in _iter(TARGET):
            if (TARGET / rel).resolve() not in wanted:
                bad.append(f"多  {(Path('kernel') / rel)!s}")
    for line in bad:
        print(f"  {line}")
    if bad:
        print(f"\n✗ 有 {len(bad)} 处漂移；跑 uv run python scripts/build_skill.py 同步")
        return 1
    print(f"✓ skill 包与内核一致（{len(pairs)} 个文件）")
    return 0


def _sync(pairs: list[tuple[Path, Path]]) -> int:
    if TARGET.exists():
        # 只删清单内的文件，绝不整目录 rmtree：用户可能已在 ui-agent/kernel 里跑过
        # bootstrap，那里有 .venv（~156 MB）与 .browsers（~435 MB），删掉要重装。
        keep = {dst.resolve() for _, dst in pairs}
        for path in sorted(TARGET.rglob("*"), reverse=True):
            if path.resolve() in keep:
                continue
            rel = path.relative_to(TARGET)
            if any(part in LOCAL_DIRS for part in rel.parts):
                continue  # .venv / .browsers / .artifacts / runs：用户的环境，别动
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
    changed = 0
    for src, dst in pairs:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        changed += 1
    print(f"✓ 同步 {changed} 个文件 → {TARGET}")
    total = sum(
        p.stat().st_size for rel in _iter(TARGET) if (p := TARGET / rel).is_file()
    )
    print(f"  体积 {total / 1024 / 1024:.2f} MB（不含 .venv / .browsers / .artifacts）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="build_skill", description="把内核同步进 skill 包")
    ap.add_argument("--check", action="store_true", help="只校验漂移，不写文件")
    ap.add_argument("--with-tests", action="store_true", help="连 tests/ 一起带上（+1.7 MB）")
    args = ap.parse_args(argv)

    if not SKILL_ROOT.is_dir():
        raise SystemExit(f"✗ 找不到 skill 包目录：{SKILL_ROOT}")
    pairs = _plan(args.with_tests)
    return _check(pairs) if args.check else _sync(pairs)


if __name__ == "__main__":
    sys.exit(main())
