# ui_agent

自然语言驱动的 UI 测试执行器：**LLM 编排与裁决，TypeSafe Jev 决策，Playwright 执行。**

用户只给一句自然语言描述流程；LLM 把它拆成分步计划（每步一个小 goal + 独立断言），
常态循环里由 Jev 看 DOM 快照选"操作 + 目标元素"，只有 Jev 卡住时才由 LLM 介入裁决
（HINT / RECOVER / REPLAN / ABORT）。

## 快速开始

```powershell
cd D:\test_skills\ui_agent
uv sync

# 浏览器装到工作区内置目录（首次）
. .\scripts\env.ps1
uv run playwright install chromium

# 无网络冒烟：验证双模式启动与 DOM 读取
uv run python scripts\smoke_browser.py            # 无头
uv run python scripts\smoke_browser.py headed     # 可视化（会弹窗几秒）

# 离线全链路冒烟：本地夹具跑 快照 → 守卫 → 点击阶梯 → 断言（零模型调用）
uv run python scripts\smoke_snapshot.py
uv run python scripts\smoke_snapshot.py --headed  # 可视化看它怎么点

# 离线多步冒烟：脚本化决策跑一条两步计划（一个会话、零模型调用）
uv run python scripts\smoke_plan.py
uv run python scripts\smoke_plan.py --headed

# 离线监督冒烟：脚本化监督模型跑三条升级路径（HINT / RECOVER / ABORT，零模型调用）
uv run python scripts\smoke_supervisor.py
uv run python scripts\smoke_supervisor.py --headed

# 离线上传冒烟：A（隐藏 input 注入 U1）与 C（页内合成拖拽 U2）两条通道 + 一条页面拒收负例
uv run python scripts\smoke_upload.py
uv run python scripts\smoke_upload.py --headed          # 可视化看拖拽与提交
uv run python scripts\smoke_upload.py --files a.png b.pdf   # 指定上传文件（默认复用已有截图）

# 离线结构冒烟：iframe / shadow DOM / 遮挡 / 新标签 / 原生对话框（本地 HTTP 服务，零模型调用）
uv run python scripts\smoke_m3.py
uv run python scripts\smoke_m3.py --headed --only tab   # 只看"新标签跟随"那一段

# 离线断言冒烟：计数 / 属性 / 轮询重试（含一条计数负例，本地 HTTP 服务，零模型调用）
uv run python scripts\smoke_assert.py
uv run python scripts\smoke_assert.py --headed --only poll   # 只看"轮询重试"那一段

# 离线契约冒烟：case compile / snapshot --json / run --json / report --json 的子进程级验证（零模型调用）
uv run python scripts\smoke_cli.py

# 离线回放冒烟：录制 → 零模型回放（真 CLI --decider off，全命中免模型）→ 失配停机（零模型调用）
uv run python scripts\smoke_replay.py

# Agent 桥冒烟：agent 决策 / 监督往返 / 零模型回放 / 超时停机（真 CLI + 真 Chromium + 假宿主）
uv run python scripts\smoke_bridge.py

# 探索全链路冒烟：探索 → 骨架被拒 → 补断言 → 录制 → 零 token 回放（零模型调用）
uv run python scripts\smoke_explore.py
```

> 冒烟会起真 Chromium。上一轮异常退出可能留下残留进程锁住 `.artifacts\chrome-profile`，
> 表现为所有冒烟都在启动浏览器时失败。先清一遍：
> `Get-Process chrome -ErrorAction SilentlyContinue | Stop-Process -Force`

## Agent 桥与四处开关（M-A）

宿主对话大模型可以接替四个 LLM 缝，**不需要任何模型 key**。通道是文件桥：
内核写 `<run-dir>\bridge\ask-*.json`，宿主写 `ans-*.json`。

### 四档开关

| 形态 | 开关 | 决策者 | 监督者 |
|---|---|---|---|
| 全关（零模型回放） | `--decider off --supervisor off` | 回放缓存 | 无（失配即停） |
| 只开 Jev | `--decider jev --supervisor off` | Jev（要 key） | 无 |
| 只开大模型 | `--decider agent --supervisor agent` | 对话大模型（桥） | 对话大模型（桥） |
| **全开（推荐）** | `--decider jev --supervisor agent` | **Jev** | **对话大模型（桥）** |

```powershell
# 后台起跑：agent 模式必须显式 --run-dir（宿主启动前就得知道桥在哪）
Start-Process -NoNewWindow uv -ArgumentList @(
  "run", "ui-agent", "--goal-file", "examples\goals\baiwang_full.txt",
  "--url", "<网址>", "--decider", "jev", "--supervisor", "agent",
  "--run-dir", ".artifacts\runs\my-run", "--json")

# 循环等事件（单次调用阻塞；到点返回 heartbeat，续等即可）
uv run ui-agent bridge wait --run-dir .artifacts\runs\my-run --timeout 240 --json
#   → {"ok":true,"event":"ask","kind":"supervise","ask":{...}}
#   → {"ok":true,"event":"heartbeat"}
#   → {"ok":true,"event":"finished","state":"finished|crashed","result":{...}}

# 作答（原子写入；晚到/重复返回 accepted=false, reason=stale）
uv run ui-agent bridge answer --run-dir .artifacts\runs\my-run --id 7 `
  --data '{\"answer\": {\"ruling\": \"HINT\", \"hint\": \"先展开高级查询再选行政区\"}}' --json
