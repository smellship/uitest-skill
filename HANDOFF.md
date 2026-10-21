# ui_agent → Skill 抽取 · M-A 交接文档

- 日期：2026-10-19（M-A 交接时的快照；M-A / M-B 已全部落码，见下）
- 交接对象：接手 **M-A（内核 Agent 桥 + 四个缝接线）** 落码的 agent
- 设计源：[spec-agent-skill.md](kernel/spec-agent-skill.md)（v2，第 2/3/5/8 节必读）
- 原件阶段：隔离副本已建、基线已验、代码 0 改动、M-A 未落码

> **本文是历史文档。** 它记录的是 M-A 开工前的计划与约束，其中的「当前阶段」「待办」「未落码」
> 描述都已过时，不要按它判断今天能做什么。今天的事实以这三处为准：
>
> - `README.md`（仓库总览 + 两种跑法 + 当前状态）
> - `ui-agent/SKILL.md`（给宿主 agent 的入口；11 篇 references 按需读）
> - `kernel/AGENTS.md`（硬约束 + 已落码事实 + 明确的待办清单）
>
> 本文仍然有价值的部分：第 1 节的工作区边界、第 8 节的硬约束与安全边界、第 9 节的行号锚点思路。
> 桥的超时默认值是 **180s**（本文第 8、10 节写的是 600s，那是当时的计划值）。
- 一句话目标：在副本内核里加「文件桥」，让宿主对话大模型接替 planner / textfill / supervise / decide 四个 LLM 缝；Jev 决策保留；平台现有 HTTP 双轨与内核全部功能原样不动。

---

## 0. TL;DR — 接手后先做什么

1. 读本文第 1、2、3、5 节 + spec 第 2、3、8 节；不要先改代码。
2. `cd /d/test_skills/ui_agent_skill_dev/kernel && uv sync`，跑一遍第 4 节基线，确认 359 单测 + 冒烟全绿。
3. 按第 5 节的文件级设计落码；所有改动**只在这个副本**里。
4. 按第 6 节补单测与 `scripts/smoke_bridge.py`；再跑第 4 节全部回归（ruff + pytest + 九套冒烟）。
5. 完成后向用户报告：改动清单、测试证据、已知风险。**不要 commit、不要 push、不要合并回源仓库、不要同步平台**——等用户确认。

---

## 1. 工作区与边界（最重要）

| 目录 | 角色 | 当前状态 | 允许改？ |
|---|---|---|---|
| `D:\test_skills\ui_agent_skill_dev\kernel` | **开发副本（唯一工作区）** | 从源仓库工作树完整复制；复制时 96 个源文件逐字节一致；当前唯一差异是副本内 spec 补记（见 1.3）；自带 `.venv`；无 `.browsers`、无 `.env`、无 `.git` | ✅ 只改这里 |
| `D:\test_skills\ui_agent` | 源仓库（上游） | 分支 `master`，HEAD `4a441ed`；工作树有 `M spec-agent-skill.md` | ❌ 用户确认前不动 |
| `D:\test_skills\ui尝试\ui_backend\kernel` | 平台的**单向同步副本** | 2026-10-15 从源仓库 `822f0fd` 同步，95 文件；平台后端只按 argv 调 CLI + 读产物，不 import 内核 | ❌ 永远不要手改 |
| `D:\test_skills` | 外层 git 仓库 | 副本位于它内部 | ⚠️ 见下 |

### 1.1 三个必须知道的坑

1. **副本没有自己的 `.git`**。在副本目录里跑 `git status` / `git add` / `git commit`，操作的是外层 `D:\test_skills` 仓库，不是 ui_agent。不要在那里做任何 git 操作。
2. **副本没有 `.env`**（里面有密钥，刻意没复制）；`TEXT_MODEL_API_KEY` / `TYPESAFE_API_KEY` 一律通过会话环境变量传入。测试不得调用付费 API。
3. **浏览器没有复制**（源仓库 `.browsers` 约 707MB）。跑真实浏览器冒烟时复用源仓库的浏览器目录：
   ```bash
   export PLAYWRIGHT_BROWSERS_PATH="D:/test_skills/ui_agent/.browsers"
   export PYTHONUTF8=1
   ```

### 1.2 合并原则

- 一切内核改动先在副本做；副本 `ruff + pytest + 九套冒烟` 全绿，且用户明确确认「不影响平台」后，才按第 8 节合并回源仓库。
- 合并后再由源仓库走 `ui_backend/scripts/sync_kernel.py` 单向同步到平台副本；平台副本不手工编辑。
- 未经用户明确要求，不 commit、不 push。

### 1.3 当前副本与源仓库的唯一差异

副本内 `spec-agent-skill.md` 已补：
- 文件头加了「落码位置」；
- 新增 **5.1 装配约束**（`--run-dir` 必填、桥由 CLI 装配、AgentEngine 不做 Phase 2、agent 文本进录像、secret 不走桥、状态文件生命周期）；
- 第 7 节 M-A 改为「先在隔离副本完成」；
- 第 8 节新增第 5 项「双轨保留」。

源仓库的 spec 还没有这些补记；合并时一并带过去。

---

## 2. 目标与架构（3 分钟版）

### 2.1 现状：四个 LLM 缝都走 HTTP

| 缝 | 文件 | 职责 | 现有开关 |
|---|---|---|---|
| planner | `src/ui_agent/llm/planner.py` | 自然语言流程 → 分步计划 | `plan` / `case draft` 命令内调 HTTP |
| textfill | `src/ui_agent/llm/textfill.py` | TYPE_TEXT 字段取值 | `--var` 优先，缺值调 HTTP 文本模型 |
| supervisor | `src/ui_agent/llm/supervisor.py` | 卡住/阻断时一次裁决 HINT/RECOVER/REPLAN/ABORT | `--supervisor on` + `TEXT_MODEL_API_KEY` |
| delegate | `src/ui_agent/decide/delegate.py` | 大模型代班 Jev 做决策 | `--decider llm` + `TEXT_MODEL_API_KEY` |

