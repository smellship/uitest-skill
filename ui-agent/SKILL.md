---
name: ui-agent
description: 用自然语言驱动真实 Chromium 跑 Web UI 测试，并把跑通的那一次录成零 token 回放。内核自己不做决策——它把「每一步点什么」写成 ask-*.json 问宿主对话大模型，宿主答 ans-*.json，浏览器动作由 Playwright 执行、断言由 DOM 独立校验。本 skill 自带内核，装进 skill 目录即可用，无需另装。用于：给一个网址和一句目标就自动点完一个业务流程；把流程固化成 YAML 用例反复跑；界面大改后重录；CI 里零模型复跑；或临时探索一个不熟悉的页面看它能点什么。会消耗模型 token，除非跑的是已录好的回放。
---

# ui-agent：让宿主模型开浏览器，把跑通的路径录成回放

**这个文件夹是自足的**：文档 + 内核 + 样例用例在一起，整个拷进 skill 目录就能跑，不用另装别的东西。

```
ui-agent/
  SKILL.md          ← 你正在读
  references/       8 篇，按下面的路由表挑
  scripts/          bootstrap.ps1 / bootstrap.sh（幂等自检 + 装依赖）
  kernel/           内核本体（自己的一份，独立虚拟环境）
  examples/cases/   两个能直接跑的样例：offline（离线夹具，不联网）+ big-fish（真实站点）
```

所有命令都在 **`kernel/` 目录下**执行，或者用 `uv run --project <本包>/kernel ui-agent ...` 从任意目录调。
**用例放哪由你定**——内核不要求用例在包内，`--in` / `--plan-file` 收绝对路径或任意相对路径。

运行产物（`runs/`、`cache/`、登录态 profile）默认写到 `kernel/.artifacts/`。
skill 目录只读、或你想让产物集中到别处，就设 `UI_AGENT_ARTIFACTS_DIR=<可写目录>`。

三层分工，任何时候都别混：

| 层 | 谁做 | 说明 |
|---|---|---|
| 编排 / 裁决 | **你**（宿主对话模型） | 决定走哪一步、卡住了怎么办、这次算不算过 |
| 决策 | Jev（HTTP）**或** 你（走文件桥）**或** 回放缓存 | 二选一，`--decider` 定 |
| 执行 | Playwright（内核，无 LLM） | 点、输、滚、传文件、读 DOM、跑断言 |

内核把「问模型」的位置做成文件桥：`<run-dir>/bridge/ask-0003.json` 等你写 `ans-0003.json`。
你是那个模型。**你不答，它就在 deadline（默认 180s）上超时停机。**

## Start here：按你手头的活挑一篇

| 你在做 | 读 |
|---|---|
| 第一次用，或换机器、装依赖、确认浏览器在 | [references/setup.md](references/setup.md) |
| 写 / 改用例 YAML，或 `case compile` 报错 | [references/cases.md](references/cases.md) |
| 起一次 run，然后循环 `bridge wait` → 处置 → `bridge answer` | [references/running.md](references/running.md) |
| 收到 `kind=supervise` 的 ask，要选 HINT/RECOVER/REPLAN/ABORT | [references/supervising.md](references/supervising.md) |
| 收到 `kind=decide` 的 ask，要答 operation + target | [references/deciding.md](references/deciding.md) |
| 还没有用例，要先探索页面 | [references/exploring.md](references/exploring.md) |
| 跑挂了：`stuck` / `check_failed` / `cache_missed` / 回放对不上 | [references/debugging.md](references/debugging.md) |
| 要处理密码、`--var`、产物落盘、能不能贴日志 | [references/security.md](references/security.md) |

环境没装好就别往下读——一条命令先自检：

```powershell
.\scripts\bootstrap.ps1          # Windows
```
```bash
./scripts/bootstrap.sh           # macOS / Linux
```

幂等（已装的跳过），退出码 0 = 就绪。`-CheckOnly` / `--check` 只报告。
装的东西只落在本包内：`kernel/.venv`（依赖）与 `kernel/.browsers`（Chromium，约 400 MB）。

## 铁律

违反任何一条都会得到「看起来跑通了但没验过」的结果——那比失败更糟。

