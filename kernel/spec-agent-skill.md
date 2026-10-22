# ui_agent Skill 抽取 Spec：对话大模型驱动 + Jev 决策 + 桥接监督

- 状态：**草案 v2：5 项已拍板 / 2 项待定**（2026-10-19，见第 8 节）
- 范围：把 ui_agent 内核抽成一个 skill（可选加 MCP 适配器），交给任意 agent 宿主；
  模型侧从「单独配置的 HTTP 文本模型」改为「宿主对话大模型」；Jev 决策保留（用户给 key）；
  阻断时由对话大模型监督裁决。
- 不改：平台（`ui尝试/ui_backend`、`ui_frontend`）继续走现有 HTTP 四档形态，内核桥是纯加法。
- **落码位置**：隔离副本 `D:\test_skills\ui_agent_skill_dev\kernel`（不是源仓库、不是平台副本）；
  操作手册与合并流程见 `D:\test_skills\ui_agent_skill_dev\HANDOFF.md`。

---

## 1. 背景与目标

现状（内核 `src/ui_agent/`）：四个 LLM 缝全部走 HTTP 到一个单独配置的文本模型（`TEXT_MODEL_*`）：

| 缝 | 文件 | 职责 | 现状 |
|---|---|---|---|
| planner | `llm/planner.py` | 自然语言流程 → 分步计划 | `plan` / `case draft` 命令内调 HTTP |
| textfill | `llm/textfill.py` | TYPE_TEXT 字段取值 | `--var` 优先，缺值回落 HTTP |
| supervisor | `llm/supervisor.py` | 卡住时一次裁决（HINT/RECOVER/REPLAN/ABORT） | HTTP，`--supervisor on` |
| delegate | `decide/delegate.py` | 大模型代班 Jev 做决策 | HTTP，`--decider llm` |

Jev 决策（`decide/jev.py`）已是「用户 key → HTTP」形态；执行/守卫/断言/回放缓存与模型无关。

**目标**：

1. 任意 agent（Qoder / Claude Code / Cursor / Codex 等）装一个 skill 即可跑 UI 自动化；
2. 对话大模型直接接替 planner / textfill / supervisor / 代班决策——**不再需要 `TEXT_MODEL_API_KEY`**；
3. 用户给 Jev key 时，常态决策仍由 Jev 做（省 token、快、类型化），**阻断时才请对话大模型监督调整**；
4. 内核既有硬约束（守卫、断言独立、缓存、预算、trace）原样保留，对话大模型必须过同一套校验。

---

## 2. 核心机制：Agent 桥（文件协议）

对话 LLM 不是一个能被 subprocess 同步调用的服务，但**任何 agent 都能做三件事**：跑后台命令、阻塞等待、读写文件。
所以内核新增一个「问宿主 agent」的通道——**Agent 桥**：

```
用户 ⇄ 对话 agent（= 大模型：编排 / 监督 / 兜底决策）
             │ ① 写用例 YAML → case compile 校验          ② bridge wait / answer 循环
             ▼
     ui-agent CLI（内核进程，后台作业；Playwright 常驻）
       observe → decide(Jev | 回放 | 桥) → guard → execute → verify → 记录缓存
             │ ③ 只读产物：trace.jsonl / actions.json / screenshots / report.html
             ▼
       运行目录（bridge/ 子目录留往返审计）
```

一轮完整回合（以「全开」为例，Jev 决策 + agent 监督）：

```
内核                                   对话 agent
 │ run 启动（后台）
 │ Jev 决策 ×N（HTTP，不经 agent）
 │ …… 触发卡住/断言未过/预算等
 │ 写 ask-0007.json（kind=supervise）─→ bridge wait 拿到 ask
 │ 轮询 ans-0007.json ←─────────────── 读 state/触发原因 → 作答 → bridge answer
 │ parse_ruling 校验（不合规=不可用）
 │ 执行裁决（RECOVER 走普通执行路径）
 │ …… 继续 / 停机
 │ 写 status.json（finished）────────→ bridge wait 返回 finished → 汇报用户
```

**关键设计点**：

- 内核主循环不动：预算（`SUPERVISOR_PER_STEP/RUN`）、卡住检测、回放缓存、守卫、降级阶梯全部照旧，
  桥只是把「四个 LLM 缝里的 HTTP 调用」换成了「问宿主 agent」。