Jev 决策（`decide/jev.py`）本来就是「用户 key → HTTP」；执行、守卫、断言、回放缓存与模型无关。

### 2.2 目标：文件桥

对话 LLM 不能被 subprocess 同步调用，但任何 agent 都能：跑后台命令、阻塞等待、读写文件。因此内核加一个 **Agent 桥**：

```
用户 ⇄ 对话 agent（大模型：编排 / 监督 / 兜底决策）
             │ ① 直接写用例 YAML → case compile       ② bridge wait / answer 循环
             ▼
     ui-agent CLI（后台进程，Playwright 常驻）
       observe → decide(Jev | 回放 | 桥) → guard → execute → verify → 记录缓存
             │ ③ 只读产物：trace.jsonl / actions.json / screenshots / report.html
             ▼
       <run-dir>/bridge/{ask-*.json, ans-*.json, status.json}
```

- 内核主循环不动：预算、卡住检测、回放缓存、守卫、降级阶梯全部照旧。
- 桥答复 = 模型输出，**同等不信任**：supervise 过 `parse_ruling`，decide 过 `assemble_decision/validate_choice`，text 过 textfill 校验。
- 桥不可用（超时/文件错/答歪）→ 与对应 HTTP 缝完全相同的处置链；监督缝一律 unavailable → M0 停机语义。
- v1 监督只看文本；ask 协议预留 `payload.attachments: []`，为未来截图留口，但 v1 恒为空。

### 2.3 四档开关映射

| 形态 | 开关 | 决策者 | 监督者 |
|---|---|---|---|
| 全关 | `--decider off --supervisor off` | 回放缓存 | 无（失配即停） |
| 只开 Jev | `--decider jev --supervisor off` | Jev | 无 |
| 只开大模型 | `--decider agent --supervisor agent` | 对话大模型（桥） | 对话大模型（桥） |
| **全开（主目标）** | `--decider jev --supervisor agent` | **Jev** | **对话大模型（桥）** |

### 2.4 M-A 范围

**只做内核桥与接线**：
- `bridge/` 包（协议 + 客户端）；
- `AgentEngine`、`AgentSupervisor`、textfill 桥路径；
- CLI `bridge wait` / `bridge answer`、`--decider agent`、`--supervisor agent`；
- runner / plan_runner 装配、trace `via`、`--run-dir` 约束；
- 单测 + `smoke_bridge.py` + 文档。

**不做**：skill 包（M-B）、真实平台页面验收（M-C）、MCP 适配器、截图监督、工具环。

---

## 3. 已拍板与待定

### 3.1 已拍板（5 项）

1. **无 Jev key** → 默认「只开大模型」：`--decider agent --supervisor agent`；开跑前告知 token 成本，不拦截、不追问；CLI 不做自动回退。
2. **监督者 v1 只看文本**；ask 协议预留 `payload.attachments`（v1 恒空），未来放开只改内核填充与文档，不改协议。
3. **Jev key 注入**：会话内 env 导出为默认；本机自用允许明文 `.env`（仅用户自己环境）。
4. **用例草稿双轨**：HTTP `plan` / `case draft` 原样保留给平台；skill 只走「agent 直写 YAML + `case compile` 校验」。
5. **双轨保留（2026-10-19）**：内核现有功能一律不变——HTTP `plan`/`case draft`、`--decider llm`、`--supervisor on`、平台四档、回放缓存、trace/产物契约全部保留；桥是纯加法。所有改动先在隔离副本做。

### 3.2 仍待定（2 项，落码时按建议值先做）

| 项 | 建议 | 说明 |
|---|---|---|
| bridge 单次 ask 超时默认值 | **600s**（`UI_AGENT_BRIDGE_TIMEOUT_S`，可配） | M-C 实测后校准；别把 600 硬编码到多处 |
| MCP 适配器是否本期做 | **后置** | 桥是协议，MCP 只是同协议的薄传输壳；内核零改动即可后补 |

---

## 4. 环境与基线（先在副本复跑一遍再开工）

```bash
cd /d/test_skills/ui_agent_skill_dev/kernel
uv sync

# 离线单测（不联网、不启浏览器）
uv run ruff check .
uv run pytest -q

# 真实浏览器冒烟：复用源仓库浏览器，避免重新下载
export PLAYWRIGHT_BROWSERS_PATH="D:/test_skills/ui_agent/.browsers"
export PYTHONUTF8=1
uv run python scripts/smoke_snapshot.py
uv run python scripts/smoke_replay.py
```

已在副本验证过的基线（2026-10-19）：

- `uv run ruff check .` → `All checks passed!`
- `uv run pytest -q` → **359 passed**
- `scripts/smoke_snapshot.py` → 通过（快照 / 守卫 / 阶梯 / 断言，零模型）
- `scripts/smoke_replay.py` → 通过（录制 / 零模型回放 / 零模型失配，真 CLI + 真 Chromium）
- 副本与源仓库 96 个源文件逐字节一致（排除 `.git/.venv/.browsers/.artifacts/缓存/.env`）

全部九套冒烟（M-A 收尾必须全绿）：

```bash
uv run python scripts/smoke_cli.py
uv run python scripts/smoke_snapshot.py
uv run python scripts/smoke_plan.py
uv run python scripts/smoke_supervisor.py
uv run python scripts/smoke_upload.py
uv run python scripts/smoke_m3.py
uv run python scripts/smoke_assert.py
uv run python scripts/smoke_replay.py
uv run python scripts/smoke_browser.py
```

> 测试/冒烟一律不得调用付费 API（TypeSafe / 文本模型）。需要「模型」时用 `tests/fakes.py` 的脚本替身或假桥。

---

## 5. M-A 文件级落码设计

### 5.1 新增 `src/ui_agent/bridge/` 包