```

三种 ask 的答复契约：

| kind | 你会收到 | 你要回 |
|---|---|---|
| `supervise` | `stuck{trigger,detail}` / `current_goal` / `remaining_goals` / `state` | `{"ruling":"HINT\|RECOVER\|REPLAN\|ABORT", ...}`（过 `parse_ruling`） |
| `decide` | `state` / `questions` / 可能的 `previous_error` | `{"answers":{...}}`，或 `{"error":"..."}` 主动中止 |
| `text` | `goal` / `field` / `page` / `recent_actions` / `hint` | `{"text":"..."}`（非空、≤2000、无多余键） |

桥答复**与 HTTP 模型同等不信任**：答歪 → 重问一次 → 升级 / 按 M0 停机；
答超时（默认 180s，`UI_AGENT_BRIDGE_TIMEOUT_S` 或 `--bridge-timeout` 可调）也是 M0 停机。

## 探索出用例（explore → YAML → 录制 → 零 token）

给一句目标，让 agent 自己走一遍，把「走过什么」导出成可编辑的证据：

```powershell
# 1) 探索：可乱走，**不录制**（乱点的页面不该进回放）
uv run ui-agent explore --goal "清空全部模拟行并核对结果" --url <网址> `
  --decider agent --supervisor agent --run-dir .artifacts\runs\explore-1 --json
#   → <run-dir>\explore\ 下有 trajectory.md（逐步轨迹）、explored.yaml（用例骨架）、explore.json

# 2) 读轨迹，自己把 explored.yaml 的 checks: [] 补上（**不补就 compile 不过，这是故意的**）
uv run ui-agent case compile --in <用例库>\<场景>\my-case.yaml --out plan.json --json

# 3) 跑这份 YAML：这一次既录制，又证明断言真的成立
uv run ui-agent run --plan-file plan.json --url <网址> `
  --decider agent --supervisor agent --run-dir .artifacts\runs\record --json

# 4) 之后零 token 复跑
uv run ui-agent run --plan-file plan.json --url <网址> `
  --decider off --supervisor off --json
```

要点：

- **`id` 是缓存身份**。写了 `id:` 的用例，改 goal 措辞不会让录像失效；
  没写的旧用例仍走内容哈希（行为完全不变）。
- **改断言不删录像**：只收尾断言没过时录像保留（`check_failed`，退出码 1），
  改回/放宽断言即可继续回放；只有**动作执行失败**才淘汰录像。
  想主动重录加 `--relearn`。
- **token 账分两种**：Jev/HTTP 的 usage 内核能统计并打印；agent 模式的花费在宿主自己的循环里，
  内核只报「N 次桥问答」。
- **敏感字段只走 `--var`**：桥路径直接拒绝，不会向 agent 询问密码。

## 浏览器开销与产物脱敏（2026-10-19）

```powershell
# 回放提速：动作后等待从 700ms 降到 200ms（改完要重录录像，否则可能判 wrong-context）
uv run ui-agent run --plan-file plan.json --url <网址> `
  --decider off --supervisor off --settle-ms 200 --json

