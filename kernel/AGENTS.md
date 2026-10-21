# ui_agent

读 README.md 再动手。三层分工：**LLM 编排/裁决，Jev 决策，Playwright 执行**。常态循环里没有 LLM。

## 硬约束

- 模型输出永不变成 selector / 坐标 / 可执行 JS；元素只能用**当次快照**分配的 index 引用。
- 快照标记 `data-uiagent-idx` 由本仓库代码生成；选择器永远只存在于 executor 内部。
- 执行前校验节点新鲜度与遮挡；浏览器 mutation 永不重试。
- 断言独立于模型自述：`DONE` 不是成功证据；没配断言只能算"未验收"。
- `TYPE_TEXT` 的值只来自文本模型、宿主 agent（桥）或 CLI `--var`（`--var` 优先，值不落盘、不进模型）；
  模型输出必须解析为严格 JSON，解析失败则不输入。**secret 字段在桥路径上直接拒绝**（只能 `--var`）。
- `UPLOAD` 的目标只能是文件控件（快照 role `file`，允许隐藏），**文件路径只来自 `--var`**，没有模型兜底：
  匹配不上就干净地拒绝。走 input（U1）还是 drop（U2）由设置决定，不做跨通道静默回退；
  执行器只做确定性预检（存在、扩展名/MIME、单文件控件不给多个），大小/数量上限属于页面策略，交给断言。
  产物只落文件名与大小，本地路径不落盘、不进模型。
- 计划执行失败即停：某步没通过，后续步骤记为 `skipped`，不把"前一步没成立"当成可继续的状态。
- 页面结构（M3）：快照与守卫要**穿透各级 open shadow root**，命中测试按"文档 → 宿主 shadowRoot"逐层下钻
  （直接问元素自己的 root 会看不见盖在宿主上面的 light DOM，把被遮挡判成可达）；`closed` shadow root 拿不到就放弃，不许猜。
- iframe：索引 → frame 的映射只来自当次快照（`_frames`），任何帧内求值（含 drop 上传）都必须回到元素所属的帧里做。
- 新标签：`context.on("page")` 只登记，切换放在动作后的 `settle()` 里（事件处理器内不做 Playwright 往返）；
  当前标签被关掉要切回剩下的一个。任何标签都不自动关闭。
- 原生对话框：必须显式挂 `page.on("dialog")` 并按 `UI_AGENT_DIALOG_POLICY` 应答——不挂处理器时 Playwright 会静默自动关掉，
  页面以为用户点了取消/确定而测试什么都没记录（假成功）。`confirm` / `prompt` / `beforeunload` 一律记缺陷候选。
- 页面事实（新标签、对话框）写成 `action="PAGE"` 的行为与 trace 的 `page_facts`；它不是动作，不计入重复判定与动作预算。
- 每步的动作历史（`actions.json`）只含本步动作；上一步带来的历史只在内存里喂给模型，不混进产物计数。
- 监督模型只能说 HINT / RECOVER / REPLAN / ABORT，落点由代码校验：元素必须是当次快照里的 index，
  恢复动作不许 `CLICK_FORCE`（缺陷路径）、`TYPE_TEXT` 与 `UPLOAD`（值/文件不能现编），REPLAN 的步骤过与 planner 相同的断言检查。
  监督模型不可用（没 key / 预算用完 / 回答不合规）时，行为必须与"没有监督模型"时完全一致：按 M0 语义停机。
- `RECOVER` 的动作走普通执行路径（守卫、降级阶梯、缺陷通报、trace 一个都不能少），不许旁路执行。
- 监督预算在调用前扣除（每步 `SUPERVISOR_PER_STEP`，整轮 `SUPERVISOR_PER_RUN`）；升级次数有限 ⇒ 循环必然终止。
- 单目标模式收到 REPLAN 只停在 `replanned` 状态，不假装跑完了新序列；计划模式才做整体替换。
- 密码等敏感字段：快照里只给掩码 + `secret` 标记，typed value 不进 trace / 历史。
- 禁用控件仍作为事实出现在元素表（供 WAIT 判断），但 `operations` 为空，永远不能被选中。
- 只读事实进 state：`rect`、`dom_index`、选择器一律不进模型输入（`to_jev_state` 显式构造，不做 model_dump 透传）。
- 可视化与无头必须用**同一个 Chromium 二进制**（`channel="chromium"`，绕开 headless shell），viewport 两边显式一致。
- 截图只进报告，永不进模型输入（Jev 不支持图像）。
- 点击降级阶梯只有 L0–L3 是正常路径；L4（force）与 L5（合成事件）必须写入 trace 并在报告标 ⚠（视作可用性缺陷候选）。
  真遮挡（命中测试失败）时 L4 直接跳过——force 只是跳过 Playwright 检查，事件仍被上层元素接收，假成功比失败更糟。