- 桥答复 = 模型输出，**同等不信任**：supervise 答复走 `parse_ruling`，decide 答复走
  `assemble_decision/validate_choice`，text 答复走 textfill 的现状校验。答歪 → 与 HTTP 缝完全相同的处置链
  （重问一次 → 升级 / M0 停机）。
- 桥不可用（agent 超时 / 文件不可写 / 答复不合规）→ 一律 unavailable → 按 M0 语义停机，与「没配监督模型」行为一致（既有铁律）。

### 2.1 桥目录与文件格式

桥目录默认 `<run-dir>/bridge/`（内核在 run 启动时创建）：

```
<run-dir>/bridge/
  status.json     # {"state": "running|finished|crashed", "seq": 7, "detail": "...", "updated_at": ...}
  ask-0007.json   # 内核写入（原子：tmp + os.replace）
  ans-0007.json   # agent 写入（越早越好，deadline 内）
```

ask 信封（`bridge/protocol.py` pydantic 契约）：

```json
{
  "id": 7,
  "kind": "supervise | decide | text",
  "at": "2026-10-16T14:03:22+08:00",
  "deadline_s": 600,
  "payload": { }
}
```

| kind | payload（与现有 HTTP 缝同构） | 期望答复 |
|---|---|---|
| `supervise` | 现状 `supervision_context()`：`{stuck:{trigger,detail}, current_goal, remaining_goals, state}`（detail 已含 B3 页面变化历史） | `{"ruling": "HINT|RECOVER|REPLAN|ABORT", ...}`，过 `parse_ruling` |
| `decide` | 现状 delegate 的 user dict：`{state, questions, previous_error?}`（与 Jev 请求同构） | `{"answers": {...}}`（或 `{"error": "..."}` = 主动中止），过 `validate_choice` |
| `text` | 现状 `field_context()` + 未命中 vars 提示 | `{"text": "..."}`，同 textfill 校验（非空、≤2000） |

答复信封 `ans-<id>.json`：`{"id": 7, "answer": {…}, "answered_by": "agent", "note": "可选"}`。

ask 信封预留 `payload.attachments: []`（v1 恒为空）。未来若放开「监督者看截图」，内核只在 supervise ask 里附
`{kind:"screenshot", path}`，宿主自行决定是否读取——协议与校验不变，只改内核填充与 SKILL.md 纪律。

### 2.2 agent 侧 CLI（新增子命令）

```bash
# 阻塞等待下一个事件；到时限没事件返回 heartbeat，让 agent 循环续等
uv run ui-agent bridge wait --run-dir <run-dir> [--timeout 240] --json
#   → {"ok": true, "event": "ask", "kind": "supervise", "ask": {...}}
#   → {"ok": true, "event": "heartbeat"}
#   → {"ok": true, "event": "finished", "state": "finished|crashed", "result": {...}}

# 答复（原子写入 + 回读校验格式；晚到/重复答复返回 accepted=false, reason=stale）
uv run ui-agent bridge answer --run-dir <run-dir> --id 7 --file ans.json --json
uv run ui-agent bridge answer --run-dir <run-dir> --id 7 --data '{"answer": {...}}' --json
```

设计口径：

- `bridge wait` 是**无状态轮询**：任何新 shell 里都能调；`--timeout` 只是单次调用时长（宿主工具时限内取值，agent 循环续等即可）。
- 单运行单桥（一个桥目录一个后台 run）；不做多路 claim，假设单一 agent。
- 内核侧的 `AgentBridge.ask()` 轮询 ans 文件（250ms），deadline（默认 `UI_AGENT_BRIDGE_TIMEOUT_S=600`，可配）到即抛 unavailable。

### 2.3 边界与失败语义（写死，逐条进单测）

