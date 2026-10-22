---
name: ui-agent
description: 用自然语言驱动真实 Chromium 跑 Web UI 测试，并把跑通的那一次录成零 token 回放。内核自己不做决策——它把「每一步点什么」写成 ask-*.json 问宿主对话大模型，宿主答 ans-*.json，浏览器动作由 Playwright 执行、断言由 DOM 独立校验。本 skill 自带内核，装进 skill 目录即可用，无需另装；用例不放在这里，放在用户自己的 git 仓库。用于：给一个网址和一句目标就自动点完一个业务流程；把流程固化成 YAML 用例反复跑；界面大改后重录；CI 里零模型复跑；iframe / shadow DOM / 新标签 / 原生对话框里点控件；或临时探索一个不熟悉的页面看它能点什么。会消耗模型 token，除非跑的是已录好的回放。
---

# ui-agent：让宿主模型开浏览器，把跑通的路径录成回放

**这个文件夹是自足的**：文档 + 内核在一起，整个拷进 skill 目录就能跑，不用另装别的东西。

```
ui-agent/
  SKILL.md          ← 你正在读
  references/       12 篇，按下面的路由表挑
  scripts/          bootstrap.ps1 / bootstrap.sh（装依赖）
                    host_loop.py（宿主循环参考实现）
                    ask.py（压缩查看并作答一条桥问答）
                    explore_policy.py（探索用的通用策略：点一次提交按钮就收）
                    parallel_run.py（多路并行编排）
                    check_docs.py（文档与内核一致性校验）
  kernel/           内核本体（自己的一份，独立虚拟环境）
```

**用例不在这里。** 用例库是用户自己的一个 git 仓库，位置由用户给（见 [references/cases-repo.md](references/cases-repo.md)）。

所有命令都在 **`kernel/` 目录下**执行，或者用 `uv run --project <本包>/kernel ui-agent ...` 从任意目录调。

运行产物（`runs/`、`cache/`、登录态 profile）默认写到 `kernel/.artifacts/`。
**每次运行都该显式给 `--run-dir`，指到用例库那个场景的 `outputs/` 下**，否则产物散在 skill 包里，
既进不了版本控制，也会在升级 skill 时丢掉。

三层分工，任何时候都别混：

| 层 | 谁做 | 说明 |
|---|---|---|
| 编排 / 裁决 | **你**（宿主对话模型） | 决定走哪一步、卡住了怎么办、这次算不算过 |
| 决策 | Jev（HTTP）**或** 你（走文件桥）**或** 回放缓存 | 三选一，`--decider` 定 |
| 执行 | Playwright（内核，无 LLM） | 点、输、滚、传文件、读 DOM、跑断言 |

内核把「问模型」的位置做成文件桥：`<run-dir>/bridge/ask-0003.json` 等你写 `ans-0003.json`。
你是那个模型。**你不答，它就在 deadline（默认 180s）上超时停机。**

## Start here：按你手头的活挑一篇

| 你在做 | 读 |
|---|---|
| 第一次用，或换机器、装依赖、确认浏览器在 | [references/setup.md](references/setup.md) |
| 要建 / 接一个用例库，或决定产物放哪 | [references/cases-repo.md](references/cases-repo.md) |
| 写 / 改用例 YAML，或 `case compile` 报错 | [references/cases.md](references/cases.md) |
| 起一次 run，然后循环 `bridge wait` → 处置 → `bridge answer` | [references/running.md](references/running.md) |
| 收到 `kind=supervise` 的 ask，要选 HINT/RECOVER/REPLAN/ABORT | [references/supervising.md](references/supervising.md) |
| 收到 `kind=decide` 的 ask，要答 operation + target | [references/deciding.md](references/deciding.md) |
| 还没有用例，要先探索页面 | [references/exploring.md](references/exploring.md)（配套 `scripts/explore_policy.py`） |
| 控件在 iframe / shadow DOM 里，或被遮挡、开新标签、弹原生对话框 | [references/m3.md](references/m3.md) |
| 跑挂了：`stuck` / `check_failed` / `cache_missed` / 回放对不上 | [references/debugging.md](references/debugging.md) |
| 要调默认值，或者不知道哪个环境变量管这件事 | [references/envs.md](references/envs.md) |
| 要处理密码、`--var`、产物落盘、能不能贴日志 | [references/security.md](references/security.md) |
| 要同时跑好几个用例，或者想开子 agent 并行 | [references/parallel.md](references/parallel.md) |

环境没装好就别往下读——一条命令先自检：

```powershell
.\scripts\bootstrap.ps1          # Windows
```
```bash
./scripts/bootstrap.sh           # macOS / Linux
```