# 产物脱敏：非 secret 的 --var 值在快照/actions/trace/报告里也打成 ••••••
uv run ui-agent run --plan-file plan.json --url <网址> --mask-vars --json
```

| 开关 | 默认 | 作用 |
|---|---|---|
| `--settle-ms` | 700 | 动作后等待；回放提速首选 |
| `--slow-mo` | 250 | 有头时操作间隔（可视化才生效） |
| `--ready-stable-ms` | 2500 | 首帧就绪门：指纹安静多久算渲染完成 |
| `--ready-timeout-ms` | 20000 | 就绪门最长等待 |
| `--assert-timeout-ms` | 5000 | DONE 验收轮询窗口 |
| `--mask-vars` | 关 | 非 secret 的 --var 值落盘时掩码（录像里仍留真实值，否则回放会输错） |

**关于「值不落盘」的准确口径**：`--var` 的**非敏感**值默认确实会出现在产物里——因为页面会把输入框内容
如实回显，内核把它当页面事实记录（快照 `value`、`actions.json` 的 `text`、trace 的 `action.detail`）。
这是平台既有行为。**secret 字段（密码等）无论开关如何一律掩码**。需要连非敏感值一起藏就用 `--mask-vars`。

## MCP 适配器（可选）

```powershell
uv sync --extra mcp
uv run python -m ui_agent.mcp_server     # stdio server，供宿主注册
```

宿主配置（Claude Code / Cursor 等）：

```json
{
  "mcpServers": {
    "ui-agent": {
      "command": "uv",
      "args": ["run", "--directory", "<本仓库>", "python", "-m", "ui_agent.mcp_server"],
      "env": { "TYPESAFE_API_KEY": "<key>" }
    }
  }
}
```

工具：`ui_agent_start` / `ui_agent_next` / `ui_agent_answer` / `ui_agent_status` / `ui_agent_abort`。
与 `bridge wait/answer` **同一套语义**（同样的校验、预算、缓存、trace），只是传输从 CLI 换成 MCP 工具。
只有「不能跑后台命令」的宿主才需要它；有 shell 的宿主直接用 bridge 更省进程。

## skill 包：自足的一整份交付（M-B）

`../ui-agent/`（**与 `kernel/` 平级**）是给宿主 agent 用的发布形态。它**自足**：
文档 + 内核副本在一个目录里，整个丢进宿主的 skills 目录就能跑，
宿主不需要再去找开发树。宿主读 `SKILL.md` 决定读哪篇 reference。

**包里不带用例**：用例是用户自己的资产，放他们自己的 git 仓库（`<场景>/<用例>.yaml` +
`<场景>/others/<用例>.plan.json` + `<场景>/outputs/<时间戳>/`）。包里只有离线夹具
`kernel/examples/local_fixture.html`——那是一个可跑的测试页，不是用例。

```
ui-agent/
  SKILL.md       references/（11 篇）      scripts/{bootstrap.{ps1,sh},host_loop.py,check_docs.py}
  kernel/                  ← build_skill.py 生成的内核副本（src / examples / pyproject / uv.lock / …
                               不含 tests、.venv、.artifacts、.browsers）
```

```powershell
# 在 ui-agent/ 下一条命令装环境（幂等；已装的跳过）
cd ..\ui-agent
.\scripts\bootstrap.ps1
.\scripts\bootstrap.ps1 -CheckOnly    # 只报告，零写入
```

```bash
./scripts/bootstrap.sh --check        # macOS / Linux
```

它做四件事并逐条打印：查 `uv` → `uv sync` → 把 chromium 装进 `kernel/.browsers` → 验 `ui-agent --help` 出子命令。
退出码 0 = 就绪；非 0 会列出哪一步没过。**不碰密钥**，只报告 `.env` 里两个 key 配没配。

改完内核要同步这份副本，否则 skill 包里的是旧的：

```bash
uv run python scripts/build_skill.py            # 同步（先删 ui-agent/kernel 再重建）
uv run python scripts/build_skill.py --check    # 只报漂移，退出码 1
```

skill 内容（11 篇 reference + 双平台 bootstrap + 两个自检脚本）：

| 文件 | 讲什么 |
|---|---|
| `SKILL.md` | 路由表 + 十条铁律 + 四档开关 + 六步工作流 + 命令面 + 退出码 + 文件桥 vs MCP |
| `references/setup.md` | bootstrap / 浏览器目录 / 密钥 / 有头无头 / 常见环境错误 |
| `references/cases-repo.md` | 用例库怎么建：给用户要哪三件事、目录约定、outputs 时间戳、录像归属 |
| `references/cases.md` | 用例 YAML 写法、8 种断言、5 种算「业务结果证据」、编译错误码全集 |
| `references/running.md` | 后台起 run + `bridge wait`/`answer` 循环 + ask 处置表 + 读产物 |
| `references/supervising.md` | `kind=supervise` 作答契约：四裁决、可用恢复动作、预算、拒绝文案 |
| `references/deciding.md` | `kind=decide` 作答契约：13 种 operation、probabilities 六条硬规则、目标 head |
| `references/exploring.md` | explore 四段工作流、产物四件、骨架为什么故意过不了编译 |
| `references/m3.md` | iframe / shadow DOM / 遮挡 / 新标签 / 原生对话框：能做什么、边界在哪 |
| `references/debugging.md` | trace 词表、cache 词表、11 个停机状态、失配判定、开销旋钮 |
| `references/envs.md` | 全部环境变量与 CLI 开关的对应关系、默认值、常用组合 |
| `references/security.md` | secret 与非 secret 两条路、落盘边界、探索产物为什么只写来源 |
| `scripts/host_loop.py` | 宿主循环参考实现（标准库、不 import 内核、按 st_mtime 判 ask） |
| `scripts/check_docs.py` | 文档与内核一致性校验（词表 / 默认值 / 开关 / 断链 / 包内不许有用例） |

**planner 缝没有桥**：桥只覆盖 supervise / decide / text 三个缝；planner 由 agent 手写 YAML 替代
（HTTP 版 `plan` / `case draft` 仍保留给平台用）。

## 跑一个目标

```powershell
# 只看不点：把即将喂给 Jev 的 state 打出来（零模型调用）
uv run ui-agent snapshot --url http://example.com