| 场景 | 行为 |
|---|---|
| agent 在 deadline 内没答 | 该缝 unavailable → M0 语义停机；trace `model`/`supervisor` 行带 `error=bridge timeout` |
| 答复 JSON 坏 / 校验不过 | 与 HTTP 缝「答歪」完全相同：supervise → unavailable；decide → 重问 1 次（带 previous_error），仍败 → 有监督走 `decide_invalid` 升级，无监督硬错；text → 该字段拒填记失败 |
| ans 晚于内核推进 | `bridge answer` 返回 `stale`（忽略，不报错）；ask 文件保留在 bridge/ 作审计 |
| 内核进程崩溃 / 被杀 | status.json 写 `crashed` + 最后异常摘要；`bridge wait` 返回 finished(crashed)，result.json 可能不完整 |
| 桥目录不可写 | 启动即报错（装配错误，不静默降级） |
| 双 shell 同时 wait | 无害（只读）；answer 幂等（首个落盘生效，后续 stale） |

### 2.4 与 MCP 适配器的关系（可选插件形态）

桥是**协议**，MCP 适配器是**另一种传输/封装**——两者不是二选一：

| 维度 | Agent 桥（skill 形态） | MCP 适配器（插件形态） |
|---|---|---|
| 本质 | ask/ans 文件协议 + `bridge wait/answer` CLI | 同一协议的 MCP 工具封装，薄壳 |
| 内核改动 | 必须（桥模块 + seam 接线） | 零（复用同一 `AgentBridge` 客户端） |
| 宿主要求 | 有 shell：后台作业 + 阻塞命令 + 文件读写 | 宿主支持注册 MCP server |
| 安装 | 装 skill 目录 + `uv sync` | 在宿主配置 MCP server（command/env） |
| 进程/浏览器生命周期 | 内核后台进程自管（与现状一致） | MCP server 常驻进程管；多 run 需 session 管理 |
| 长等待 | `bridge wait` 单次调用阻塞，受宿主 shell 超时约束 | 长轮询工具调用受宿主工具时限约束，常需分片轮询 |
| 通用性 | 高（「任意 agent」的最低共同能力） | 中（仅 MCP 宿主） |

行为语义完全一致：同一套 ask/answer、同一套校验、同一套预算/缓存/trace。
MCP 工具面（后置）示意：`ui_agent_start(plan_file, url, vars, decider, supervisor) → run_id`、
`ui_agent_next(run_id, timeout_s) → ask|heartbeat|finished`、`ui_agent_answer(run_id, ask_id, answer)`、
`ui_agent_status/abort(run_id)`。

**为什么先做桥**：MCP 适配器内部仍要实现同一套问答语义，桥无论如何都得先有；桥是通用性最高的传输；
skill 形态迭代与评审成本最低。适配器可以后续以「零内核改动」的薄壳补上，不影响本 spec 的协议设计。

---

## 3. 缝分工（全部四个缝的最终归属）

| 缝 | Skill 形态 | 备注 |
|---|---|---|
| planner | **agent 直写用例 YAML**（schema 见 `references/cases.md`）→ `ui-agent case compile --in X.yaml --json` 校验到 0 error → 拿 `plan` 落盘跑 | agent 本身就是 planner，无需桥；HTTP 版 `plan`/`case draft` 原样保留（平台用） |
| decide | `--decider jev`（用户 key，**推荐**）| `--decider agent`（桥，kind=decide，无 key 时兜底）| 回复缓存（`--decider off`）不变 |
| supervise | `--supervisor agent`（桥，kind=supervise）| HTTP 版 `--supervisor on` 保留（平台用） |
| textfill | `--var` 优先不变；缺值：supervisor=agent 时走桥（kind=text） | 敏感字段（`secret` 标记）仍拒经任何模型/桥供值，只能 `--var` |

新增开关取值（向后兼容，平台现传参数不受影响）：

- `--decider jev | llm | agent | off`（新增 `agent`）
- `--supervisor on | off | agent`（新增 `agent`；映射到 `ui_agent_supervisor_mode: model|agent`）
- 新配置：`ui_agent_bridge_dir`（空 = `<run-dir>/bridge`）、`ui_agent_bridge_timeout_s`（默认 600）

### 与平台四档开关的等价映射（SKILL.md 放同一张表）

| 平台四档 | Skill 开关 | 决策者 | 监督者 |
|---|---|---|---|
| 全关 | `--decider off --supervisor off` | 回放缓存 | 无（失配即停） |
| 只开 Jev | `--decider jev --supervisor off` | Jev | 无（阻断即 M0 停） |
| 只开大模型 | `--decider agent --supervisor agent` | 对话大模型 | 对话大模型 |
| **全开** | `--decider jev --supervisor agent` | **Jev** | **对话大模型**（本次主目标） |