#### `bridge/__init__.py`
导出 `AgentBridge`、`Ask`、`Answer`、`BridgeStatus`、`BridgeError`、`BridgeTimeout`、`BridgeUnavailable`。

#### `bridge/protocol.py`
Pydantic 契约 + 原子读写 + 文件名约定。

```python
KINDS = ("supervise", "decide", "text")
STATES = ("running", "finished", "crashed")

class Ask(BaseModel):
    id: int
    kind: str
    at: str
    deadline_s: float
    payload: dict = {}

class Answer(BaseModel):
    id: int
    answer: dict
    answered_by: str = "agent"
    note: str = ""

class BridgeStatus(BaseModel):
    state: str = "running"
    seq: int = 0
    detail: str = ""
    updated_at: str = ""
```

文件命名：
- `ask-{id:04d}.json`、`ans-{id:04d}.json`、`status.json`
- 原子写：同目录 `*.tmp` → `os.replace`；UTF-8、`ensure_ascii=False`。
- 读：`read_json(path)`；文件不存在返回 `None`；JSON 坏抛 `BridgeError`。
- `deadline_s` 用浮点也兼容整数；`payload` 里 `attachments` 默认 `[]`（v1 恒空）。
- 时间用 `datetime.now().isoformat(timespec="milliseconds")`（本机时区即可，桥是本机文件）。

#### `bridge/client.py`
内核侧与 CLI 侧共用的 `AgentBridge`：

```python
class AgentBridge:
    def __init__(self, bridge_dir: Path, timeout_s: float = 600.0,
                 poll_s: float = 0.25, clock=time.monotonic, sleeper=time.sleep): ...

    # 内核侧
    def start(self) -> None: ...          # mkdir -p；不可写立刻抛 BridgeUnavailable；status=running seq=0
    def ask(self, kind: str, payload: dict) -> tuple[dict, dict]: ...
    def finish(self, detail: str = "") -> None: ...
    def crashed(self, detail: str) -> None: ...
    def finish_from_result(self, result: dict) -> None: ...

    # CLI 侧
    def wait(self, timeout_s: float) -> dict: ...
    def answer(self, ask_id: int, answer: dict, note: str = "") -> dict: ...
```

**`ask(kind, payload)` 语义**：
- `id = 当前最大 ask/status seq + 1`（从目录扫描初始化，防止 CLI 重启撞号）。
- 写 `ask-{id}.json`（信封含 `deadline_s=self.timeout_s`），更新 `status.json`（running, seq=id）。
- 每 `poll_s` 读 `ans-{id}.json`；读到后校验 `Answer.id == id`，返回 `(answer_dict, meta)`。
- `meta` 至少包含：`{"via": "agent", "ask_id": id, "answered_by": ..., "latency_ms": ...}`。
- 到 deadline 抛 `BridgeTimeout("bridge timeout: kind=%s id=%d after %.1fs")`。
- 答复文件 JSON 坏 / 结构不合法 / id 不匹配：抛 `BridgeUnavailable`（决定缝可在上层转成 `ValueError` 以触发重问；见 5.4）。
- 轮询用注入的 `clock/sleeper`，单测不要真睡。

**`wait(timeout_s)` 语义**（无状态轮询）：
- status 不存在：按 `running` 处理（启动竞态）。
- status 为 `finished`/`crashed`：**优先**返回 `{"ok": True, "event": "finished", "state": ..., "detail": ..., "result": {...}}`；`result` 读 `result.json`（计划模式退回 `summary.json`），读不到给 `null`。已结束的运行即使还有过期 ask，也不再让宿主作答。
- 否则找到**最早一条没有 ans 的 ask**：返回 `{"ok": True, "event": "ask", "kind": ..., "ask": {...}}`。
- 超时无事件：返回 `{"ok": True, "event": "heartbeat"}`。
- 不写任何文件；不要 claim/加锁（单运行单桥假设）。

**`answer(ask_id, answer, note)` 语义**：
- 桥目录不存在 / ask 不存在 / status 已 finished/crashed / ans 已存在 → `{"ok": True, "accepted": False, "reason": "stale"}`。
- 成功 → 原子写 ans，`{"ok": True, "accepted": True, "id": ask_id}`。
- `answer` 必须是 dict；CLI 侧先校验。
- 重复答复幂等：第一个落盘生效，后续 stale。

**状态生命周期**：
- CLI 在 run 开始前 `start()`；runner 正常返回（包括 `result.status="error"`）后 `finish_from_result()` → `finished`。
- 未捕获异常 / 键盘中断 → `crashed(detail)`。
- 内核侧不直接写 `finished`（由 CLI/外层负责），避免 Runner 被测试直调时产物混乱。

### 5.2 `src/ui_agent/config.py`

新增字段（保持向后兼容）：

```python
ui_agent_supervisor_mode: str = "model"   # model=HTTP 文本模型；agent=文件桥
ui_agent_bridge_dir: str = ""             # 空 = <run-dir>/bridge
ui_agent_bridge_timeout_s: float = 600.0
```

保留：
- `ui_agent_supervisor: bool = True` 仍是**总闸**：False 时忽略 mode，监督缝关闭、textfill 只认 `--var`。
- `ui_agent_decider: str = "jev"` 取值扩为 `jev|llm|agent|off`。

`--supervisor` 的映射（在 `cli.apply_overrides` 做）：
- `off` → `ui_agent_supervisor=False`
- `on` → `ui_agent_supervisor=True`、`ui_agent_supervisor_mode="model"`
- `agent` → `ui_agent_supervisor=True`、`ui_agent_supervisor_mode="agent"`

`bridge_dir` 解析 helper 建议放在 CLI 或 bridge 包：

```python
def resolve_bridge_dir(settings, run_dir: Path) -> Path:
    return Path(settings.ui_agent_bridge_dir) if settings.ui_agent_bridge_dir else run_dir / "bridge"
```

### 5.3 桥的装配位置（CLI 持有）

原因：`Runner.__init__` / `PlanRunner.__init__` 早于 `run_dir` 确定；桥默认在 `<run-dir>/bridge`，不能在里面猜。