# 跑目标：debug 模式可视化；断言用 --check 独立指定（DONE 不算证据）
uv run ui-agent run `
  --goal-file examples\goals\baiwang_login.txt `
  --url <网址> `
  --task baiwang-login --mode debug `
  --var 账号=<账号> --var 密码=<密码> `
  --check element_exists=行政区域

# 回归（无头）：同一份代码、同一个 Chromium 二进制
uv run ui-agent run --goal-file examples\goals\baiwang_full.txt --url <网址> --mode ci `
  --check element_value=行政区域|2102 大连 `
  --check text_contains=票据类型
```

示例目标文件里**不含任何真实凭据**：账号、密码、上传文件路径一律在运行时用 `--var` 注入
（值不落盘、不进任何模型调用），所以这些文件可以安全地进版本库。

断言写法：`kind=值`、`element_value=标签|期望值`、`element_count=标签|个数`（或只给个数），
或直接给 JSON（可带 `attr` / `timeout_ms`）。可用 kind：
`text_contains / url_contains / title_contains / element_exists / element_absent / element_value /
element_count / element_attribute`。

`element_count` 数「标签包含该值的元素个数」；`element_attribute` 读元素属性
（`{"kind": "element_attribute", "value": 标签, "attr": "href", "expected": "..."}`）。
DONE 后的验收**自动轮询**：默认窗口 `UI_AGENT_ASSERT_TIMEOUT_MS`（5000ms），单条可用
`timeout_ms` 加宽；页面慢半拍不再白跑一次模型往返，trace 的 `assert` 行记第几轮才成立。

## 跑一条计划（M1）

```powershell
# 1) 一段自然语言流程 → 分步计划（这一步会调用文本模型）
uv run ui-agent plan --desc "打开系统，用账号登录，行政区域选 2102 大连，点查询，看到票据列表" `
  --url <网址> --out plans\baiwang.json

# 2) 按计划执行：一个浏览器会话跑完所有步骤，每步独立断言，失败即停
uv run ui-agent run --plan-file plans\baiwang.json --url <网址> --mode debug