---

## 4. Skill 打包形态

`skills/ui-agent/` 放 ui_agent 仓库内（随内核同版本）；安装方式按宿主约定（Qoder 技能目录 / `.claude/skills/` / 内部 jx-skills 库 / `npx skills add` 布局兼容）：

```
skills/ui-agent/
  SKILL.md                 # frontmatter（name+description）+ 路由表 + 铁律 + 顶层工作流
  references/
    setup.md               # 一次性环境准备：uv / chromium / 可选 Jev key / headless 说明
    cases.md               # 用例 YAML 写法：步骤、8 种断言、vars 规范、编译校验口径
    running.md             # 后台启动 run + bridge wait/answer 循环 + 读产物
    supervising.md         # supervise ask 处置手册（四种裁决的合法落点与纪律）
    deciding.md            # decider=agent 时的作答契约（候选键全集、概率规则、错误重问）
    debugging.md           # trace.jsonl 分层 / cache 行词表 / 停机状态词表 / 失败截图路径
    security.md            # 密钥、vars、凭据、落盘边界
  scripts/
    bootstrap.sh / bootstrap.ps1   # 幂等：uv sync + playwright install chromium（装进 .browsers/）
```

SKILL.md 工作流（教 agent 怎么带用户跑一遍）：

1. **环境自检**（一次性）：`scripts/bootstrap` 装 `.venv` + Chromium；告诉用户装了什么。
2. **收需求**：用户一句话流程 → agent 写 `<用例库>/<场景>/<name>.yaml` → `case compile` 校验 → 落 `plan.json`。
3. **定决策者**：用户给了 Jev key → 本轮导出 `TYPESAFE_API_KEY`（默认端点已是 dmxapi 中转 `https://www.dmxapi.cn/v1/systemone`；换站点再给 `TYPESAFE_BASE_URL` / `TYPESAFE_AUTH_MODE`），走「全开」；没有 key → **默认走「只开大模型」**（`--decider agent --supervisor agent`），开跑前一句话说明「我将用当前对话模型逐跳作答，会消耗 token」，不拦截、不追问。
4. **跑**：`run --plan-file … --supervisor agent [--decider jev]` 后台启动 → 循环 `bridge wait` → 收到 supervise ask 按 `references/supervising.md` 处置 → `bridge answer` → `finished` 后读 `result.json` / `report.html` 汇报（状态、逐步断言、缺陷候选）。
5. **复跑省 token**：`--decider off --supervisor off` 零模型回放；失配词表（no-entry / no-variant / wrong-context …）教 agent 读 cache 行并决定「重录还是改断言」。
6. **收尾纪律**：每次回答前先看 `deadline_s`；阻断处置不做长调研（要长调研就先 HINT 或 ABORT 整理后续跑）；一轮跑完必须确认 status.json 终态与 result.json 一致。

铁律节（与仓库 AGENTS.md 硬线逐条对齐，skill 侧新增两条）：

- 桥答复 = 模型输出：不成 selector / 坐标 / 可执行 JS；元素只认当次快照 index；执行前守卫照旧。
- **v1 监督只看文本证据**（state + trace + actions），截图仍不进任何模型输入——沿用「截图只进报告」硬线；
  ask 协议已预留 `payload.attachments`（v1 恒空），未来放开只改内核填充与 SKILL.md 纪律、不改协议。
- 密钥只进环境变量（agent 会话内导出）；不写进 skill 文件、用例文件、产物。
- `--var` 值、上传文件路径不落盘；密码等敏感字段只经 `--var`。

---

## 5. 内核改动清单（文件级）