1. **`DONE` 不是成功证据。** 成功只由断言（checks）对 DOM 校验决定。没有断言 = `done_unverified`，不许跟用户说「过了」。
2. **桥答复等同模型输出，同等不可信。** 元素只能用**当次 ask 的 state.elements 里的 `index`**；
   永远不要给 selector、坐标、XPath、可执行 JS —— 它们会被校验拒掉，或者更糟：指向别的东西。
3. **一次 ask 只答一次。** 重复答、改答都是 stale，第一个落盘的生效。答之前把 payload 读完。
4. **`operation` 答案的 `choice` 必须是该 question 的 `criteria` 键**，不是标签、不是中文描述。
   `probabilities` 必须覆盖**该 question 的全部** criteria 键，和≈1，被选中的那个概率最大。
5. **`TYPE_TEXT` 的值你不该编。** 值只能来自 `--var`（优先）或宿主作答；**secret 字段只能 `--var`**，桥路径上内核直接拒绝询问。
6. **RECOVER 不许用 `CLICK_FORCE` / `TYPE_TEXT` / `UPLOAD`。** 前者是缺陷路径，后两者是「值不能现场编」。
7. **探索（explore）不录制。** 探索会点开用不到的页面；录进去，回放时就会重放垃圾动作。要录像必须另起一次 `run`。
8. **断言要能区分「做之前」和「做之后」。** 做完才为真的才算断言；点过的那个控件还在页面上，证明不了任何事。
9. **密钥只进环境变量 / `--var`，不进 goal、不进 YAML 字面量、不进对话。**
10. **停下来了就是停下来了。** `blocked` / `stuck` / `cache_missed` / `aborted` 不是「再试一次就好」——
    读 `result.json` 的 `detail`，判断是改用例、改参数，还是这页面真做不到。

## 四档开关：先定这次谁花钱

| 档 | 开关 | 决策者 | 监督者 | 用途 |
|---|---|---|---|---|
| 全关 | `--decider off --supervisor off` | 回放缓存 | 无（失配即停） | **零 token 复跑**，CI 常态 |
| 只开 Jev | `--decider jev --supervisor off` | Jev | 无（阻断即停） | 首次录制，最省心的录制档 |
| 只开大模型 | `--decider agent --supervisor agent` | 你 | 你 | 没有 Jev key，或要你亲自掌舵 |
| 全开 | `--decider jev --supervisor agent` | Jev | 你 | 录制时 Jev 走、卡住你救 |

选档规则：

- 用户给了 Jev key、要录制 → **只开 Jev**（不要让 Jev 和你在同一个循环里抢方向盘）。
- 没有 Jev key → **只开大模型**，并在开跑前用一句话说清楚「这次会消耗你的 token」，然后直接开跑，不追问。
- 已经有能用的录像、只想验证 → **全关**。
- `--decider agent` / `--supervisor agent` **必须显式给 `--run-dir`**：内核要在起浏览器之前就知道桥目录。

## 六步工作流

```
① 环境自检      uv run ui-agent --help 能出子命令；.browsers 里有 chromium
② 定用例        cases/<场景>/<用例>.yaml  →  case compile 到 0 error  →  取 plan 落 others/*.plan.json
③ 定档          按上一节选四档之一；说清这次会不会花 token
④ 跑            后台起 run（--run-dir 显式） → 循环 bridge wait → 处置 ask → bridge answer → 等 finished
⑤ 汇报          读 result.json + summary.json：status、每步断言、缺陷候选；status=ok 才算过
⑥ 存档          录制成功 ⇒ 下次同命令加 --decider off --supervisor off 即可零 token 复跑
```

第 ② 步的 YAML 是唯一真源；`.plan.json` 是派生物，可随时重建，不手改。

## 快速开始

### 第一步：不联网、零 token 确认装好了

`examples/cases/offline.yaml` 指向包内自带的页面 `kernel/examples/local_fixture.html`。
下面三条都不花 token，能一路验到「浏览器起得来、DOM 读得到、用例编译过」：

```bash
# 都在 kernel/ 目录下执行
uv run ui-agent case compile --in ../examples/cases/offline.yaml --json   # → ok: true
uv run ui-agent snapshot --url "file:///D:/path/to/ui-agent/kernel/examples/local_fixture.html" --json  # → element_count: 18
uv run ui-agent snapshot --url https://example.com --json                 # → 验外网与浏览器
```