# 3) 重出报告（不需要重跑）
uv run ui-agent report --run-dir .artifacts\runs\<某次运行>
```

版本库里的计划文件**不写入口地址**（免把生产域名带进公开仓库），运行时用 `--url` 给；
忘了给会直接报「计划里没有 url，请用 --url 指定入口」，不会拿空地址去跑。

`--var 键=值` 可以给 `TYPE_TEXT` 字段直接供值（优先于文本模型，值不落盘、不进模型），
也可以给 `UPLOAD` 供**上传文件路径**（键名要出现在控件标签里，多文件用 `;` 分隔）；
适合口令等不便写进目标的场景；缺值且没有文本模型时，字段会被拒填并记为失败而不是猜一个值。

## 卡住了怎么办（M2）

常态循环不调 LLM；只有下面五种情况会停下来问一次监督模型（HINT / RECOVER / REPLAN / ABORT）：

| 触发点 | 什么时候 |
|---|---|
| `blocked` | Jev 说没有任何支持的操作能推进 |
| `repeat_no_progress` | 同一个决策连做 3 次且页面没有变化 |
| `low_confidence` | 连续 3 次决策置信度低于 `UI_AGENT_CONF_MIN` |
| `budget_actions` / `budget_decisions` | 动作或决策预算耗尽 |
| `check_failed` | 第一次 DONE 没通过断言（第二次直接判失败） |

裁决由代码裁决，不由模型自己保证：元素只能用当次快照里的 index，恢复动作只允许
`CLICK / SELECT / HOVER / SCROLL_TO / PRESS_KEY / SCROLL_UP / SCROLL_DOWN / WAIT`（不含 `CLICK_FORCE`
与 `TYPE_TEXT`），SELECT 的选项必须在该元素当次选项里且未禁用，REPLAN 的步骤要过与 planner 相同的断言检查。
RECOVER 的动作走和 Jev 决策完全相同的执行路径（守卫、降级阶梯、缺陷通报都照旧）。

预算：`SUPERVISOR_PER_STEP`（默认 2，每步重置）与 `SUPERVISOR_PER_RUN`（默认 6，整轮共享），
在调用前扣除，所以升级次数有限、循环必然终止。监督模型不可用（没 key、预算用完、回答不合规）
时行为与 M0 完全一致：按 M0 语义收尾。`--supervisor off` 或 `UI_AGENT_SUPERVISOR=false` 关掉大模型总闸：
升级点直接停（断言未过不再给第二次 DONE 机会），TYPE_TEXT 字段值也只认 `--var`。
决策层另有 `--decider`（或 `UI_AGENT_DECIDER`）：`jev`=Jev 决策（默认）、`llm`=大模型代班 Jev（快路径不开思考）、
`off`=关闭决策（只认回放缓存，没有可回放的记录就按 `cache_missed` 停）。

计划模式下 REPLAN 会把**剩余步骤整体换成新序列**（原计划留在 `plan.json`，新计划写 `plan.final.json`，
重试的那一步另开 `-r2` 目录，`summary.json` 里有 `replans` 记录）；单目标模式下 REPLAN 只会停在
`replanned` 状态并提示改用计划模式——不会假装成功。

## 回放缓存（零模型回归）

同一条用例在同一环境跑过后，决策与收尾结论会按「用例哈希 + 环境地址」建键录下来：下次整步命中就回放，
**零模型调用**；命中不了才交还模型。录像按起点上下文（URL 路径 + 标题）分成变体：回放自动挑与当前
页面相符的那条（后录优先），选不中整步交还；同一用例的不同初始状态（如登录页 / 已登录页）各录一条变体。
回放不是另走一套：元素按语义重定位（role+label 精确 → 唯一标签兜底），页面上下文守卫不符即交还；
回放出的收尾结论仍要过独立断言。回放出的记录没能成立（动作执行失败 / 断言没过）时，本步剩余录像
整体作废交还模型（后面的记录都建立在前面成立的前提上）；整步走完且结论成立才把新录像并入对应变体
（自愈回填 + 防投毒：失败只淘汰回放过的那条变体，且失败那步绝不落盘）。

开关：`--cache off`（`UI_AGENT_CACHE=false`）本次运行不碰缓存；`--cache-strict`
（`UI_AGENT_CACHE_STRICT=true`）严判——命中的记录还须状态签名一致，页面悄悄变了也能看出来。
配合 `--decider off` 与 `--supervisor off` 整轮零模型：全命中直接过，失配即 `cache_missed` 停机，
并报出原因（no-entry / no-variant / wrong-context / invalid-entry / truncated / retry）。`trace.jsonl` 多一行
运行级 `kind="cache"` 事实：模式（self-finalized / agent-concluded / missed）、写入结果
（saved / kept / no-change / unconfirmed / evicted / not-written）、命中/失配与逐类步骤计数。

## 上传文件（拖拽 / 点选）

`UPLOAD` 操作的目标只能是**文件控件**（role `file`，快照里带 `accept` / `multiple` 事实）；
被 `display:none` 藏起来也算，因为真实用户入口就是那个按钮或拖拽框。走哪条通道由**配置**决定，
不由模型选，也不做跨通道静默回退：

| 通道 | 开关 | 阶梯级别 | 机制 |
|---|---|---|---|
| A：隐藏 input 注入 | `--upload-mode input`（默认）/ `UI_AGENT_UPLOAD_MODE=input` | `U1` | `set_input_files`，协议层设置 FileList |
| C：页内合成拖拽 | `--upload-mode drop` | `U2` | 页内造 `DataTransfer` + `dragenter/dragover/drop`（`isTrusted=false`，属已知限制） |

文件路径只来自 `--var`（键名要出现在控件标签里，多文件用 `;` 分隔），**没有模型兜底**：匹配不上就干净地拒绝
（"未执行上传"）而不是猜一个路径。执行前只做确定性预检（文件存在、扩展名/MIME 对得上 `accept`、单文件控件不给多个），
大小与数量上限属于**页面自己的策略**，用断言判（本地夹具与百望页面都把它们写在页面上）。

`UPLOAD` 常是两段式：先进入待提交列表，再点提交才算数——所以验收要看提交后的文案（如 `text_contains=提交成功`），
以及待提交列表里的**文件名**。产物里只落**文件名 + 大小 + 变量键名**（trace 的 `upload` 字段），本地绝对路径不落盘、不进模型。

本地夹具 `examples/local_fixture.html` 复刻了百望上传页的规则（pdf/jpg/jpeg/png、图片 ≤ 20M、PDF ≤ 10M、≤ 50 个），
`scripts/smoke_upload.py` 用真实 Chromium 跑 A、C 两条通道各一次，外加一条负例（往未声明 `accept` 的控件传 `.txt`，
页面必须自己拒收并给出文案），全程零模型调用。

## 页面结构支持（M3）：iframe / shadow DOM / 新标签 / 原生对话框

| 结构 | 行为 | 边界 |
|---|---|---|
| iframe | 快照逐帧收集（元素带 `frame` 事实）；索引 → locator 在元素所属帧内解析，动作（含 drop 上传）也在那一帧里执行 | 逐帧求值不受同源限制，跨域帧同样收 |
| shadow DOM | 穿透各级 **open** shadow root：元素进快照并标 `shadow` 事实，影子里的文本同样收集；命中测试按"文档 → 宿主 shadowRoot"逐层下钻 | `closed` 的 shadow root 从页面 JS 进不去，里面的元素模型看不见 |
| 新标签 | `context.on("page")` 登记，动作后的 settle 里跟随最新标签；当前标签被关掉就切回剩下的一个 | 只跟随，不自动关闭任何标签 |
| 原生对话框 | 挂 `page.on("dialog")`：默认 `dismiss`（不改变页面状态）；`--dialog-policy accept` 才点确定（prompt 保留页面输入的默认值）；`confirm` / `prompt` / `beforeunload` 一律记缺陷候选 | `alert` 只记事实、不算缺陷 |

不挂处理器时 Playwright 会**静默自动关掉**对话框——页面代码以为用户点了取消/确定，测试却什么都没记下来。所以这里一律显式应答并把事实落盘：
trace 每行的 `page_facts`（`{"fact": "dialog", "kind": "confirm", "message": …, "handled": "dismiss"}` 与 `{"fact": "tab", "note": …}`），
`actions.json` 里对应一行 `action="PAGE"`——它不是动作，所以不计入重复判定与动作预算，只作为模型与报告能看到的事实。

离线冒烟 `scripts/smoke_m3.py` 起一个本地 HTTP 服务跑 `examples/m3_fixture.html`（子页 `m3_child.html`），
八个用例：帧内点击、帧内 drop 上传、影子点击、被 light DOM 盖住的影子按钮判**不可达**（不许假成功）、新标签跟随、
alert（dismiss）/ confirm（dismiss 与 accept）/ prompt（accept 保留默认值）。全程零模型调用。

## 给平台/CI 用的命令行契约（P0）

外层平台只按 **argv 调 CLI + 读文件/JSON**，不 import 内核代码。带 `--json` 的子命令：
stdout 只留最后一份 JSON（人类话术走 stderr），退出码 **0=成功、1=用例失败（JSON 仍有效）、其他=内核错误**。

```powershell
# 用例 YAML → 计划：校验 + 编译（--secret-keys 给出环境已登记的密钥名，用于查未知引用）
uv run ui-agent case compile --in <用例库>\<场景>\baiwang.yaml --json --secret-keys 账号,密码
#   → {ok, errors[{line,code,message}], warnings[…], plan}；errors 非空时 plan 为 null
#   → 要落盘给 run --plan-file，用 --out（写的是 plan 本体；--json 的外壳不能直接当计划）
uv run ui-agent case compile --in <用例库>\<场景>\baiwang.yaml --out plan.json --secret-keys 账号,密码