幂等（已装的跳过），退出码 0 = 就绪。`-CheckOnly` / `--check` 只报告、不写盘。
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
11. **产物写进用例库的 `outputs/`，不写进本包。** 每次运行显式 `--run-dir`；`outputs/` 默认不进版本库。
12. **并行时每路一个浏览器、一个 profile、一个产物目录。** 共用 profile 会让后起的那路起不来
    （Chromium 单实例锁）；共用 `--run-dir` 会让两路的产物互相覆盖。详见 [parallel.md](references/parallel.md)。

## 并行：一次跑多个用例

单进程一次只跑一个用例。用例之间没有依赖时，并行能成倍缩短反馈时间——但要守两条：

- **每路一个 `--profile-dir`**：Chromium 对 `user-data-dir` 加单实例锁，共用目录时后起的进程直接失败。
- **每路一个 `--run-dir`**：桥目录跟着 `--run-dir` 走，不撞路径就不会答错 lane。

两种并行方式：

| 方式 | 适合 | 怎么做 |
|---|---|---|
| `scripts/parallel_run.py` | 策略已确定、能机械作答（比如回放或离线夹具） | 写一份 `jobs.json`，`--max-parallel N` 一次拉起 |
| **子 agent** | 需要你（模型）逐步决策 | 每个子 agent 跑一个用例，各自维护自己的 `bridge wait` 循环 |

**你可以创建子 agent 并行工作。** 并发数上限**跟随宿主给的上限**：宿主允许几个就最多开几个，
不要超；宿主没开这个能力就老实串行。每个子 agent 的指令里必须明确给出它自己的
`--run-dir` 与 `--profile-dir`（具体绝对路径），并要求它只回 `status`、断言结果、产物目录
——**别把 trace 全文回传**，三路 trace 会把主上下文撑爆。

细节与症状对照见 [references/parallel.md](references/parallel.md)。

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

## 文件桥还是 MCP：默认走文件桥

内核把「问模型」的位置抽象成一层协议，两种壳都能接：

| | 文件桥（默认） | MCP（可选） |
|---|---|---|
| 你怎么用 | `ui-agent run ...` 后台起进程，再 `bridge wait` / `bridge answer` | MCP server 暴露 `ui_agent_start` / `next` / `answer` / `status` / `abort` |
| 前置条件 | 能跑后台命令 | `uv sync --extra mcp`，且宿主支持 MCP |
| 参数面 | 完整（`run` 的全部开关） | 一部分（起 run 的那几个） |
| 超时 | 你自己控（`bridge wait --timeout`） | server 侧有上限 |
| 适合 | 绝大多数情况 | 宿主跑不了后台进程，或连 shell 引号都难传 |

**默认走文件桥**：它的参数面是全的，你能设 `--relearn`、`--mask-vars`、五个开销旋钮、`--check`，
也能在 run 进行到一半时读 `ask-*.json` 复盘。MCP 壳只是同一套协议的另一层包装，能力是子集。

什么时候可以考虑 MCP（由你判断，不要默认开）：

| 情形 | 用 MCP |
|---|---|
| 宿主**起不了后台进程**（只允许一次一个工具调用，内核会被掐死） | ✅ 唯一解 |
| 传参要过一层难搞的 shell（编码、引号、中文 `--var` 被解成乱码） | ✅ 省事 |
| 要在同一个会话里管多个 run，靠 server 的 `status` / `abort` | ✅ 顺手 |
| 需要用 `--relearn` / `--mask-vars` / 开销旋钮 / `--check` | ⚠ 得绕 `extra_args` 透传 |
| 想中途读 `ask-*.json` 复盘、或进程崩了要接着答 | ❌ 文件桥 |

MCP 装了之后（`uv sync --extra mcp`，`python -m ui_agent.mcp_server`）：
`ui_agent_start` 的 `extra_args` 可以透传其余 `run` 开关，例如
`{"plan_file": "...", "extra_args": ["--relearn", "--settle-ms", "1500"]}`；
但 `--run-dir` / `--decider` / `--supervisor` / `--plan-file` / `--goal` / `--goal-file` /
`--bridge-timeout` / `--json` 会被拒——这些是 server 自己装配桥接线用的。
`ui_agent_next` 默认等 240s（与 `bridge wait` 同口径），上限 300s。

## 六步工作流

