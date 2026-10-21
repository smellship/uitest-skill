#!/usr/bin/env python3
"""校验 skill 文档与内核源码一致。

文档最怕的不是写错，是**内核改了、文档没改**，而读者没法分辨。
这个脚本只做一件事：从内核源码里把「词表」抽出来（断言种类、停机状态、缓存词表、
操作清单、CLI 开关……），逐条确认它们确实出现在该出现的文档里。

只用标准库（`ast` 读源码常量），不 import 内核，所以随便哪个 python 3.9+ 都能跑：

    python scripts/check_docs.py
    python scripts/check_docs.py -v      # 列出每一条通过项

退出码 0 = 全过，1 = 有文档漏了或写了不存在的词。
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
KERNEL = SKILL_ROOT / "kernel"
SRC = KERNEL / "src" / "ui_agent"
REFS = SKILL_ROOT / "references"

# 文档里绝不该出现的路径：它们只对开发树成立，装到别人机器上就是断链
FORBIDDEN = (
    "skills/ui-agent",
    "cases/大肥鱼",
    "cases/百望",
    "examples/cases",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _const(rel: str, name: str):
    """从源码里取一个模块级字面量（tuple / dict / str / int），不执行它。"""
    tree = ast.parse(read(SRC / rel), filename=str(SRC / rel))
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                return ast.literal_eval(node.value)
    raise SystemExit(f"✗ 在 {rel} 里找不到常量 {name}；内核可能改名了，先看源码再改脚本")


def _cli_flags() -> list[str]:
    """把 cli.py / bridge_cli.py 里所有 add_argument 的 flag 抽出来。"""
    flags: list[str] = []
    for rel in ("cli.py", "bridge_cli.py"):
        text = read(SRC / rel)
        for match in re.finditer(r'add_argument\(\s*"([^"]+)"', text):
            flag = match.group(1)
            if flag.startswith("-") and flag not in flags:
                flags.append(flag)
    return flags


class Report:
    def __init__(self, verbose: bool) -> None:
        self.verbose = verbose
        self.bad: list[str] = []
        self.good = 0

    def ok(self, msg: str) -> None:
        self.good += 1
        if self.verbose:
            print(f"  ✓ {msg}")

    def fail(self, msg: str) -> None:
        self.bad.append(msg)
        print(f"  ✗ {msg}")

    def words(self, label: str, words, doc: Path, *, minimum: int = 1) -> None:
        """words 里的每一个都必须出现在 doc 里。"""
        text = read(doc)
        missing = [w for w in words if str(w) not in text]
        if missing:
            self.fail(f"{doc.name} 缺少 {label}：{'、'.join(map(str, missing))}")
            return
        if len(words) < minimum:
            self.fail(f"{doc.name} 的 {label} 只有 {len(words)} 条，至少应有 {minimum} 条")
            return
        self.ok(f"{doc.name} 含全部 {len(words)} 个 {label}")


def check_vocab(rep: Report) -> None:
    print("词表：源码常量是否都写进了文档")

    kinds = _const("schema/check.py", "KINDS")
    rep.words("断言种类", kinds, REFS / "cases.md", minimum=8)

    result_kinds = _const("case/yaml_case.py", "RESULT_KINDS")
    rep.words("结果类断言", result_kinds, REFS / "cases.md", minimum=5)

    labels = _const("schema/status.py", "STATUS_LABELS")
    rep.words("停机状态", list(labels), REFS / "debugging.md", minimum=11)

    for name in ("CACHE_MODES", "CACHE_WRITES", "CACHE_MISSES"):
        values = _const("run/trace.py", name)
        rep.words(name, values, REFS / "debugging.md")

    for name in ("RULINGS", "RECOVER_OPS"):
        values = _const("schema/supervisor.py", name)
        rep.words(name, values, REFS / "supervising.md")

    ops = _const("decide/jev.py", "OPERATION_LABELS")
    rep.words("操作种类", list(ops), REFS / "deciding.md", minimum=13)

    press = _const("decide/jev.py", "PRESS_KEYS")
    rep.words("按键", list(press), REFS / "deciding.md")


def check_facts(rep: Report) -> None:
    print("事实：默认值与契约是否与源码一致")
    timeout = _const("bridge/protocol.py", "DEFAULT_TIMEOUT_S")
    if float(timeout) != 180.0:
        rep.fail(f"桥超时默认值变成了 {timeout}；文档里写的是 180s，需同步改 protocol.py 或文档")
    else:
        rep.ok("桥超时默认 180s")
    rep.words("桥超时", [int(timeout)], REFS / "running.md")
    rep.words("桥超时", [int(timeout)], REFS / "envs.md")

    policy = _const("replay/cache.py", "CACHE_POLICY_VERSION")
    if policy != "v3":
        rep.fail(f"CACHE_POLICY_VERSION 变成 {policy} 了；文档承诺保持 v3，需确认是否要升版本")
    else:
        rep.ok("缓存策略版本仍是 v3")

    modes = _const("bridge/protocol.py", "KINDS")
    rep.words("桥的 ask 种类", modes, REFS / "running.md", minimum=3)

    states = _const("bridge/protocol.py", "STATES")
    rep.words("桥终态", states, REFS / "running.md")

    # 上传：分号分隔、accept 预检
    rep.words("上传分隔符", [";"], REFS / "security.md")


def check_cli(rep: Report) -> None:
    print("命令面：cli.py 的开关是否都提到了")
    flags = _cli_flags()
    corpus = "\n".join(
        read(p) for p in [SKILL_ROOT / "SKILL.md", *sorted(REFS.glob("*.md"))]
    )
    missing = [f for f in flags if f not in corpus]
    if missing:
        rep.fail(f"有 {len(missing)} 个 CLI 开关全文档未提及：{'、'.join(missing)}")
    else:
        rep.ok(f"{len(flags)} 个 CLI 开关都在文档里出现过")

    for sub in ("run", "explore", "report"):
        if sub not in read(SKILL_ROOT / "SKILL.md"):
            rep.fail(f"SKILL.md 没提子命令 {sub}")
        else:
            rep.ok(f"SKILL.md 提到 {sub}")


def check_links(rep: Report) -> None:
    print("链接与结构")
    skill = read(SKILL_ROOT / "SKILL.md")
    linked = set(re.findall(r"\((references/[a-z0-9\-]+\.md)\)", skill))
    on_disk = {f"references/{p.name}" for p in REFS.glob("*.md")}

    for miss in sorted(linked - on_disk):
        rep.fail(f"SKILL.md 链接了不存在的 {miss}")
    if not linked - on_disk:
        rep.ok("SKILL.md 的链接都指向真实文件")

    for orphan in sorted(on_disk - linked):
        rep.fail(f"{orphan} 没被 SKILL.md 路由表收录（读者找不到它）")
    if not on_disk - linked:
        rep.ok(f"{len(on_disk)} 篇 references 全部被路由")

    # 每篇 references 内部的相对链接也要能落地
    for path in sorted(REFS.glob("*.md")):
        for target in re.findall(r"\((?!http)([a-zA-Z0-9\-]+\.md)\)", read(path)):
            if not (REFS / target).is_file():
                rep.fail(f"{path.name} 链接了不存在的 {target}")
    rep.ok("references 之间的相对链接都有效")

    if not skill.startswith("---\nname: ui-agent"):
        rep.fail("SKILL.md 缺 frontmatter（name: ui-agent）")
    else:
        rep.ok("frontmatter 齐全")


def check_paths(rep: Report) -> None:
    print("路径：不许出现只对开发树成立的写法")
    targets = [
        SKILL_ROOT / "SKILL.md",
        *sorted(REFS.glob("*.md")),
        *sorted((SKILL_ROOT / "scripts").glob("bootstrap.*")),
    ]
    hits: list[str] = []
    for path in targets:
        text = read(path)
        for bad in FORBIDDEN:
            if bad in text:
                hits.append(f"{path.relative_to(SKILL_ROOT)} 含 {bad}")
    if hits:
        for line in hits:
            rep.fail(line)
    else:
        rep.ok(f"{len(targets)} 个文件都不含开发树专用路径")

    # 用例不能进了包：skill 只描述结构
    strays = [
        p.relative_to(SKILL_ROOT)
        for p in SKILL_ROOT.rglob("*")
        if p.is_file()
        and p.suffix in {".yaml", ".yml"}
        and "kernel" not in p.relative_to(SKILL_ROOT).parts
    ]
    if strays:
        rep.fail(f"skill 包里出现了用例文件（用例应留在用户自己的库）：{'、'.join(map(str, strays))}")
    else:
        rep.ok("skill 包里没有 .yaml 用例")


def check_outputs(rep: Report) -> None:
    print("产物约定：outputs 口径是否写清")
    repo = read(REFS / "cases-repo.md")
    for token in ("outputs", "--run-dir"):
        if token not in repo:
            rep.fail(f"cases-repo.md 没讲 {token}")
        else:
            rep.ok(f"cases-repo.md 讲了 {token}")
    for doc in ("running.md", "exploring.md", "cases.md"):
        if "--run-dir" not in read(REFS / doc):
            rep.fail(f"{doc} 起 run 却没提 --run-dir（产物会落到包内）")
        else:
            rep.ok(f"{doc} 提了 --run-dir")

    # .gitignore 模板：outputs 与 .profiles 必须挡，录像必须留。
    # 录像一旦被挡，换台机器就得重录一遍，「克隆下来零 token 复跑」这条承诺就断了。
    block = _gitignore_block(repo)
    if block is None:
        rep.fail("cases-repo.md 没给 .gitignore 模板")
        return
    for must in ("**/outputs/", ".profiles/"):
        if must in block:
            rep.ok(f".gitignore 模板挡了 {must}")
        else:
            rep.fail(f".gitignore 模板没挡 {must}")
    if ".cache" in block:
        rep.fail("cases-repo.md 的 .gitignore 模板挡了 .cache/：录像要入库，否则换机就得重录")


def _gitignore_block(text: str) -> str | None:
    """取出 cases-repo.md 里那段 ```gitignore 围栏。"""
    marker = "```gitignore"
    start = text.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = text.find("```", start)
    return text[start:end] if end > 0 else None