# 一句话 → 用例草稿（这一步调用文本模型）
uv run ui-agent case draft --desc "登录后查询票据" --url <网址> --json
#   → {ok, yaml, plan, meta}；出来的 YAML 先过一遍 compile 自检，不合规就报错

# 跑（--run-dir 指定产物目录；--mode 只管产物策略，服务器用 UI_AGENT_HEADLESS=true 照样无头）
uv run ui-agent run --plan-file plan.json --run-dir .artifacts\runs\case-123 --mode ci --url <网址> --json --var 账号=<账号>

# 探针：只看不点，给平台登记环境用
uv run ui-agent snapshot --url <网址> --json
#   → {ok, summary:{final_url,title,element_count,needs_login,has_password_field}, state}
#   needs_login 取「页面上有密码输入框」这个确定性信号，不做业务推断

# 重出报告
uv run ui-agent report --run-dir .artifacts\runs\case-123 --json   # → {ok, report}
```

用例 YAML 的校验规则（错误：空 steps / 空 goal / 空 checks / 把 `${{secret.*}}` 写进 goal /
未知断言类型 / 名称含 `/\:*?"<>|` / 未登记的密钥名 / 重复键；警告：末步断言偏弱、
步骤 > 12、单步断言 > 3、变量写了字面量敏感值、硬编码 url）由 `ui_agent/case/yaml_case.py` 实现，
错误带**真实行号**（按 YAML 节点定位），未知密钥通过 `--secret-keys` 与环境的登记表比对。

`case draft` 与 `plan` 生成的 YAML **不写 url**：入口留给环境决定，同一份用例才能跑在不同环境。

## 产物

单目标运行在 `.artifacts/runs/<任务>-<时间>/` 留下：`result.json`、`trace.jsonl`、`actions.json`
（动作 + 是否改变页面）、`snapshots/*.json`（喂给模型的 state）、`screenshots/`、`report.html`。

`trace.jsonl` 是一份**分层时间线**：每行有 `seq`（文件内连续编号，行序即事件序）与 `at`（毫秒），
按 `kind` 分四层——`run_start`/`run_end`（运行边界：身份、配置、最终状态与耗时）、
`step_start`/`step_end`（计划模式的步边界，只在根 trace 有）、`decision`（一次决策往返 + 落地的动作，
保留秒级 `time` 兼容老消费方）、`assert`（DONE 验收：轮询次数 `attempts`、耗时、逐条结果）、
`model`（没产出决策的模型调用，比如响应不合规）。回放缓存另有一行运行级 `kind="cache"`：
模式、写入结果、命中/失配与逐类步骤计数（词表见 `run/trace.py` 头部注释）。