```
⓪ 先探路（可选）  页面不熟就用 explore 走一遍：不录制、不看断言，只导出轨迹 + 用例骨架
                → <run-dir>/explore/{trajectory.md, explored.yaml}，见 references/exploring.md
① 环境自检      uv run ui-agent --help 能出子命令；.browsers 里有 chromium
② 定用例        在用户给的用例库里写 <场景>/<用例>.yaml  →  case compile 到 0 error
                →  --out 把 plan 落 <场景>/others/<用例>.plan.json
③ 定档          按上一节选四档之一；说清这次会不会花 token
④ 跑            后台起 run（--run-dir 指到 <场景>/outputs/<时间戳>） → 循环 bridge wait
                → 处置 ask → bridge answer → 等 finished
⑤ 汇报          读 result.json + summary.json：status、每步断言、缺陷候选；status=ok 才算过
⑥ 存档          录制成功 ⇒ 下次同命令加 --decider off --supervisor off 即可零 token 复跑
                用例 YAML + plan.json + 录像进库，outputs/ 默认不进
```

第 ② 步的 YAML 是唯一真源；`.plan.json` 是派生物，可随时由 YAML 重建，不手改。

**⓪ 不要跳**：不熟的页面凭想象写 `goal` 和断言，九成要在 ④ 里返工重录（每次重录都重新烧一遍 token）。
探索的成本只是一次单目标运行，而且**不产生录像**——所以它永远不会污染回放。
反过来，如果页面你已经很熟、或者已有能用的录像，就别 explore，直接写 YAML 跑。

探索阶段**每次点哪儿都取决于刚看到的页面**，所以别急着写 policy，用 `scripts/ask.py` 一问一答：

```bash
python scripts/ask.py show --run-dir "$OUT" --grep "查询"   # 看当前待答什么、候选里哪个对
python scripts/ask.py answer --run-dir "$OUT" --op CLICK --target 36
```

原始 `ask-*.json` 可以到 300 KB（250 个元素的整页快照），**永远不要直接读它**——
`ask.py show` 会把它压成几十行摘要，`ask.py answer` 会自己造一份合法答复。

## 快速开始

### 第一步：不联网、零 token 确认装好了

三条都不碰模型，一路验到「浏览器起得来、DOM 读得到」：

```bash
# 都在 kernel/ 目录下执行
uv run ui-agent --help                                                    # 子命令齐全
uv run python scripts/smoke_browser.py                                    # 浏览器起得来、DOM 读得到
uv run ui-agent snapshot --url "$FIXTURE" --json                          # → element_count: 18
```

`$FIXTURE` 是包内自带的离线页面 `kernel/examples/local_fixture.html`——**它只是一个测试页，不是用例**。

本地文件必须是**合法的 file URL**：`file:///` + 绝对路径 + **正斜杠**。拼错了 Chromium 会报
`navigating to "file:////d/..."` 并抛 `net::ERR_FILE_NOT_FOUND`。三种 shell 的正确拼法：

| shell | 拼法（在 `kernel/` 里） |
|---|---|
| PowerShell | `$FIXTURE = "file:///" + ((Get-Location).Path -replace '\\','/') + "/examples/local_fixture.html"` |
| Git Bash（Windows） | `FIXTURE="file:///$(cygpath -m "$(pwd)")/examples/local_fixture.html"` |
| Linux / macOS | `FIXTURE="file://$(pwd)/examples/local_fixture.html"` |

**别用 `file:///$PWD`**：Git Bash 的 `$PWD` 是 `/d/...`，拼出来 `file:////d/...`（四斜杠）打不开。

### 第二步：写第一个用例，录一次，之后永久免费

用例放在**你自己的用例库**里（还没有库？见 [cases-repo.md](references/cases-repo.md)）。
下面这段存成 `<用例库>/demo/login-query.yaml`——这就是用例的完整形状，也是文档里唯一的示例结构：

```yaml
id: login-query                 # 缓存身份：定了就别改，改了等于换一份录像
title: 登录后查询
notes: 示例结构。页面是任意一个「账号 + 密码 + 确定 + 查询」的界面。

steps:
  - goal: 在「账号」和「密码」里填入给定值，然后点击「确 定」
    checks:
      - element_exists: 查询
  - goal: 点击「查询」，等结果出现
    checks:
      - text_contains: 查询完成
```

跑之前先把 `账号` / `密码` 留在命令行（**YAML 里不写凭据**）：

```bash
# ① 编译到 0 error，同时把 plan 落到 others/（--out 写的是 plan 本体，能直接喂 --plan-file）
uv run ui-agent case compile --in "$CASE" --out "$CASE_DIR/others/login-query.plan.json" --json
# ② 首次录制（会问你 ask，见 references/running.md；宿主循环可直接用 scripts/host_loop.py）
uv run ui-agent run --plan-file "$CASE_DIR/others/login-query.plan.json" \
  --url "$URL" --decider agent --supervisor off --relearn \
  --var 账号=demo --var 密码=demo \
  --run-dir "$OUT" --json
# ③ 之后每次零 token 复跑：只改 --decider
uv run ui-agent run --plan-file "$CASE_DIR/others/login-query.plan.json" \
  --url "$URL" --decider off --supervisor off \
  --var 账号=demo --var 密码=demo \
  --run-dir "$OUT" --json
```