本地文件必须是**合法的 file URL**：`file:///` + 绝对路径 + **正斜杠**。拼错了 Chromium 会报
`navigating to "file:////d/..."` 并抛 `net::ERR_FILE_NOT_FOUND`。三种 shell 的正确拼法：

| shell | 拼法 |
|---|---|
| PowerShell | `"file:///" + ((Get-Location).Path -replace '\\','/') + "/examples/local_fixture.html"` |
| Git Bash（Windows） | `"file:///$(cygpath -m "$(pwd)")/examples/local_fixture.html"` |
| Linux / macOS | `"file://$(pwd)/examples/local_fixture.html"` |

**别用 `file:///$PWD`**：Git Bash 的 `$PWD` 是 `/d/...`，拼出来 `file:////d/...`（四斜杠）打不开。

### 第二步：录一次，之后永久免费

离线样例的完整闭环（实测 6 次决策：填账号 → 填密码 → 点确定 → 点查询 → DONE，第 2 步再 1 次 DONE；
录完就再也不用模型）。先把夹具地址存进变量，避免每行都写长路径：

```bash
# Git Bash（Windows）—— cygpath -m 给 D:/...，拼出来才是合法 URL
FIXTURE="file:///$(cygpath -m "$(pwd)")/examples/local_fixture.html"
```

```bash
# Linux / macOS
FIXTURE="file://$(pwd)/examples/local_fixture.html"
```

```powershell
# PowerShell
$FIXTURE = "file:///" + ((Get-Location).Path -replace '\\','/') + "/examples/local_fixture.html"
```

```bash
uv run ui-agent run --plan-file ../examples/cases/offline.plan.json \
  --url "$FIXTURE" \
  --decider agent --supervisor off --relearn \
  --var 账号=demo --var 密码=demo \
  --run-dir .artifacts/runs/offline-rec --json
```

`--decider agent` 会停下来等你答 `ask-*.json`——这就是 [references/running.md](references/running.md)
讲的 `bridge wait` / `bridge answer` 循环。录成功后再加 `--decider off` 跑同一条命令，就是 0 token 回放：

```bash
uv run ui-agent run --plan-file ../examples/cases/offline.plan.json \
  --url "$FIXTURE" \
  --decider off --supervisor off \
  --var 账号=demo --var 密码=demo \
  --run-dir .artifacts/runs/offline-rep --json
```

**`--var` 每次都带**：录像只认动作序列，不认变量值，但缺了 `--var` 会让输入动作判不成立，
录像被当失败牵连淘汰（`write: evicted`）。回放还要用**同一个 `--url`**——键里含环境地址。

真实站点的例子是 `examples/cases/big-fish.yaml`（必应搜索 + 翻页），把 `--decider agent` 换成
`--decider jev --url https://cn.bing.com/` 即可，其余同理。

判断回放真的没调模型：根 `trace.jsonl` 里每行 `decision` 的 `model == "cache"`，
且倒数第二行是 `cache` 行、`hits > 0`、`misses == 0`。
（`summary.json` / `result.json` **不含** cache 统计，别在那儿找。）

## 命令面

| 子命令 | 干什么 | 会花 token |
|---|---|---|
| `run` | 跑一个 goal 或 plan，读写录像 | 除 `--decider off` 外都会 |
| `explore` | 只给目标让 agent 自己走，导出轨迹 + 用例骨架 | 会（默认 `--decider agent`） |
| `case compile` | 校验用例 YAML → plan | 不会（纯本地） |
| `case draft` | 自然语言 → 用例草稿 | 会 |
| `plan` | 自然语言流程 → 分步计划 | 会 |
| `snapshot` | 只快照不动作，看喂给模型的 state | 不会 |
| `report` | 按 run 目录重出 HTML 报告 | 不会 |
| `bridge wait` | 等下一个 ask / heartbeat / 终态 | 不会 |
| `bridge answer` | 落一份答复 | 不会 |

`bridge` 子命令走薄入口，冷启动约 0.1s；`run`/`explore` 连带 playwright，约 0.8s。
宿主会调 `bridge` 几十上百次，所以两者刻意分开。

退出码：`0` 成功、`1` 用例失败（`--json` 仍有效）、`2` 内核/参数错误。