计划的运行目录多出：`plan.json`、`summary.json`（逐步结果 + `replans`）、`steps/<NN-目标>/`（每步自成一套产物）、
根 `trace.jsonl`（各步的**聚合**：决策/断言/模型行原样汇入并补记 `plan_step`，生命周期行留在各自文件）、
根 `actions.json`（同理，带 `plan_step`）、`report.html`（汇总报告，截图按相对路径引用）；发生重排时还有 `plan.final.json`。

平台的回读口径就锚在这套布局上：`summary.json` 给状态与逐步结果，根 `trace.jsonl` 给逐决策时间线
（决策行含 `url`/`title`），根 `actions.json` 给动作序列。截图只进报告、永不进模型输入；
`mode=debug` 逐步留图，`mode=ci` 只留失败那一张。
`mode` 与有无头已经解绑（`UI_AGENT_HEADLESS` 单独控制），服务器上 `--mode debug` 也不会弹窗。

## 目录

| 路径 | 内容 |
|---|---|
| `src/ui_agent/cli.py` | 命令行：`case` / `plan` / `run` / `snapshot` / `report`（`--decider`、`--supervisor`、`--cache`、`--cache-strict`、`--upload-mode`、`--dialog-policy` 可覆盖配置） |
| `src/ui_agent/case/` | 用例 YAML：按行号校验 → 编译成计划；计划 → 用例 YAML 草稿 |
| `src/ui_agent/llm/` | 三个 LLM 缝：planner 编排、textfill 字段取值、supervisor 卡住裁决 |
| `src/ui_agent/observe/` | state 组装；`js/snapshot.js` 页面内取数与索引标记；`js/guard.js` 执行前守卫 |
| `src/ui_agent/decide/` | Jev 客户端、questions 模板、提示词 |
| `src/ui_agent/driver/` | Playwright 驱动：双模式启动、点击降级阶梯、跨 frame 快照、新标签跟随、原生对话框事实 |
| `src/ui_agent/act/` | 白名单动作执行 + 守卫预检 + 降级通报；上传取值校验（`upload.py`） |
| `src/ui_agent/verify/` | 独立断言（文本/URL/元素/控件值/计数/属性 + DONE 后轮询验收），不看模型自述 |
| `src/ui_agent/run/` | 单目标主循环与多步计划执行、卡住检测、监督升级、预算、分层时间线（`trace.py`）与产物落盘；`explore.py` 探索出用例骨架 |
| `src/ui_agent/bridge/` | Agent 桥：文件协议（`protocol.py`）与内核侧/CLI 侧客户端（`client.py`） |
| `src/ui_agent/bridge_cli.py` | `ui-agent bridge wait|answer`：宿主侧只读写桥目录，不 import 浏览器栈 |
| `src/ui_agent/entry.py` | `ui-agent` 入口薄分发：`bridge` 走 `bridge_cli`，其余走 `cli`（延迟导入，冷启动 ~30ms 而非 780ms） |
| `src/ui_agent/mcp_server.py` | 可选 MCP 适配器：把桥的 ask/answer 包成 5 个工具（零内核改动） |
| `src/ui_agent/replay/` | 回放缓存：键与单键文件（`cache.py`）、语义重定位与上下文守卫（`match.py`）、录制/回放会话（`session.py`） |
| `src/ui_agent/schema/` | 契约：state / decision / check / plan / supervisor / status |
| `src/ui_agent/report/` | HTML 报告（计划模式读 `summary.json`，单目标读 `result.json`） |
| `tests/fakes.py` | 离线单测的假驱动、假决策引擎与脚本化监督模型（不起浏览器、不调模型） |
| `../ui-agent/` | 自足 skill 包（与 `kernel/` 平级）：SKILL.md + 8 篇 reference + 双平台 bootstrap + 样例用例 + 由 `build_skill.py` 生成的内核副本 |
| `scripts/build_skill.py` | 把内核最小可运行集同步进 `../ui-agent/kernel/`；`--check` 报漂移 |
| `.browsers/` | 工作区内置浏览器（gitignore，`scripts/env.ps1` 导出路径） |
| `.artifacts/` | 持久化 profile、运行产物（gitignore） |
| `examples/goals/` | 现成的自然语言目标（百望登录、百望全流程） |
| `examples/` | 本地夹具：`local_fixture.html`（上传规则）、`m3_fixture.html` + `m3_child.html`（iframe / shadow / 新标签 / 对话框） |

## 状态

M0 完成：单目标循环可跑（state → Jev → 执行 → 断言），零模型调用即可验证快照/守卫/降级阶梯。

M1 完成：planner 拆解 + 多步计划执行（一个会话、失败即停、历史交接）+ 独立断言 + HTML 报告 + `--var` 供值。

M2 完成：卡住/阻塞/预算/断言未过时交给监督模型（HINT / RECOVER / REPLAN / ABORT），
裁决与落点由代码校验，预算有限、不可用即退回 M0 停机语义；计划模式支持整体重排。