def check_plan_file(rep: Report) -> None:
    """落 plan 只能靠 --out。

    曾经踩过两次：① `case compile --json > plan.json` 写的是外壳，run --plan-file 解析不出来；
    ② PowerShell 的 `>/Set-Content -Encoding UTF8` 加 BOM，json.loads 直接抛错。
    内核已经两项都兜住了（--out 写本体、读侧用 utf-8-sig），但文档不该再教弯路。
    """
    print("plan 落盘：是否只用 --out，而不是重定向 --json")
    for doc in (SKILL_ROOT / "SKILL.md", REFS / "cases.md", REFS / "cases-repo.md"):
        text = read(doc)
        if "case compile" not in text:
            continue
        if "--out" not in text:
            rep.fail(f"{doc.name} 讲 case compile 却没提 --out（读者会把 --json 外壳当计划）")
        else:
            rep.ok(f"{doc.name} 用 --out 落 plan")

    # 反例（「别用 `case compile --json > 文件`」）是允许的：这类行带劝阻词
    bad = re.compile(r"case compile[^\n]*--json\s*>")
    corpus = [SKILL_ROOT / "SKILL.md", *sorted(REFS.glob("*.md"))]
    hits = []
    for path in corpus:
        for line in read(path).splitlines():
            if bad.search(line) and not any(word in line for word in ("别用", "不要", "错", "别")):
                hits.append(f"{path.name}: {line.strip()[:60]}")
    if hits:
        rep.fail(f"这些文档还在教 `case compile --json > 文件`：{'；'.join(hits)}")
    else:
        rep.ok("没有文档教 `--json > 文件`")

    if "_write_plan_file" not in read(SRC / "cli.py"):
        rep.fail("cli.py 里找不到 _write_plan_file；--out 的落盘口径可能变了")
    else:
        rep.ok("cli.py 仍有 --out 落盘实现")