**CLI `_run_goal` / `_run_plan`**：
1. `settings = apply_overrides(Settings(), args)`。
2. 若 `settings.ui_agent_decider == "agent"` 或 `settings.ui_agent_supervisor_mode == "agent"`（且 supervisor 开）：
   - `--run-dir` 必须显式给出；否则 `raise RuntimeError("agent 模式必须指定 --run-dir：宿主需要在启动前知道桥目录")`。
   - `run_dir = Path(args.run_dir)`；`run_dir.mkdir(parents=True, exist_ok=True)`。
   - `bridge = AgentBridge(resolve_bridge_dir(settings, run_dir), settings.ui_agent_bridge_timeout_s)`；`bridge.start()`。
3. 把 `bridge=` 传给 `Runner(...)` / `PlanRunner(...)`。
4. `try/finally`：正常返回 `bridge.finish_from_result(...)`；异常 `bridge.crashed(...)` 后继续抛/按原错误处理。

**Runner / PlanRunner** 新增可选参数 `bridge: AgentBridge | None = None`：
- `self.engine = engine or make_engine(settings, bridge=bridge)`；
- supervisor：显式传入优先（plan 模式），否则 `make_supervisor(settings, bridge=bridge)`；
- plan 模式把同一个 `bridge` 传进每一步的 `Runner`；
- 测试可传临时目录桥；没传且 mode 需要桥时，在 `run()` 开头抛装配错误（不要静默退回 HTTP）。

**不要让 Runner 自己写 status**：status 归 CLI/外层；Runner 只 `ask`。

### 5.4 新增 `src/ui_agent/decide/agent_engine.py`

```python
class AgentEngine:
    def __init__(self, settings: Settings, bridge: AgentBridge): ...
    def decide(self, state, goal, rules=None, error_context=None) -> Decision:
        questions, _ = build_questions(state, goal, rules)
        payload = {"state": to_jev_state(state), "questions": questions}
        if error_context:
            payload["previous_error"] = error_context
        data, meta = self.bridge.ask("decide", payload)
        if isinstance(data.get("error"), str) and data["error"].strip():
            raise AbortDecision(data["error"].strip())
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise ValueError("agent 未返回 answers 对象；未执行任何动作。")
        return assemble_decision(state, questions, answers,
                                 model="agent", usage=meta.get("usage", {}),
                                 latency_ms=int(meta.get("latency_ms", 0)))
```

要点：
- **不做 HTTP、不看 `TEXT_MODEL_API_KEY`**。
- **不做 Phase 2**：runner 现有 Phase 1 已经会重问 2 次并带 `previous_error`；两次都失败后直接按 5.7 的 `decide_invalid` 走，不再构造 `LlmEngine`。
- `{"error": "..."}` → `AbortDecision`；**runner Phase 1 必须捕获它并立即 `aborted` 返回**（否则会被当成可重试异常）。这是对共享 runner 的唯一行为补丁，HTTP 引擎在 Phase 1 不会抛它，不影响现状。
- 答复坏 JSON / 缺 answers → `ValueError`（runner 自动带 previous_error 重问一次）。`AgentBridge` 把不可解析的答复抛成 `BridgeUnavailable`，`AgentEngine` 要把它转成 `ValueError`，别让它当硬错直接终止。
- 桥超时 → `BridgeTimeout`（`RuntimeError` 子类）→ 硬停；错误文案必须带 `bridge timeout`。
- 不可解析的 ans 建议短暂重试（例如 2 个 poll 周期）后再判 `BridgeUnavailable`，防止宿主手写文件时读到半截 JSON；`bridge answer` 本身是原子写，不会产生半截。

### 5.5 `src/ui_agent/decide/engine.py`

```python
ENGINE_KINDS = ("jev", "llm", "agent", "off")

def make_engine(settings: Settings, bridge: AgentBridge | None = None) -> Decider:
    if settings.ui_agent_decider == "llm":   return LlmEngine(settings)
    if settings.ui_agent_decider == "agent":
        if bridge is None:
            raise RuntimeError("decider=agent 需要 Agent 桥，但未装配 bridge。")
        return AgentEngine(settings, bridge)
    if settings.ui_agent_decider == "off":   return OffEngine(settings)
    return JevEngine(settings)
```

更新 `tests/test_engine_switch.py` 覆盖 `agent` 分支与「缺桥报错」。

### 5.6 `src/ui_agent/llm/supervisor.py`

新增 `AgentSupervisor`（与 `Supervisor` 同接口，便于 `escalate` 鸭子类型）：

```python
class AgentSupervisor:
    def __init__(self, settings, bridge):
        self.s = settings
        self.bridge = bridge
        self.budget = SupervisorBudget(settings.supervisor_per_step, settings.supervisor_per_run)

    @property
    def available(self) -> bool: return True   # 桥在启动时已校验可写

    def new_step(self): self.budget.new_step()

    def ask(self, state, goal, trigger, detail, remaining=None) -> tuple[Ruling, dict]:
        if self.budget.exhausted:
            raise RuntimeError(f"监督预算已用完（{self.budget.detail}）；不再咨询监督模型。")
        self.budget.take()
        data, meta = self.bridge.ask("supervise", supervision_context(state, goal, trigger, detail, remaining))
        return parse_ruling(data, state), meta
```

工厂：

```python
def make_supervisor(settings: Settings, bridge: AgentBridge | None = None):
    if not settings.ui_agent_supervisor:
        return None
    if settings.ui_agent_supervisor_mode == "agent":
        if bridge is None:
            raise RuntimeError("supervisor=agent 需要 Agent 桥，但未装配 bridge。")
        return AgentSupervisor(settings, bridge)
    return Supervisor(settings)
```