- 密钥只放 `.env`（已 gitignore）；测试不得调用付费 API（含 TypeSafe 与文本模型）。
- 平台契约（P0）：平台只按 **argv 调 CLI + 读文件/JSON**，永不 import 内核代码。
  带 `--json` 的子命令 stdout 只留最后一份 JSON（人类话术走 stderr），退出码 0=成功、1=用例失败（JSON 仍有效）、其他=内核错误。
  `case compile` 出错时 `plan` 必须为 `null`；`--out` 只在编译通过时写文件，写的是 `plan` 本体
  （`--json` 外壳 `{ok,errors,warnings,plan}` 不能直接喂 `run --plan-file`，这是给宿主踩过的坑）；
  `run --run-dir` 的产物布局（根 `trace.jsonl` 是分层时间线：
  `run_start`/`run_end`/`step_start`/`step_end` 生命周期行 + `decision`（每行有 `step/url/title`）+ `assert` + `model` 行，
  并带 `seq` 与毫秒级 `at`；根 `actions.json`、`snapshots/`、`screenshots/`、`summary.json`/`plan.json`）是对外接口，改名即破坏平台。
  产物策略由 `UI_AGENT_MODE` 定，有无头由 `UI_AGENT_HEADLESS` 定（服务器 `--mode debug` 也要无头）。
- 中文 Windows 下一律 UTF-8 读写（`encoding="utf-8"`），否则 GBK 解码会崩。
  **读用户给的文件用 `utf-8-sig`**（`--goal-file` / `--plan-file` / `--desc-file` / `--in` / `bridge answer --file`）：
  PowerShell 的 `>`、`Set-Content -Encoding UTF8`、记事本都会写 BOM，`json.loads` 见 BOM 直接抛
  `Unexpected UTF-8 BOM (decode using utf-8-sig)`。写出侧仍用 `utf-8`（不带 BOM）。
- 未经用户要求不 commit / push。

## Agent 桥 / skill（M-A，2026-10-19）

宿主对话大模型接替四个 LLM 缝的通道：内核写 `ask-*.json`，宿主写 `ans-*.json`（`<run-dir>/bridge/`）。

- **桥答复 = 模型输出，同等不信任**：supervise 过 `parse_ruling`，decide 过 `assemble_decision`/`validate_choice`，
  text 过 textfill 的现有校验。答歪 → 与对应 HTTP 缝完全相同的处置链。
- **桥不可用（超时/坏文件/id 不匹配）→ unavailable → 按 M0 语义停机**，与"没有监督模型"一致。
- **`AgentEngine` 不做 Phase 2**：两次不合规后直接走 `decide_invalid` 升级，绝不构造 `LlmEngine`；
  `AbortDecision`（模型返回 `{"error": ...}`）必须在 Phase 1 立即 `aborted`，不能当可重试异常。
- **agent 模式 `--run-dir` 必填**：宿主必须在起后台进程**之前**就知道桥目录。
- **桥由 CLI 装配**，`Runner`/`PlanRunner` 只接受传入的 bridge；plan 模式全计划共用一个桥 + 一份监督预算。
- **status 归 CLI**：Runner 只 `ask`，不写终态；`bridge wait` 遇到 status 缺失按 running 处理（启动竞态）。
- **v1 监督只看文本**：`payload.attachments` 恒空，截图永不进任何模型输入。
- **`--var` 值与上传路径不进模型、不进桥**；secret 字段在桥路径上直接拒绝。
  **落盘口径（2026-10-19 修订）**：`--var` 的**非敏感**值默认仍出现在产物里——快照的元素当前值、
  `actions.json` 的 `text`、trace 的 `action.detail`、`report.html`。页面会回显输入框内容，
  这是内核把页面事实如实记录的结果，也是**平台既有行为**（未改动）。要连它一起掩掉用
  `--mask-vars`（`UI_AGENT_MASK_VARS=true`）：掩码作用于上述全部位置，但**录像里保留真实值**
  （否则回放会输错）。secret 字段无论开关如何一律掩码。
  代价：开了掩码后连"输入了什么"都看不出来，排障时先关掉它复现。