def check_no_runtime(rep: Report) -> None:
    """包里不许混进运行产物。

    运行产物（录像、report.html、trace.jsonl、snapshot、截图、plans/）是**某一次运行**的结果：
    既不该进版本库，也不该随 skill 发出去——它们会过时、会让包体积膨胀，还会让人以为那是权威结果。
    曾经 `kernel/plans/baiwang.json` 就这样被当示例提交过。
    """
    print("包装：skill 包里有没有混进运行产物")
    banned_suffix = {".json", ".jsonl", ".log", ".png", ".jpg", ".jpeg", ".webp", ".zip", ".db", ".sqlite"}
    banned_dirs = {"outputs", "snapshots", "screenshots", "bridge", ".cache", ".profiles", "runs", "plans"}
    owners = ("src", "scripts", "examples", "tests")
    # 本机安装/运行目录：bootstrap 装进来的，不属于「随包发布的内容」
    local = {".venv", ".browsers", ".artifacts", "runs", ".pytest_cache", ".ruff_cache", "__pycache__"}
    strays: list[str] = []
    for path in SKILL_ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(SKILL_ROOT)
        if any(part in local for part in rel.parts):
            continue
        # kernel/src 是源码，那里可以有叫 bridge/ 的包；产物目录只在源码树之外算
        in_source = rel.parts[:3] == ("kernel", "src", "ui_agent")
        if not in_source and any(part in banned_dirs for part in rel.parts):
            strays.append(str(rel))
            continue
        if rel.parts[0] not in ("kernel", "scripts", "references") and rel.name != "SKILL.md":
            continue
        if path.suffix.lower() in banned_suffix and rel.parts[:2] != ("kernel",) + owners:
            strays.append(str(rel))
    if strays:
        rep.fail(f"skill 包里混进了运行产物：{'、'.join(strays[:5])}")
    else:
        rep.ok("skill 包里没有运行产物")