- `Supervisor.available` 仍是「有 `TEXT_MODEL_API_KEY`」；`AgentSupervisor.available` 恒 True。
- `run/supervise.py` 的 `escalate` 已捕获 `RuntimeError` → `unavailable`，桥超时自然走 M0 停机，无需改语义；只需把类型标注从 `Supervisor | None` 放宽为协议/`Any`。
- `Verdict.trace()` 增加 `via`：模型模式 `"http"`，agent 模式 `"agent"`（从 `meta.get("via")` 取，默认 http）。

### 5.7 `src/ui_agent/run/runner.py` 接线

按行号锚点（副本当前）：

| 位置 | 改动 |
|---|---|
| `__init__` L76-114 | 新增 `bridge` 参数；`make_engine(settings, bridge=bridge)`；supervisor 用 `make_supervisor(settings, bridge=bridge)`；保存 `self.bridge` |
| `run()` L117-129 | 若 mode 需要桥却没有 bridge，尽早抛装配错误；不要在这里写 status |
| Phase 1 L221-239 | 增加 `except AbortDecision` → 立即返回 `RunResult("aborted", ...)`；两次失败后如果是 `AgentEngine`，**跳过 Phase 2** |
| Phase 2 入口 L240-247 | `AgentEngine` 不进 `LlmEngine` 分支；直接走 `decide_invalid` 升级（有 supervisor）或无监督硬错 |
| `_decider_model()` L357-363 | `agent` → `"agent"` |
| `_prepare()` L476-503 | `resolve_text(..., bridge=self.bridge)` |
| `_execute()` L407-409 | 录制条件：`self.text_meta.get("source") in ("text_model", "agent")`（或 `!= "var"`） |
| `_trace()` L535-566 | 决策行增加 `via`（cache/agent/http，按实际情况）；supervisor 行已由 `Verdict.trace()` 带 `via`；`text_model` 键名保留，内容含 `source=agent` |
| `_start_row()` L570-588 | 增加 `supervisor_mode`、`bridge_dir`（仅路径）、`bridge_timeout_s`；`decider` 可报 `agent` |
| `_write_result()` L625-647 | 与 run_start 同步加 `supervisor_mode`（可选加 bridge 信息），保持旧字段名不变 |

`decide_invalid` 分支的行为（agent 引擎两次 ValueError 后）：
- 有 supervisor（agent 或 model）→ `self._escalate(..., "decide_invalid", "agent 两次决策均不合规；末次：...")`，与现有 Phase 2 失败路径一致；
- 无 supervisor → `raise RuntimeError(...)`（退出码 2）。
- 不创建 HTTP `LlmEngine`，不发任何文本模型请求。

### 5.8 `src/ui_agent/run/plan_runner.py` 接线

| 位置 | 改动 |
|---|---|
| `__init__` L94-118 | 新增 `bridge` 参数；`make_engine(settings, bridge=bridge)`；`make_supervisor(settings, bridge=bridge)`；保存 `self.bridge` |
| `_step()` L242-252 | 把 `bridge=self.bridge` 传给每步 `Runner` |
| `_run_start_row()` L268-286 | 与目标模式 run_start 同样加 `supervisor_mode` / `bridge_dir` / `bridge_timeout_s` |
| `_write_summary()` L330-356 | 可选加 `supervisor_mode`；不要改旧字段名 |

- `PlanRunner` 全计划共用一个 `AgentSupervisor`（现有代码已保证 `new_step()` 调用与预算共享），不要每步新建。
- 全计划共用一个 `AgentBridge`；ask id 连续。

### 5.9 `src/ui_agent/llm/textfill.py`

签名扩为：

```python
def resolve_text(settings, goal, element, state, vars=None, bridge=None) -> tuple[str, dict]:
```

顺序：
1. `match_var` 命中 → 原样返回 `source="var"`（值不进桥、不进模型、不落盘）。
2. 没有命中：
   - `element.secret` 为真 → **桥路径直接拒绝**：`RuntimeError("敏感字段只能通过 --var 提供；未向 agent 询问。")`。M-A 不改 HTTP 路径的现状（保持内核原行为），只在 agent 分支加这条。
   - `not settings.ui_agent_supervisor` → 保持现有「大模型总闸已关」错误。
   - `settings.ui_agent_supervisor_mode == "agent"` → `bridge.ask("text", {**field_context(...), "hint": _unmatched_vars_hint(...)})`；答复必须是 `{"text": str}`，非空、≤2000、无多余键；`meta = {"source": "agent", "model": "agent", "via": "agent", "ask_id": ..., "latency_ms": ...}`。
   - 否则 → 现有 `chat_json(TEXT_VALUE, ...)`。
3. 桥超时/坏答复 → `RuntimeError`；`Runner._prepare` 会把它变成该动作的失败说明（字段拒填），不会伪造值。

### 5.10 `src/ui_agent/cli.py`

#### 参数
- `--decider` choices 加 `agent`，help 写「agent=宿主对话大模型经文件桥作答」。
- `--supervisor` choices 加 `agent`，help 写「agent=宿主对话大模型经文件桥监督」。
- `--run-dir` 在 agent 模式下必填（`cmd_run` 里按生效 settings 校验，覆盖 env 配置的情况）。
- 可选加 `--bridge-timeout`（覆盖 `UI_AGENT_BRIDGE_TIMEOUT_S`）；不加也可，M-C 用 env 校准。

#### `bridge` 子命令

```bash
ui-agent bridge wait --run-dir DIR [--timeout 240] [--json]
ui-agent bridge answer --run-dir DIR --id 7 (--file ans.json | --data '{"answer": {...}}') [--note ...] [--json]
```

- `wait`：解析 `--run-dir`，桥目录按 settings 的 `ui_agent_bridge_dir` 或 `run_dir/bridge`；目录不存在 → 退出 2；`--timeout` 默认 240s；JSON 契约见 5.1。
- `answer`：`--file` 与 `--data` 互斥且必填；读到的内容必须是对象，且包含 `answer` 对象（可带 `answered_by` / `note`）；写前做同样校验；成功/ stale 都退出 0；参数/格式错误退出 2。
- `--json` 时 stdout 只留最后一份 JSON；人类话术走 stderr（沿用现有 P0 契约）。
- `bridge` 子命令不需要 `Settings` 中的模型 key；也不启动浏览器。