**别用 `case compile --json > plan.json`**：`--json` 打的是外壳
`{"ok":…,"errors":…,"warnings":…,"plan":{…}}`，`run --plan-file` 只吃里面的 `plan` 本体。
`--out` 就是为这一步准备的，它写出来的文件与 `plan` 字段逐字节相同。
（PowerShell 的 `>` / `Set-Content -Encoding UTF8` 还会加 BOM；内核现在读侧能容忍 BOM，但没必要绕这一圈。）

想先拿包内的离线夹具把这条流程走通，把 `$URL` 设成上面的 `$FIXTURE` 即可——夹具正好是
「账号 + 密码 + 确定 + 查询」的形状，两条命令都能跑过。

**`--var` 每次都要带同样的键**：录像只认动作序列，不认变量值（回放时把
`账号=demo` 换成 `账号=other` 照样命中）；但**缺了那个键**，输入动作当场判不成立，
录像被当失败牵连淘汰（`write: evicted`，之后变 `no-entry`）。回放还要用**同一个 `--url`**——键里含环境地址。

判断回放真的没调模型：根 `trace.jsonl` 里每行 `decision` 的 `model == "cache"`，
且倒数第二行是 `cache` 行、`hits > 0`、`misses == 0`。
（`summary.json` / `result.json` **不含** cache 统计，别在那儿找。）

## 命令面

| 子命令 | 干什么 | 会花 token |
|---|---|---|
| `run` | 跑一个 goal 或 plan，读写录像 | 除 `--decider off` 外都会 |
| `explore` | 只给目标让 agent 自己走，导出轨迹 + 用例骨架 | 会（默认 `--decider agent`） |
| `case compile` | 校验用例 YAML → plan | 不会（纯本地） |
| `case draft` | 自然语言 → 用例草稿（走 HTTP 文本模型） | 会 |
| `plan` | 自然语言流程 → 分步计划（走 HTTP 文本模型） | 会 |
| `snapshot` | 只快照不动作，看喂给模型的 state | 不会 |
| `report` | 按 run 目录重出 HTML 报告 | 不会 |
| `bridge wait` | 等下一个 ask / heartbeat / 终态 | 不会 |
| `bridge answer` | 落一份答复 | 不会 |

`case draft` 与 `plan` 是**另一条路**：它们让 HTTP 文本模型写用例/计划，和「你自己写 YAML」互斥。
主路线是你亲手写 YAML（断言必须由懂业务的人定），这两个只在「先要个草稿」时用，产出仍要人工审。
它们是**唯一用到 `--desc` / `--desc-file`** 的两个子命令：`case draft --desc "自然语言描述" --url <入口>`，
`plan --desc-file goal.txt --url <入口> --out <落点.json>`。长文本走 `--desc-file`，避免 shell 引号。

其余几个不常用但存在的开关：

| 开关 | 在哪 | 干什么 |
|---|---|---|
| `--goal-file` | `run` / `explore` | 目标太长就写进文件；与 `--goal` 互斥 |
| `--task` | `run` | run 目录名的前缀（默认 `task`）。想看产物目录名叫得有意义就设它 |
| `--profile-dir` | `run` / `explore` / `snapshot` | 登录态 profile 目录。**并发时每路必须不同**，否则后起的进程被 Chromium 单实例锁挡住 |
| `--note` | `bridge answer` | 给这条答复留一句备注，写进 `ans-*.json`，事后审计用 |

`run` 的三个入口互斥：`--goal` / `--goal-file` / `--plan-file`，必须给一个。
`bridge` 子命令走薄入口，冷启动约 0.1s；`run`/`explore` 连带 playwright，约 0.8s。
宿主会调 `bridge` 几十上百次，所以两者刻意分开。

退出码：`0` 成功、`1` 用例失败（`--json` 仍有效）、`2` 内核/参数错误。

## 自检这份文档有没有跟内核脱节

内核改了、文档没改，是最难发现的错。一条命令能查出来：

```bash
python scripts/check_docs.py        # 只需要 python 3.9+，不 import 内核
```

它从源码里抽词表（8 种断言、11 个停机状态、缓存词表、13 种操作、全部 CLI 开关），
逐条确认文档写了；再检查 SKILL.md 的路由表覆盖了每一篇 references、没有断链、
没有只对开发树成立的路径、包里没有混进用例。退出码 0 = 一致。