def check_scripts(rep: Report) -> None:
    """scripts/ 里的每个脚本都得在 SKILL.md 里点名，文档提到的脚本都得真存在。

    这一组是为「加了脚本忘了写文档」和「文档写了还没做」两个方向设的。历史上两种都发生过：
    `host_loop.py` 写好了但文档只字未提；`ask.py` 文档里已经教人用了，脚本还没提交。
    """
    print("脚本：scripts/ 与文档是否对得上")
    scripts = sorted(p.name for p in (SKILL_ROOT / "scripts").glob("*") if p.is_file())
    if not scripts:
        rep.fail("scripts/ 是空的")
        return
    skill = read(SKILL_ROOT / "SKILL.md")
    for name in scripts:
        if name in skill:
            rep.ok(f"SKILL.md 提到了 scripts/{name}")
        else:
            rep.fail(f"scripts/{name} 没在 SKILL.md 出现（宿主不会知道有这个脚本）")

    # 反向：文档里写 `scripts/xxx.py` 的，文件得在（本包 scripts/ 或 kernel/scripts/ 都算）。
    # smoke_browser.py 是内核自检脚本，文档教的是「在 kernel/ 下跑 scripts/smoke_browser.py」。
    pattern = re.compile(r"(?<!kernel/)(?<!/)scripts/([A-Za-z0-9_.-]+\.(?:py|sh|ps1))")
    mentioned = set(pattern.findall(skill))
    for doc in REFS.glob("*.md"):
        mentioned.update(pattern.findall(read(doc)))
    for name in sorted(mentioned):
        if (SKILL_ROOT / "scripts" / name).is_file() or (KERNEL / "scripts" / name).is_file():
            rep.ok(f"文档提到的 scripts/{name} 存在")
        else:
            rep.fail(f"文档提到 scripts/{name}，但本包 scripts/ 与 kernel/scripts/ 都没有")

    # stdlib-only 是这几类的硬约束：宿主机器不一定装过内核的依赖。
    # 用 AST 判 import，别用字符串匹配——这个文件自己的检查逻辑里就有 "import ui_agent" 字样。
    for name in ("ask.py", "host_loop.py", "check_docs.py", "explore_policy.py"):
        path = SKILL_ROOT / "scripts" / name
        if not path.is_file():
            continue
        if _imports_kernel(path):
            rep.fail(f"scripts/{name} import 了内核；宿主机器上不一定装得上")
        else:
            rep.ok(f"scripts/{name} 不 import 内核")


def _imports_kernel(path: Path) -> bool:
    """这个脚本有没有 import ui_agent（含 `from ui_agent.x import y`）。"""
    tree = ast.parse(read(path), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == "ui_agent" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root == "ui_agent":
                return True
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="check_docs", description="校验 skill 文档与内核源码一致")
    ap.add_argument("-v", "--verbose", action="store_true", help="列出每一条通过项")
    args = ap.parse_args(argv)

    if not (SRC / "cli.py").is_file():
        raise SystemExit(f"✗ 找不到内核源码：{SRC}（skill 包应自带 kernel/）")

    rep = Report(args.verbose)
    check_vocab(rep)
    check_facts(rep)
    check_cli(rep)
    check_links(rep)
    check_paths(rep)
    check_outputs(rep)
    check_plan_file(rep)
    check_no_runtime(rep)
    check_scripts(rep)

    print()
    if rep.bad:
        print(f"✗ {len(rep.bad)} 处不一致（另有 {rep.good} 项通过）")
        return 1
    print(f"✓ 文档与内核一致（{rep.good} 项检查全过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