#### 现有命令
- `plan` / `case draft` / `case compile` / `run --decider llm` / `--supervisor on` **完全不动**。
- `run --json` 的字段保持不变；新增字段只能加不改。

### 5.11 trace 与文档

- `run/trace.py` 模块 docstring 补 `via` 口径（不改 `ROW_KINDS`）。
- `AGENTS.md`：加「Agent 桥 / skill」硬线——桥答复是模型输出；secret 只 `--var`；截图不进模型；agent 模式 `--run-dir` 必填；不 commit。
- `README.md`：加开关表、四档映射、bridge 命令示例、桥目录产物说明。
- 这些文档改动也只在副本做。

### 5.12 回放缓存兼容

- `CACHE_POLICY_VERSION` **保持 `v3`**，不要因为桥升级；录像记录的是动作，与决策者无关。
- `AgentEngine` 产出的决策 `model="agent"`，照常录制。
- TYPE_TEXT 的 agent 值必须录制（见 5.7），否则零模型回放缺值。
- `--var` 值仍不落盘：`record_from` 只接收 `text`，runner 只传 `source != "var"` 的值。
- 回放命中时不问桥；`--decider off --supervisor off` 完全不要求桥、不要求 `--run-dir`。

---

## 6. 测试与验收

### 6.1 新增/修改单测

| 文件 | 覆盖 |
|---|---|
| `tests/test_bridge_protocol.py`（新） | 原子写、文件名、JSON 坏、附件默认空、status 状态 |
| `tests/test_bridge_client.py`（新） | ask 立即答 / 超时 / 坏答复 / id 不匹配 / 晚到 stale / 重复答复幂等 / wait 的 ask-heartbeat-finished-crashed / 双 wait 无害 / 缺 status 按 running |
| `tests/test_agent_engine.py`（新） | 合法 answers → Decision（model=agent）；缺 answers → ValueError；`{"error"}` → AbortDecision；超时 → BridgeTimeout；不碰 HTTP |
| `tests/test_agent_supervisor.py`（新） | 合法裁决过 `parse_ruling`；坏裁决 → RuntimeError；预算扣减；`new_step`；`available` 恒 True |
| `tests/test_engine_switch.py` | `make_engine` 的 agent 分支；缺桥报错 |
| `tests/test_llm_client.py` | agent 模式 textfill：var 优先、桥答复、坏答复、超时、secret 拒绝、HTTP 路径不变 |
| `tests/test_runner.py` | agent 决策 + agent 监督全链路（FakeDriver）；`decide_invalid` 升级；无 Phase 2（monkeypatch `chat_json` 为 boom）；AbortDecision 立即 aborted；text source=agent 进录像；trace `via` |
| `tests/test_plan_runner.py` | 共享桥与监督预算；两步 ask id 连续；每步 `new_step` |
| `tests/test_cli.py` | `bridge wait/answer` JSON 与退出码；agent 模式缺 `--run-dir` → 2；stale；`--json` 只输出一份 JSON |
| `tests/fakes.py` | 新增 `FakeBridge`（队列式脚本答复，记录 asks）或 `ScriptedAgent`；保持现有 `ScriptedSupervisor` 不变 |

关键负例（必须逐条进单测）：
- agent 不答 → supervisor unavailable → 与「没有监督模型」完全一致的停机结果；
- agent 答歪 → decide 重问一次带 `previous_error`；仍歪 → `decide_invalid` 升级；
- text 答歪/超时 → 该字段拒填，动作失败，不产生伪造值；
- `ans` 晚于内核推进 → `bridge answer` 返回 stale，内核已按超时处理；
- 内核崩溃/被杀 → status=crashed，`bridge wait` 返回 finished(crashed)；
- 桥目录不可写 → 启动即报错，不静默降级；
- secret 字段 → 不产生 text ask。

### 6.2 `scripts/smoke_bridge.py`（新）

结构参照 `scripts/smoke_replay.py`：
- 本地 HTTP 服务 + `examples/local_fixture.html`（真实 Chromium）。
- 临时 run dir；后台启动真 CLI：
  ```bash
  run --goal ... --url ... --check ... --task bridge-smoke \
      --decider agent --supervisor agent --run-dir <tmp>/run \
      --mode ci --json
  ```
- 假 agent 循环（同一脚本内的线程/子进程）：
  - `bridge wait --run-dir ... --timeout 1 --json`；
  - `decide` → 按脚本答 CLICK / TYPE_TEXT / DONE；
  - `text` → 答一个固定值；
  - `supervise` → 人为制造一次阻断（或脚本化 BLOCKED），答 HINT 或 RECOVER；
  - 直到 `finished`。
- 断言：
  - 退出码 0、stdout 纯 JSON、`ok=true`；
  - `bridge/status.json` 终态 `finished`；
  - trace 中决策/监督行 `via=agent`，`model="agent"`；
  - `text_model` 行 `source=agent`；
  - 再跑一次 `--decider off --supervisor off` 零模型回放全绿，且不产生任何 bridge ask；
  - 全程无外网/无付费 API（清空 key，把 base URL 指向不可达本地端口作为保险）。
- 可选负例：`--bridge-timeout 2` 且无人应答 → 监督 unavailable → M0 停机，status 正常收尾。

### 6.3 DoD 清单

- [ ] `uv run ruff check .` 全绿
- [ ] `uv run pytest -q` 全绿（359 + 新增）
- [ ] 九套既有冒烟全绿
- [ ] `scripts/smoke_bridge.py` 全绿（含零模型复跑）
- [ ] HTTP 双轨回归：`smoke_cli.py` / `smoke_supervisor.py` / `smoke_plan.py` 与改动前一致
- [ ] `--decider agent/jev/llm/off`、`--supervisor on/off/agent` 组合的 CLI 校验正确
- [ ] trace/result/summary 旧字段零改名；新增字段为加法
- [ ] `CACHE_POLICY_VERSION` 未变
- [ ] 文档更新（README / AGENTS / spec）
- [ ] 未 commit / 未 push / 未合并 / 未同步平台