- **用例 YAML 的 `id` 是缓存身份**：有 id 用 id，没有才退回内容哈希（旧用例键不变）；
  改 goal 措辞不丢录像。`plan_case_hash` 必须排除 `case_id`，否则旧录像集体作废。
- **仅收尾断言没过时保留录像**（`keep_on_check_failed`）：用户可能只是改了断言；
  动作执行失败仍淘汰（防投毒）。想主动重录用 `--relearn`。
- `CACHE_POLICY_VERSION` **保持 v3**：加 id 不改记录格式。
- **探索不录制**（`cache=None`）：探索会点开用不到的页面，录进去回放时会重放垃圾动作。
  探索产物落 `<run-dir>/explore/`（trajectory.md / explored.yaml / explore.json），不动既有布局。
- **不替 agent 编断言**：`explored.yaml` 的 `checks: []` 故意过不了 `case compile`，
  逼 agent 读轨迹自己写；②③之间那次「真跑 YAML」是唯一能证明断言成立的证据。
- **YAML 的断言写法是「类型: 值」**（`text_contains: 已登录`），与 CLI `--check` 的 `text_contains=已登录` 不同。
- **token 账分两种**：Jev/HTTP 的 usage 内核能统计；agent 模式的花费在宿主循环里，内核只报「N 次桥问答」。

## 浏览器开销开关（2026-10-19）

`run` / `explore` 都接受这几个开关，不传即用配置默认值（平台行为不变）：

| 开关 | 字段 | 默认 | 作用 |
|---|---|---|---|
| `--settle-ms` | `settle_ms` | 700 | 每个动作后的等待；**回放提速的首选旋钮** |
| `--slow-mo` | `slow_mo` | 250 | 有头时每步操作间隔；只影响可视化 |
| `--ready-stable-ms` | `ready_stable_ms` | 2500 | 首帧就绪门：指纹安静多久算渲染完成 |
| `--ready-timeout-ms` | `ready_timeout_ms` | 20000 | 就绪门最长等待 |
| `--assert-timeout-ms` | `ui_agent_assert_timeout_ms` | 5000 | DONE 验收轮询窗口 |

调 `--settle-ms` 会改变页面在动作后的稳定时机，**可能让既有录像判 `wrong-context` 失配**——
回放提速要先把录像重录一遍，别在录制与回放之间来回改它。

## MCP 适配器（可选，2026-10-19）

`src/ui_agent/mcp_server.py`：把桥的 ask/answer 包成 MCP 工具，**零内核改动**（只复用 `AgentBridge`）。
五个工具：`ui_agent_start` / `ui_agent_next` / `ui_agent_answer` / `ui_agent_status` / `ui_agent_abort`。

- **可选依赖**：`uv sync --extra mcp`；没装 `mcp` 包时模块给出安装提示并退出 2，内核本身不依赖它。
- **它只覆盖「不能跑后台命令」的宿主**；有 shell 的宿主用 `bridge wait/answer` 更直接、更省进程。
- 与 skill 形态**同一套语义**：同样的 ask/answer 校验、预算、缓存、trace。
- server 常驻 ⇒ 一个进程管多个 run；`abort` 按 run_id 终止对应子进程树。

## skill 包（M-B，2026-10-20）

发布的 skill 包在 **`../ui-agent/`**（与 `kernel/` 平级，**不在 kernel 内**），由
`scripts/build_skill.py` 从开发树同步生成。**它自足**：文档 + 内核副本在一起，
整个目录丢进宿主的 skills 目录就能跑。宿主 agent 读 `SKILL.md` 决定读哪篇 reference。

**包里不带用例。** 用例是用户自己的资产（断言必须由懂业务的人定），放在用户自己的 git 仓库里；
skill 只教怎么写、怎么跑、产物放哪。包里唯一相关的是一个离线夹具
`kernel/examples/local_fixture.html`——测试页，不是用例。