| 文件 | 改动 |
|---|---|
| `src/ui_agent/bridge/__init__.py`、`protocol.py` | 新增：ask/ans/status pydantic 契约 + 原子读写 |
| `src/ui_agent/bridge/client.py` | 新增：`AgentBridge.ask(kind, payload, deadline)`（内核侧轮询阻塞）；`wait()`/`answer()`（CLI 侧复用） |
| `src/ui_agent/llm/supervisor.py` | `supervisor_mode=agent` 时改走桥（kind=supervise）；`available` = 桥就绪；`parse_ruling` 校验不变 |
| `src/ui_agent/decide/agent_engine.py` | 新增 `AgentEngine`（kind=decide）；复用 `build_questions`/`assemble_decision`；重问口径对齐 delegate（ATTEMPTS=2，带 previous_error），**不做 Phase 2**（对话模型无需快/慢路径）；`model="agent"` |
| `src/ui_agent/llm/textfill.py` | supervisor 为 agent 模式时缺值走桥（kind=text）；校验与 meta.source 语义同现状 |
| `src/ui_agent/decide/engine.py` | `ENGINE_KINDS` 加 `agent` 分支 |
| `src/ui_agent/config.py` | 新增 `ui_agent_supervisor_mode`、`ui_agent_bridge_dir`、`ui_agent_bridge_timeout_s` |
| `src/ui_agent/cli.py` | `--decider` 加 `agent`；`--supervisor` 加 `agent`；新增 `bridge wait` / `bridge answer` 子命令（--json 契约不变）；`run_start` 行记桥配置 |
| `src/ui_agent/run/runner.py`、`plan_runner.py` | 装配 AgentEngine/桥监督；trace `model`/`supervisor` 行加 `via: "http"|"agent"` 字段；停机文案补桥超时/桥不可用两种 unavailable 原因 |
| `src/ui_agent/run/trace.py` | 词表补 `via` 与 bridge 相关 error 文案 |
| `tests/fakes.py`、`tests/test_bridge*.py` | 新增：脚本化假 agent 驱动桥（离网）——超时/答歪/重问/stale/crashed 全链路 |
| `scripts/smoke_bridge.py` | 新增冒烟：真 CLI + 真 Chromium + 假 agent 应答，跑「Jev=脚本化 + supervisor=桥」一条用例，零外网 |
| `README.md`、`AGENTS.md` | 新增「Agent 桥 / skill」节：开关表、四档映射、桥协议指向 spec |

不动：`observe/` `act/` `verify/` `replay/` `case/` `report/` `driver/` 全部。

### 5.1 装配约束（落码前确认，2026-10-19 补记）

这几条不是设计变更，而是现有构造顺序推出来的硬约束；落码时先按它接线，别再踩：

1. **agent 模式下 `--run-dir` 必填**：桥默认在 `<run-dir>/bridge`，宿主必须在启动后台进程**之前**
   就知道路径。`--decider agent` 或 `--supervisor agent` 时 CLI 直接报错要求显式 `--run-dir`
   （不要依赖「跑完 JSON 里再给 run_dir」——那时 ask 早就超时了）。
2. **桥由 CLI/外层装配**：`Runner`/`PlanRunner` 的 `__init__` 早于 `run_dir` 确定，禁止在里面
   猜桥路径。CLI 解析出 run_dir 后创建 `AgentBridge`，再传给 `make_engine(..., bridge=)`、
   `AgentSupervisor(bridge)` 和 `Runner/PlanRunner`；测试可直接传临时目录的桥。
   plan 模式必须**全计划共用同一个 bridge + 同一个 supervisor 预算**，不能每步新建。
3. **AgentEngine 不做 Phase 2**：runner 现有的 Phase 2 是「HTTP 大模型思考路径」；`decider=agent`
   两次失败（runner 已带 previous_error 重问）后直接走 `decide_invalid` 升级（supervisor=agent 时
   就是一次 supervise ask），不再尝试创建 `LlmEngine`。
4. **agent 给的 TYPE_TEXT 值要进录像**：现录制条件只认 `source == "text_model"`；必须扩到
   `source in ("text_model", "agent")`（或 `!= "var"`），否则零模型回放会缺字段值。
5. **secret 字段不走桥**：`element.secret` 为真时 text seam 必须拒绝（提示只能 `--var`）。
   注意现有 HTTP textfill 并没有这条硬拒绝，M-A **不要顺手改 HTTP 行为**（保持内核现状），
   只在桥路径加；是否统一收紧留给后续拍板。
6. **状态文件生命周期**：CLI 在 run 开始前写 `status=running`，正常/异常收尾分别写
   `finished`/`crashed`；`bridge wait` 遇到 status 不存在按 running 处理（启动竞态），
   不要直接报 finished。