---

## 7. 合并回源仓库与平台同步（用户确认后才做）

### 7.1 先证明「不影响平台」

1. 副本内回归全绿（第 6.3）。
2. 生成差异并人工 review：
   ```bash
   diff -ruN \
     -x .git -x .venv -x .browsers -x .artifacts -x __pycache__ \
     -x .pytest_cache -x .ruff_cache -x .mypy_cache -x .env \
     "D:/test_skills/ui_agent" "D:/test_skills/ui_agent_skill_dev/kernel" > bridge.patch
   ```
   确认只有预期的文件变化；runtime 目录不进入差异。
3. 向用户报告：改动文件清单、测试证据、HTTP 双轨回归结果、平台契约（argv + 产物）未变。

### 7.2 合并回源仓库（需用户明确同意）

推荐**按文件清单复制**，不要整目录 robocopy：

```bash
# 示例，按实际改动清单执行
cp "D:/test_skills/ui_agent_skill_dev/kernel/src/ui_agent/..." "D:/test_skills/ui_agent/src/ui_agent/..."
```

复制后在源仓库跑：
```bash
cd /d/test_skills/ui_agent
uv run ruff check . && uv run pytest -q
# 有浏览器时再跑九套冒烟
```

源仓库当前工作树已有 `M spec-agent-skill.md`；合并前先 `git status` 看清，不要把无关改动卷进来。commit 只在用户明确要求时做；push 同理。

### 7.3 同步到平台副本（合并后、部署前）

```bash
cd "/d/test_skills/ui尝试/ui_backend"
uv run python scripts/sync_kernel.py
```

- 脚本从 `D:\test_skills\ui_agent` 单向同步到 `ui_backend/kernel`，写 `kernel/KERNEL_SOURCE.md`。
- 平台副本当前落后（`822f0fd`，95 文件）；同步会带上源仓库 HEAD + 未提交改动，先确认源仓库没有无关脏改动。
- 平台副本里的 `.env` / `.browsers` 等运行态不会被删（同步脚本排除项保留）。
- 同步后跑后端单测/冒烟，并重启 dev 后端（平台后端没有 `--reload`），核验 `CACHE_POLICY_VERSION` 仍为 v3。
- 不要手工编辑 `ui_backend/kernel`。

---

## 8. 已知坑与硬约束（逐条遵守）

### 8.1 工程坑

- **`--run-dir` 必填**：agent 模式没有它，宿主找不到桥；不要等 CLI 结束才从 JSON 拿 run_dir。
- **桥不能每步新建**：plan 模式共享 bridge + supervisor 预算；ask id 连续。
- **status 归 CLI**：Runner/PlanRunner 不写终态；否则测试直调会污染产物。
- **status 缺失按 running**：`bridge wait` 启动竞态；不要误报 finished。
- **轮询要可注入时钟**：单测不真睡；默认 250ms，deadline 600s。
- **原子写**：ask/ans/status 一律 tmp + `os.replace`；避免半截 JSON。
- **Windows UTF-8**：所有文件读写 `encoding="utf-8"`；子进程设 `PYTHONUTF8=1`。
- **副本无 `.git`**：不要在里面 git 操作（会落到外层 `D:\test_skills`）。
- **浏览器复用**：`PLAYWRIGHT_BROWSERS_PATH=D:/test_skills/ui_agent/.browsers`。
- **无 `.env`**：key 用环境变量；测试不调付费 API。

### 8.2 安全与模型边界（原仓库 AGENTS.md 硬线，全部继续有效）

- 模型/桥输出**永不**变成 selector、坐标、可执行 JS；元素只能用当次快照的 index。
- `DONE` 不是成功证据；没配断言只能算 `done_unverified`。
- 截图只进报告，**永不进任何模型输入**；v1 监督 only text，`attachments` 恒空。
- `--var` 值、上传文件路径不落盘、不进模型；密码等敏感字段只 `--var`。
- TYPE_TEXT 值只来自 `--var`、文本模型或（新）桥；必须过严格校验，坏值不输入。
- UPLOAD 路径只来自 `--var`，无模型兜底。
- 监督裁决只能说 HINT/RECOVER/REPLAN/ABORT；RECOVER 走普通执行路径，不旁路守卫。
- 监督预算调用前扣；升级可用性不足 → 与「没有监督模型」完全一致的 M0 停机。
- 密钥只进环境变量；不写 skill/用例/产物。

### 8.3 设计边界

- **不改** `observe/` `act/` `verify/` `replay/` `case/` `report/` `driver/`。
- **不改** HTTP 四档语义；`--supervisor on` 仍走 HTTP，`--decider llm` 仍走 HTTP。
- **不改** `CACHE_POLICY_VERSION`；旧录像继续有效。
- **不加** MCP、截图监督、工具环、批量回归。
- **不做** 自动回退（无 Jev key 不自动切 agent；由 skill 文档告诉 agent 默认选 agent）。
- secret 的 HTTP textfill 现状没有硬拒绝；M-A 只在桥路径加拒绝，统一收紧留待后续拍板。

---

## 9. 精确锚点表（副本当前行号）