```
ui-agent/
  SKILL.md                        frontmatter(name+description) + 路由表 + 十二条铁律 + 四档开关 + 六步工作流 + 并行 + 命令面 + 文件桥 vs MCP
  references/
    setup.md                      bootstrap 脚本 / 浏览器目录 / 密钥 / 有头无头 / 常见环境错误
    cases-repo.md                 用例库怎么建：向用户要哪三件事 / 目录约定 / outputs 时间戳 / 录像归属
    cases.md                      用例 YAML 写法、8 种断言、5 种算「业务结果证据」、编译错误码全集
    running.md                    后台起 run + bridge wait/answer 循环 + 处置表 + 读产物
    supervising.md                kind=supervise 的作答契约（RULINGS / RECOVER_OPS / 预算 / 拒绝文案）
    deciding.md                   kind=decide 的作答契约（operation 13 种 / probabilities 六条硬规则 / 目标 head）
    exploring.md                  explore 四段工作流 + 产物四件 + 骨架为什么故意过不了编译
    m3.md                         iframe / shadow DOM / 遮挡 / 新标签 / 原生对话框的能做什么与边界
    debugging.md                  trace 词表 / cache 词表 / 11 个停机状态 / 失配判定 / 浏览器开销旋钮
    envs.md                       全部环境变量 ↔ CLI 开关对照、默认值、常用组合
    security.md                   secret 与非 secret 两条路 / 落盘边界 / 探索产物为什么只写来源
    parallel.md                   多开：profile 隔离 / run-dir 隔离 / parallel_run.py / 子 agent 分工与上限
  scripts/
    bootstrap.ps1 / bootstrap.sh  幂等自检：uv sync + chromium 装进 kernel/.browsers + 验 --help
    host_loop.py                  宿主循环参考实现：起内核 → 读 ask → 作答 → 读终态（标准库，不 import 内核）
    parallel_run.py               多路并行编排：分配 profile/产物目录、并发起 N 个 host_loop、按 status 汇总
    check_docs.py                 文档与内核一致性校验：词表 / 默认值 / 开关 / 断链 / 包内不许有用例与运行产物
  kernel/                         ← build_skill.py 生成：内核最小可运行集（src / examples / pyproject / uv.lock /
                                     scripts/env.ps1 / scripts/smoke_browser.py），不含 tests 与运行产物
```

**生成与校验**（改内核后必须同步，否则 skill 包里的内核是旧的）：

```bash
uv run python scripts/build_skill.py            # 同步（只覆盖清单内的文件）
uv run python scripts/build_skill.py --check    # 只报漂移，退出码 1（CI 用）
uv run python scripts/build_skill.py --with-tests
```

`ui-agent/kernel/` 是生成物，一律不可手改：同步会重写清单内的每个文件，并删掉清单外的散落文件。
**但 `.venv` / `.browsers` / `.artifacts` / `runs` 原样保留**——那是用户跑过 bootstrap 之后的环境
（约 600 MB），删掉要重装。早期版本整目录 rmtree，把用户环境一起铲了。

写这些文档时必须守住的四条：

1. **只写已落码的事实**。spec 与实际不一致时按实际写，例如桥超时默认 **180s**（spec 里写 600）、
   断言可用 **8 种**而 `RESULT_KINDS` 只有 **5 种**（两者不是同一件事）。
2. **planner 缝没有桥**。桥只覆盖 supervise / decide / text 三个缝；planner 由 agent 手写 YAML 替代，
   不要写成「四个缝都能走桥」。
3. **不给 agent 编断言的余地**。`explored.yaml` 的 `checks: []` 是故意的，它必须过不了 `case compile`。
4. **路径一律相对本包**。文档里不许出现 `skills/ui-agent/`、`cases/大肥鱼/` 这类只对开发树成立的路径；
   示例用例只写「结构」（一段 YAML 片段 + 该存到哪），不指向包里任何文件；命令一律假设 cwd 在 `kernel/`。
   `scripts/check_docs.py` 会把这几条当断言跑，别绕过它。

`bootstrap.ps1` 必须存成 **UTF-8 with BOM**：Windows PowerShell 5.1 读无 BOM 的 UTF-8 会按 ANSI 解码，
中文注释里的全角标点会把语法解析带偏，报 `Unexpected token`。**edit 工具会剥掉 BOM**，每次改完要补：

```powershell
$p = "ui-agent/scripts/bootstrap.ps1"
$t = [System.IO.File]::ReadAllText($p, [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText($p, $t, (New-Object System.Text.UTF8Encoding $true))
```

`bootstrap.sh` 无此要求。

`bootstrap` 的 `-CheckOnly` / `--check` 必须**不装任何东西**：早期版本在这一支里用 `uv run` 查 python 版本，
结果顺手创建了 `.venv`。现在先 sync 再读 `.venv` 里的 python，`--check` 全程零写入。

## 多开 / 并发（2026-10-21）

内核支持**多个进程同时各跑一个用例**（一路一个浏览器）。三件事必须每路独立：