回放缓存与桥天然协同：缓存录的是「动作」不是「模型轮次」（录制点与决策者无关），
`--decider agent` 产出的决策同样可录；第二轮 `--decider off --supervisor off` 零模型复跑，
**对话模型的 token 成本只在首次录制**。

---

## 6. 安全与兼容边界

- **密钥**：Jev key 由用户在会话里给，agent 导出为子进程环境变量；`.env` 持久化非默认（需用户明说）。
  中转站口径（bearer / appcode）沿用现有 `TYPESAFE_AUTH_MODE`。
- **宿主兼容矩阵**（SKILL.md 注明）：

| 宿主能力 | 结果 |
|---|---|
| 后台作业 + 阻塞命令（Qoder / Claude Code 等） | 全功能（推荐路径） |
| 只有同步命令 | `bridge wait --timeout 30` 短轮询也可全功能（调用次数多些） |
| 无后台/文件能力 | 退回 `--decider llm --supervisor on`（HTTP 文本模型，旧形态）或纯回放 |

- **v1 非目标**：MCP 适配器（同协议薄壳，后置）；工具环（decide 槽位）接线；批量回归（平台已有）；
  平台 UI / 四档 HTTP 形态改造；监督模型看截图（先议后做，本轮保持文本）。

## 7. 验收（切片与口径）

- **M-A 内核桥（离线为主）**：bridge 模块 + 两处 seam + CLI + 边界语义全落码；
  新增 pytest（假 agent 脚本驱动）+ `smoke_bridge.py`；回归口径含 ruff、全量 pytest、九套既有冒烟全绿。
  **全部改动只在隔离副本 `ui_agent_skill_dev/kernel` 内进行**；用户确认不影响平台后才合并回源仓库，
  再由源仓库单向同步到平台副本（流程见 `HANDOFF.md`）。
- **M-B skill 打包**：SKILL.md + 7 篇 references + bootstrap 双平台脚本；commands 逐个实跑核对；
  在本机 Qoder 会话内实测安装（用户目录技能目录），确认 agent 能按 SKILL.md 独立走完流程。
- **M-C 真跑验收（对话内）**：
  1. 无 key：`--decider agent --supervisor agent` 跑本地夹具用例（`examples/local_fixture.html` 或 m3 夹具），全程对话大模型作答；
  2. 有 key：`--decider jev --supervisor agent` 跑一条真页面用例（登录 + 查询），常态零打断；人为制造一次阻断（点不存在的目标），验证 supervise 往返 + 裁决执行 + trace `via=agent` 行；
  3. 复跑：同用例零模型回放全绿（`--decider off --supervisor off`），断言与首跑一致。
- **DoD**：三条切片各有证据（命令输出/产物路径）；文档（README/AGENTS）同步；未提交前不经用户点名不 commit。

## 8. 拍板状态

**已拍板（2026-10-19）**：

1. **无 Jev key** → 默认「只开大模型」（`--decider agent --supervisor agent`）；
   SKILL.md 开跑前说明 token 成本，不拦截、不追问；CLI 层不做自动回退。
2. **监督者 v1 只看文本**；ask 协议预留 `payload.attachments`（v1 恒空），为后续看截图留好位置。
3. **Jev key 注入**：会话内 env 导出为默认；`.env` 明文持久化也接受（仅本机自用场景）。
4. **用例草稿**：CLI 保留 HTTP 版 `plan` / `case draft`（平台依赖，不删）；skill 只走
   「agent 直写 YAML + `case compile` 校验」，不使用 HTTP 双轨——不构成选择。
5. **双轨保留（2026-10-19）**：内核现有功能一律不变——HTTP `plan` / `case draft`、
   `--decider llm`、`--supervisor on`、平台四档开关、回放缓存、trace/产物契约全部保留；
   skill/桥是**纯加法**。所有内核改动先在隔离副本做，确认不影响平台后再合并。

**仍待定**：

5. **bridge 单次 ask 超时默认值**：暂定 600s，落码后按 M-C 实测校准（每 ask 可配）。
6. **形态**：MCP 适配器是否随本期做（桥协议不受影响，差异见 2.4 节；推荐后置）。