| 文件 | 位置 | 内容 |
|---|---|---|
| `src/ui_agent/config.py` | L37-44 | decider / supervisor 开关 |
| `src/ui_agent/config.py` | L58-93 | headless / runs_dir / cache_dir 等属性 |
| `src/ui_agent/decide/engine.py` | L13 | `ENGINE_KINDS` |
| `src/ui_agent/decide/engine.py` | L32-37 | `make_engine` |
| `src/ui_agent/decide/delegate.py` | L25-72 | `LlmEngine` / Phase 1/2 模板 |
| `src/ui_agent/decide/jev.py` | L46 | `validate_choice` |
| `src/ui_agent/decide/jev.py` | L191 | `build_questions` |
| `src/ui_agent/decide/jev.py` | L219 | `assemble_decision` |
| `src/ui_agent/decide/jev.py` | L256 | `JevEngine` |
| `src/ui_agent/llm/supervisor.py` | L17-24 | `supervision_context` |
| `src/ui_agent/llm/supervisor.py` | L27-49 | `Supervisor` |
| `src/ui_agent/run/supervise.py` | L49-61 | `escalate`（RuntimeError → unavailable） |
| `src/ui_agent/run/supervise.py` | L64-75 | `recover_decision` |
| `src/ui_agent/run/runner.py` | L76-114 | Runner 装配 |
| `src/ui_agent/run/runner.py` | L117-158 | `run()` 生命周期 |
| `src/ui_agent/run/runner.py` | L217-276 | 两阶段决策循环 |
| `src/ui_agent/run/runner.py` | L357-363 | `_decider_model` |
| `src/ui_agent/run/runner.py` | L365-399 | `_escalate` |
| `src/ui_agent/run/runner.py` | L401-432 | `_execute` / 录制条件 |
| `src/ui_agent/run/runner.py` | L476-503 | `_prepare` / `resolve_text` |
| `src/ui_agent/run/runner.py` | L535-566 | `_trace` |
| `src/ui_agent/run/runner.py` | L570-588 | `run_start` 行 |
| `src/ui_agent/run/plan_runner.py` | L94-118 | PlanRunner 装配 |
| `src/ui_agent/run/plan_runner.py` | L242-252 | 每步 Runner 构造 |
| `src/ui_agent/run/plan_runner.py` | L268-286 | 计划 run_start 行 |
| `src/ui_agent/cli.py` | L54-74 | `apply_overrides` |
| `src/ui_agent/cli.py` | L168-224 | `cmd_run` / `_run_goal` / `_run_plan` |
| `src/ui_agent/cli.py` | L286-378 | 参数与 `main` |
| `src/ui_agent/llm/textfill.py` | L20-29 | `match_var` |
| `src/ui_agent/llm/textfill.py` | L40-57 | `resolve_text` |
| `src/ui_agent/replay/session.py` | L32-63 | `record_from`（text 语义） |
| `src/ui_agent/replay/session.py` | L202-265 | stage / settle（录制闸门） |
| `src/ui_agent/replay/cache.py` | L35 | `CACHE_POLICY_VERSION = "v3"` |
| `src/ui_agent/replay/cache.py` | L74 | 缓存 key |
| `src/ui_agent/run/trace.py` | L34-39 | trace 词表 |
| `tests/fakes.py` | L114-153 | FakeEngine / ScriptedSupervisor 模板 |
| `tests/fakes.py` | L156-171 | `settings_for` |
| `scripts/smoke_replay.py` | 全文 | 冒烟结构模板（本地服务 + 真 CLI + 真 Chromium） |

> 行号随改动会漂移；以函数名/锚点文本为准。

---

## 10. 协议与命令速查

### 10.1 桥文件示例

`<run-dir>/bridge/ask-0007.json`：
```json
{
  "id": 7,
  "kind": "supervise",
  "at": "2026-10-19T14:03:22.123",
  "deadline_s": 600.0,
  "payload": {
    "stuck": {"trigger": "blocked", "detail": "决策模型判定无法推进"},
    "current_goal": "查询并核对结果",
    "remaining_goals": [],
    "state": {"page": {}, "elements": []},
    "attachments": []
  }
}
```

`<run-dir>/bridge/ans-0007.json`：
```json
{
  "id": 7,
  "answer": {"ruling": "HINT", "hint": "先展开高级查询再选行政区"},
  "answered_by": "agent",
  "note": "从 trace 第 3 行看到筛选面板未展开"
}
```

`<run-dir>/bridge/status.json`：
```json
{"state": "running", "seq": 7, "detail": "", "updated_at": "2026-10-19T14:03:22.123"}
```

### 10.2 宿主 agent 的典型循环

```bash
# 后台启动（必须显式 run-dir）
uv run ui-agent run --plan-file plan.json --decider agent --supervisor agent \
  --run-dir .artifacts/runs/my-run --json &

# 循环等事件
uv run ui-agent bridge wait --run-dir .artifacts/runs/my-run --timeout 240 --json
# → ask：读 payload → 作答
uv run ui-agent bridge answer --run-dir .artifacts/runs/my-run --id 7 \
  --data '{"answer": {"ruling": "HINT", "hint": "..."}}' --json
# → finished：读 result.json / report.html 汇报
```

### 10.3 四档命令

```bash
# 全关（零模型回放）
uv run ui-agent run --plan-file plan.json --decider off --supervisor off --json

# 只开 Jev
uv run ui-agent run --plan-file plan.json --decider jev --supervisor off --json

# 只开大模型（无 key）
uv run ui-agent run --plan-file plan.json --decider agent --supervisor agent \
  --run-dir .artifacts/runs/x --json

# 全开（Jev + 对话大模型监督；主目标）
uv run ui-agent run --plan-file plan.json --decider jev --supervisor agent \
  --run-dir .artifacts/runs/x --json
```

---

## 11. 交接完成后的报告模板

落码 agent 完成后，向用户报告（不要贴长 diff）：

1. **改了什么**：新增/修改文件清单（按 5.x 分组）。
2. **测试证据**：ruff、pytest 数字、九套冒烟、`smoke_bridge.py` 的关键断言输出。
3. **契约确认**：HTTP 双轨未变；trace/result 旧字段未改名；`CACHE_POLICY_VERSION=v3`。
4. **已知风险/未决**：bridge 超时值、secret HTTP 路径未统一、MCP 后置。
5. **下一步**：等用户确认后合并（第 7 节）；不要自行 commit/push/同步。