- **profile**：`Driver.start()` 用 `launch_persistent_context(user_data_dir=...)` 保登录态，Chromium 对同一
  `user-data-dir` 加**单实例锁**——第二个进程直接被拒。所以 `--profile-dir`（或 `UI_AGENT_PROFILE_DIR`）
  必须每路不同。撞锁时 Chromium 原生的报错是一句极具误导性的 `Target page, context or browser has been closed`，
  `driver/playwright_.py` 把它翻成 `ProfileLocked`（`RuntimeError` 子类）：
  「profile 目录已被另一个浏览器占用：…；并发跑多个用例时每个进程要给自己的 --profile-dir（或设 UI_AGENT_PROFILE_DIR）」。
  **不许把这条翻译去掉**——没有它，用户只会看到 "been closed" 然后去查网络。
- **run-dir**：`run/dirs.py` 的 `make_run_dir()` 用**原子 mkdir 抢位置**（`exist_ok=False`），
  被占了就依次试 `-2`、`-3`……同一秒并发起同名任务不再互相覆盖。
  显式 `--run-dir` 仍然优先（并行时应该显式给）。
- **桥**：桥目录默认跟着 `<run-dir>/bridge` 走，所以 run-dir 不撞桥就不撞。
  **`UI_AGENT_BRIDGE_DIR` 在并行时不许设**——它会让所有路共用一个桥，宿主会答错 lane。

录像（cache）本身是并发安全的：`save()` 用 `tmp` + `os.replace` 原子替换，同键后写覆盖。
但**同一个用例并行录制没有意义**（两路录同一段动作，纯浪费 token）。

skill 侧的并行编排在 `../ui-agent/scripts/parallel_run.py`，文档在 `references/parallel.md`。

## 待办（明确未做，不要误以为已支持）

- **网络请求/响应不可见**。全库没有 `page.on("response")` / `route(` / `wait_for_response` / HAR 任何一处，
  driver 只等 `domcontentloaded` + 指纹稳定。后果：**接口返回 500 但页面没变，内核与模型都看不见**。
  要做的话落点在 `driver/playwright_.py` 的 `start()` 挂 `context.on("response")`，按
  `url + method + status + content-type + body 摘要` 进环形缓冲，再让 `snapshot()` 把网络事实并进 State。
  必须先定裁决策略（只留 JSON、截 8KB、域名白名单），否则 body 里的 token / 手机号 / 二进制会直接进产物。
- **closed shadow root 拿不到**，只覆盖 open。页面 JS 进不去，只能放弃，不许猜。
- **原始 DOM 不进模型**（这是有意的）：模型看到的只有 `snapshot.js` 蒸馏出的元素表（index/role/label/value/
  rect/operations/options），截到 `max_elements`（默认 250）与正文 `max_text`（默认 6000）。想加任意 DOM 属性
  得先进快照结构，不是随手读。
- **M4（批量回归）未开始**。

## 检查

```bash
uv run ruff check . && uv run pytest -q          # 离线单测（不起浏览器、不联网）
uv run python scripts/smoke_cli.py               # P0 命令行契约：case compile / snapshot / run / report 的子进程级冒烟（零模型调用）
uv run python scripts/smoke_snapshot.py          # 本地夹具全链路冒烟（真实 Chromium，零模型调用）
uv run python scripts/smoke_plan.py              # 两步计划的真实浏览器冒烟（脚本化决策，零模型调用）
uv run python scripts/smoke_supervisor.py        # 监督升级的真实浏览器冒烟（脚本化监督，零模型调用）
uv run python scripts/smoke_upload.py            # 上传 A/C 两条通道 + 页面拒收负例（零模型调用）
uv run python scripts/smoke_m3.py                # iframe / shadow / 遮挡 / 新标签 / 对话框（本地 HTTP 服务，零模型调用）
uv run python scripts/smoke_assert.py            # 断言：计数 / 属性 / 轮询重试 + 计数负例（零模型调用）
uv run python scripts/smoke_replay.py            # 回放缓存：录制 → 零模型回放 → 失配停机（零模型调用）
uv run python scripts/smoke_bridge.py            # Agent 桥：agent 决策 / 监督往返 / 零模型回放 / 超时停机（零模型调用）
uv run python scripts/smoke_explore.py           # 探索全链路：探索 → 骨架被拒 → 补断言 → 录制 → 零 token 回放
```

> 冒烟会启动真 Chromium。跑之前若上一轮异常退出留下残留进程，先清一遍（Windows 上残留 chrome 会锁住
> `.artifacts/chrome-profile`，导致后续所有冒烟在启动浏览器时失败）：
> `Get-Process chrome -ErrorAction SilentlyContinue | Stop-Process -Force`