M3 完成（本地夹具验证）：**上传**（A/C 两条通道 + 页面级验收）与**页面结构**（iframe / open shadow DOM /
新标签跟随 / 原生对话框事实化 + `--dialog-policy`）。这些能力都在 `scripts/smoke_m3.py` 与 `smoke_upload.py`
的真实 Chromium 上跑通，但**尚未在真实业务页面上实跑校准**——拿到合适的页面后再验证一遍（尤其是遮挡判定与跨域帧）。
M4（批量回归）未开始。P0（平台契约）已完成：`case compile/draft`、`run --run-dir/--json`、`snapshot --json`、
`report --json` 与产物布局（根聚合 `trace.jsonl`/`actions.json`、`screenshots/`），命令行契约有子进程级冒烟 `scripts/smoke_cli.py` 把守。
回放缓存（plan.md 第 4 项）内核侧已落地：按「用例哈希 + 环境地址」建键的录制 / 回放（语义重定位、上下文守卫、
失配交还与自愈回填）、独立严判开关、零模型停机语义，`scripts/smoke_replay.py` 在真实 Chromium 上把守。

M-A 完成（Agent 桥 / skill，2026-10-19）：宿主对话大模型可接替 **textfill / supervisor / decide 三个** LLM 缝，
走 `<run-dir>/bridge/` 下的 `ask-*.json` / `ans-*.json` 文件协议；Jev 决策保留，四档开关 `--decider jev|llm|agent|off`
与 `--supervisor off|on|agent` 可组合。桥答复与模型输出同等不信任（过原有校验链），不可用即按 M0 停机。
配套：`explore` 子命令（探索不录制 → 产出轨迹与用例骨架）、YAML `id:` 作缓存身份、仅断言失败时保留录像、`--mask-vars`、
五个浏览器开销开关、可选 MCP 适配器。

> 注：**planner 缝没有走桥**。桥覆盖的是 supervise / decide / text 三处；planner 在 skill 形态下由 agent 直写用例 YAML
> 替代（`case compile` 校验到 0 error 再跑），HTTP 版 `plan` / `case draft` 保留给平台用。

M-B 完成（skill 包，2026-10-20）：`../ui-agent/` 是**自足**的一整份交付——文档 + 内核副本 + 样例用例在一个目录里，
整个丢进宿主的 skills 目录就能跑。宿主 agent 读 `SKILL.md` 路由到 8 篇 reference
（setup / cases / running / supervising / deciding / exploring / debugging / security），配双平台幂等自检脚本
`scripts/bootstrap.{ps1,sh}`（查 uv → `uv sync` → chromium 装进 `.browsers` → 验 `--help`，退出码 0 = 就绪）。
内核副本由 `scripts/build_skill.py` 从开发树生成（`--check` 报漂移），只挑运行必需的文件，约 1 MB，不含 tests / `.venv` / 运行产物。
文档口径按**实际落码**而非 spec 原文（桥超时 180s、断言可用 8 种而 `RESULT_KINDS` 5 种）。

离线单测 **590 项**与十一个冒烟脚本（`smoke_browser` / `smoke_cli` / `smoke_snapshot` / `smoke_plan` / `smoke_supervisor` /
`smoke_upload` / `smoke_m3` / `smoke_assert` / `smoke_replay` / `smoke_bridge` / `smoke_explore`，或 `run_all_smoke.py` 串跑）全绿；
`ruff` 无告警。

实跑校准（2026-09-23，百望非税查询页）：`goto` 之后**不能立刻信快照**——页面 `domcontentloaded`
后先渲染壳（约 20 个导航元素），内容区 0.8–3 秒后才到，中间那段窗口"看起来稳定"却没有任何
业务控件。所以：

- 首次渲染过一道就绪门（`ready_stable_ms`，默认 2.5s 指纹不动才算渲染完），导航后先等它；
- 快照里一个元素都没有时（SPA 偶发全空白）刷新一次再看，只刷一次，之后按事实交给决策层；
- 模型的传输层错误（DNS / 连接重置 / 超时）重试，HTTP 4xx/5xx 不重试。

`SCROLL_TO` 的可选性一度与目标 head 不一致（目标 head 从来没有被组装出来，模型一选就抛错），
现在两者都由 `build_action_space` 派生，不会再分叉。

实跑记录：登录 → 行政区域"2102 大连" → 查询 的完整流程与单独的查询流程都已可视化跑通
（Jev 逐步决策，脚本断言验收；凭证只经 `--var` 注入，产物里没有明文密码）。

模型密钥：文本模型接 **DeepSeek 官方**（`https://api.deepseek.com/v1`，`TEXT_MODEL=deepseek-flash`
即 V4.1-Flash）；`.env` 里已有 key。`TEXT_MODEL_REASONING=none` 关掉思考（实测掉出
`reasoning_content`，同题 5 tokens vs 40）；想让它思考就改成别的值。
密钥只进 `.env`（已 gitignore）：单测由 `tests/conftest.py` 钉成空串，冒烟脚本显式关监督缝，
两者都不会联网计费；脚本化的 `--var` 路径也不需要 key。

