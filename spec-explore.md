# Spec 增补：探索式用例生成（explore → YAML → 录制 → 零 token 回放）

- 状态：**草案 v1**（2026-10-19）
- 定位：`kernel/spec-agent-skill.md` 的增补篇，只补 M-A 之外新增的三块——
  **explore 命令**、**YAML `id` 作缓存身份**、**断言生命周期**。
  M-A 内核桥本身仍以 `HANDOFF.md` 第 5 节为准，本篇不改它。
- 用户拍板（2026-10-19）：
  1. 探索走**方案 A**（复用 Agent 桥，不另写决策引擎）；
  2. **保留 Jev**：`explore --decider jev|agent` 都能逐步决策；
  3. 本地**有头**运行，用户实时看步骤与消耗，**不设硬性上限**，思考强度跟随 agent；
  4. YAML 加**稳定 `id`** 作为缓存身份（用户选 C）；
  5. 断言由 **agent 自己先写**，后续允许用户修改；
  6. MCP 适配器**后置**。

---

## 1. 目标：四段工作流

用户给一句话目标，agent 自己探索、自己写用例，之后零 token 复跑：

```
① explore      给的只是 goal：agent 开有头浏览器，自己走，可乱走、可不录
     │          → 产物：trajectory / actions / 截图；**不写回放缓存**
     ▼
② 写 YAML      agent 读轨迹，自己写 steps[{goal, checks}]，过 case compile 0 error
     │          → 产物：<用例库>/<场景>/<name>.yaml（带稳定 id）
     ▼
③ 录制跑       run --plan-file/--case ... ：这一次既录制，又**证明这份 YAML 成立**
     │          → 只有断言通过才落盘（现有闸门，不改）
     ▼
④ 零 token     run --decider off --supervisor off ：命回放，不问任何模型
```

**②③ 之间那道「真跑一遍」不可省**：它是唯一能证明断言成立的证据。没有它，
agent 写的断言就只是模型自称，等于绕过「`DONE` 不是成功证据」这条铁律。

**① 必须不录制**：探索会点开用不到的页面（菜单、设置、绕路分支），
录进去就会在回放时重放这些垃圾动作。

---

## 2. `explore` 命令

### 2.1 形态

```bash
ui-agent explore --goal "登录后查询票据并核对结果" --url http://x/ \
  --decider agent --run-dir .artifacts/runs/explore-1 --headed --json
```

| 参数 | 说明 |
|---|---|
| `--goal` / `--goal-file` | 必填；探索目标（自然语言） |
| `--url` | 必填；入口 |
| `--decider jev\|agent` | **两种都支持**。`agent`=对话大模型经桥逐步决策；`jev`=Jev 逐步决策（要 key） |
| `--supervisor agent\|on\|off` | 与 `run` 同义；探索时建议 `agent` 或 `off` |
| `--run-dir` | `--decider agent` 或 `--supervisor agent` 时**必填**（同 M-A 约束，桥要在 `<run-dir>/bridge`） |
| `--headed` | 有头（本地默认建议有头；`UI_AGENT_HEADLESS=false` 亦等效） |
| `--var` | 与 `run` 同义 |
| `--max-actions` / `--max-decisions` | 沿用现有预算开关，**不新增硬顶**；要给大值由用户显式传 |
| `--json` | stdout 只留最后一份 JSON（沿用 P0 契约） |

**不设 step/time 硬上限**（用户拍板）。终止靠现有三件事兜底：
决策/动作预算（可配大值）、卡住检测（`StuckDetector`）、`DONE`/`BLOCKED`。
三者任一触发即收尾，保证不会无限跑。

### 2.2 实现口径：复用 `Runner`，不新写引擎

探索 = **一次没有断言的单目标运行**。因此：

```python
runner = Runner(settings, goal, url, checks=[], vars=..., run_dir=...,
                cache=None,          # ← 关键：不录制，也不读回放
                json_mode=..., verbose=...)
result = runner.run()
```

- `checks=[]` ⇒ 模型给 `DONE` 时走现有 `done_unverified` 分支——
  这正好是探索的语义（「它说探索完了，但没有断言能证明」），**不需要改 runner 的 DONE 分支**。
- `cache=None` ⇒ `_stage()` 直接空转（现有代码已判 `self.cache is not None`），
  **不落任何录像**，无需新增开关。
- 决策走 M-A 的 `make_engine(settings, bridge=bridge)`：
  `--decider agent` → `AgentEngine`（桥），`--decider jev` → `JevEngine`（HTTP）。
  **两者都是一行不动地复用**。
- 监督走 M-A 的 `make_supervisor`。

> 结论：`explore` 的命令层改动 ≈「组装 Runner + 导出轨迹」，**没有新决策逻辑**。
> 这是方案 A 的全部价值。

### 2.3 多轮 charter（v1 不做）

e2e 的 explore 会「规划一步 → 执行一步 → 再规划」，一圈一圈推进。
v1 **只做单一 goal 的连续循环**（即一个 `Runner.run()`）。
多轮 charter 留作后续：如有需要，在外层套一个「分 Charter → 各跑一次 Runner」的循环，
**不改 Runner 内部**。

### 2.4 产物布局

```
<run-dir>/
  trace.jsonl / actions.json / snapshots/ / screenshots/ / result.json   # 现有契约，原样
  bridge/                                                                 # agent 模式下
  explore/                                                                # 新增（只在 explore 下）
    goal.txt            本次探索目标
    trajectory.md       人/agent 可读的轨迹摘要（逐步：操作 + 目标标签 + 结果 + 页面变化）
    explored.yaml       **骨架** YAML（单 step，goal 已填，checks 留占位注释）
```

`explore/` 是**新增目录**，不动 `run --run-dir` 的既有布局，平台契约不受影响。
骨架要落到用例库里再补 `checks`：`<用例库>/<场景>/<name>.yaml`。

---

## 3. 轨迹 → YAML

### 3.1 导出什么

`trajectory.md`（给 agent 读，比读原始 `actions.json` 省 context）：

```
# 探索轨迹：登录后查询票据并核对结果
入口：http://127.0.0.1:8000/login
共 14 个动作，3 个页面

## 页面 1 http://127.0.0.1:8000/login 「登录」
 1. TYPE_TEXT 「账号」 ← 值来自 --var（不落盘）
 2. TYPE_TEXT 「密码」 ← 敏感字段（已掩码）
 3. CLICK 「登录」 → 页面变为 /dashboard

## 页面 2 /dashboard「工作台」
 4. CLICK 「票据查询」 → 页面变为 /tickets
 ...
```

约束（与 `AGENTS.md` 硬线一致）：
- **`--var` 的值不写进轨迹**，只写「值来自 --var」；
- `secret` 字段只写掩码；
- 上传只写文件名与大小，不写本地路径；
- 截图路径**可以**出现在轨迹里（它是产物，不是模型输入），但**不得**作为任何 ask 的 payload（→ 与 v1「监督只看文本」一致）。

### 3.2 `explored.yaml` 骨架

```yaml
id: tickets-query-001          # 稳定身份：缓存键的一半
title: 票据查询并核对
vars:
  账号: ${{secret.账号}}
steps:
  - goal: 登录系统
    checks: []                 # ← 必须补：agent 根据轨迹写断言
  - goal: 查询票据
    checks: []
```

`checks: []` 的骨架**故意过不了 `case compile`**（`checks_empty` 报错）。
这是有意的闸门：**agent 必须自己写断言才能让用例成立**。
`explore` 命令**不替 agent 编断言**——它只提供证据（轨迹），断言由 agent 判断。

### 3.3 agent 写断言的依据

`trajectory.md` 里已带足够素材：最终页面文本、关键元素标签、URL。
agent 可选 8 种断言（`schema/check.py: KINDS`）：
`text_contains` / `url_contains` / `title_contains` / `element_exists` /
`element_absent` / `element_value` / `element_count` / `element_attribute`。

**推荐 agent 优先用能证明业务结果的四种**（`text_contains`、`element_value`、
`element_count`、`element_attribute`），因为编译器会对「最后一步只证明控件还在」
发 `weak_final_check` 警告。

### 3.4 断言「先由 agent 写、后续允许用户改」

- agent 写完 → `case compile --in <yaml> --json` 必须 0 error；
- 用户可后改断言（改期望值、加严、放宽）；
- **改断言的后果由第 5 节定义**（不淘汰录像）。

---

## 4. YAML `id` 作缓存身份

### 4.1 现状（已核实）

`case/yaml_case.py: _build()` 里 **`id` 已经被读取并校验**（`bad_name` 检查非法字符），
但**没有带进 `TestPlan`**——被丢掉了：

```python
return TestPlan(name=title or "task", url=..., vars=vars_map, notes=..., steps=built)
#              ↑ 没有 id
```

所以这是**纯加法**：YAML 侧早已合法，只是内核没接住。

### 4.2 改动

```python
# schema/plan.py
class TestPlan(BaseModel):
    case_id: str = ""        # 新增：稳定身份；空 = 旧用例，沿用内容哈希
    name: str = "task"
    ...
```

```python
# case/yaml_case.py: _build()
return TestPlan(case_id=_text(data.get("id")), name=title or "task", ...)
```

```python
# case/yaml_case.py: plan_to_case_yaml()  ← 补回写，保证 round-trip
if plan.case_id:
    doc["id"] = plan.case_id
```

```python
# replay/cache.py
def case_identity(plan: TestPlan) -> str:
    """有 id 就用 id，没有就退回内容哈希（旧用例零变化）。"""
    return f"id:{plan.case_id}" if plan.case_id else plan_case_hash(plan)

def plan_case_hash(plan: TestPlan) -> str:
    data = plan.model_dump()
    data.pop("url", None)
    data.pop("case_id", None)      # ← 必须排除，否则所有旧用例哈希都变（等于作废全部录像）
    data["vars"] = {key: "" for key in sorted(data.get("vars") or {})}
    return _sha256_json(data)
```

调用点（`cli.py: _run_plan`、`plan_runner` 开 session 的地方）改用 `case_identity(plan)`。

### 4.3 为什么**不用**升 `CACHE_POLICY_VERSION`

| 用例 | 键 | 旧录像 |
|---|---|---|
| 无 `id`（现有全部用例、平台四档） | `plan_case_hash` → **与今天完全一致** | ✅ 继续有效 |
| 有 `id` | `id:<case_id>` | 新身份，自然 miss（这是加 id 的预期迁移成本） |

`CACHE_POLICY_VERSION` **保持 v3**。HANDOFF「旧录像继续有效」的硬约束不破；
`CacheEntry.policy` 与 `scan_other_policies` 的逻辑一律不动。

### 4.4 加 id 的收益（这是选 C 的全部理由）

- agent 可以**随便润色 goal 措辞、改 title、调 note**，录像不失效；
- 同一份用例换环境跑（`base_url` 变）仍按 `base_url` 区分（`cache_key` 里本来就有它）；
- 用户改断言不再导致键变化（配合第 5 节，录像也不淘汰）。

---

## 5. 断言生命周期：**断言失败不淘汰录像**

### 5.1 问题（C 方案引入，必须一起修）

现状：

```
回放到 DONE → 断言没过
  → cache.replay_failed()            # 标记本步「回放过但没成立」
  → status = check_failed
  → settle_step(): 失败分支 → _evict_variant()   # 录像被删
```

旧口径下，用户改断言 ⇒ 键变了 ⇒ 只是一次 miss，旧录像还在。
**C 方案下**，用户改断言 ⇒ 键不变 ⇒ 录像被找到 ⇒ 回放跑完动作 ⇒ 新断言不成立
⇒ **把一份本来正确的录像淘汰掉**，`--decider off` 下连重试都没有。

用户明确说「断言后续允许更改」，所以这条必须修。

### 5.2 改法：区分「动作失败」与「仅断言不成立」

| 情形 | 现有行为 | 新行为 |
|---|---|---|
| 动作执行失败（`action.ok == False`） | 淘汰该变体 | **不变**（录像确实不成立，防投毒） |
| 回放走到头但 `DONE` 断言没过 | 淘汰该变体 | **保留录像**，报 `check_failed`（退出码 1） |
| 缺缓存 / 变体不匹配 | miss | 不变 |

实现（在 `ReplaySession` 加一个「保留」标记，`settle_step` 读它）：

```python
def keep_on_check_failed(self) -> None:
    """回放动作成立、只是收尾断言没过：不淘汰录像（用户可能只是改了断言）。"""
    self.miss("retry")                 # 仍然记一次未命中（如实反映这次没成立）
    self._keep_video.add(self.current_step)

def settle_step(self, status: str) -> None:
    keep = step in self._keep_video
    self._keep_video.discard(step)
    ...
    # 失败分支：原来 if active is not None: if used or broke: evict
    if active is not None:
        if (used or broke) and not keep:
            self._evict_variant(step, active)
            self._evicted += 1
```

`runner.py` 的 DONE 分支改为：

```python
if outcome.ok:
    ...
if self.cache is not None and decision.model == CACHE_SOURCE:
    self.cache.keep_on_check_failed()      # ← 原 replay_failed()
```

`replay_failed()` 保留给「动作执行失败」那条路径（`_execute` 里不变）。

### 5.3 `--relearn`（可选，建议一起做）

用户想「重录」时：

```bash
ui-agent run --plan-file x.json --decider jev --relearn
```

语义：本次运行**忽略已存在录像**（等同 miss），断言通过后按新路径覆盖。
实现：`open_session(..., relearn=True)` → `entry.records = []` 之后照常录制。
不加 `--relearn` 也不会死锁：用户改断言后录像保留，跑一次 `--decider jev` 即可重新确认。

---

## 6. 可观测性（有头 + 实时消耗）

### 6.1 逐步实时输出

现有 `Runner._step_line()` 已经在每步打印 `[序号] 操作 置信 → 标签 ✓/✗ 结果`，
`_escalate` 打印 `⇄ 升级（trigger）：裁决`。
**探索模式直接继承**，无需新增格式。需要补的是**汇总**：

```
运行结束：
  动作 14 个 · 决策 15 次 · 升级 2 次
  Jev 决策 12 次（usage: 34,120 tokens）        ← agent 模式这行不打印
  桥问答 15 次（decide 13 / supervise 2 / text 0）  ← agent 模式打印这行
```

### 6.2 两种 decider 的「消耗」含义不同（必须写进 skill）

| decider | 内核能统计吗 | 口径 |
|---|---|---|
| `jev` | ✅ | `usage` 已进决策行（`Decision.usage`），可累加 |
| `agent` | ❌ | token 花在**宿主自己的循环**里，内核看不见；只能报「N 次桥问答」 |

内核不做 token 估算（那是宿主的账）。skill 里必须写清，避免用户以为内核漏统计。

### 6.3 `bridge` 子命令延迟导入（实测必改）

实测：`import ui_agent.cli` = **0.633s**，且**连带 import playwright**；
起一次 CLI 进程 = **0.78s**。

`bridge wait` 会被宿主调几百次，每次白付 0.78s 且拉进整个 playwright 是浪费。
改法：`cli.py` 把 `from ui_agent.driver.playwright_ import Driver` 等重导入
挪进各自 `cmd_*` 函数内（模块顶层只留 `argparse`/`json`/`sys`/`pathlib` 与轻量 schema）。
目标：`bridge wait` 的冷启动 ≤ 0.2s。

---

## 7. 硬约束（照旧，逐条遵守）

- **探索不录制**（`cache=None`）；录制只发生在「跑 YAML」那一次。
- **断言不替 agent 编**：`explore` 只给证据，`checks: []` 骨架过不了编译是**有意的**。
- **回放命中不问模型**；`--decider off --supervisor off` 不要求桥、不要求 `--run-dir`。
- **`CACHE_POLICY_VERSION` 保持 v3**；`plan_case_hash` 必须排除 `case_id`。
- **动作失败仍淘汰录像**（防投毒）；只有「仅断言不成立」才保留。
- 轨迹里 `--var` 值不落盘、secret 掩码、上传只留文件名/大小。
- 截图不进任何 ask payload（v1 监督只看文本；`payload.attachments` 恒空）。
- 不改 `observe/` `act/` `verify/` `case/`（除 3 处 id 透传）`report/` `driver/`。
- 不改现有 `run`/`plan`/`case draft`/`--decider llm`/`--supervisor on` 的任何行为。

---

## 8. 文件级改动清单

| 文件 | 改动 | 阶段 |
|---|---|---|
| `src/ui_agent/run/explore.py`（新） | `explore` 的组装与轨迹导出（`trajectory.md` / `explored.yaml`） | 2,3 |
| `src/ui_agent/cli.py` | 新增 `explore` 子命令；`bridge`/轻量子命令**延迟导入**；`_run_plan` 改用 `case_identity` | 2,6 |
| `src/ui_agent/schema/plan.py` | `TestPlan.case_id` 字段 | 4 |
| `src/ui_agent/case/yaml_case.py` | `_build` 透传 `id`；`plan_to_case_yaml` 回写 `id` | 4 |
| `src/ui_agent/replay/cache.py` | `case_identity()`；`plan_case_hash` 排除 `case_id` | 4 |
| `src/ui_agent/replay/session.py` | `_keep_video` 标记 + `keep_on_check_failed()`；`settle_step` 读标记 | 5 |
| `src/ui_agent/run/runner.py` | DONE 分支改调 `keep_on_check_failed()`；收尾打印用量汇总 | 5,6 |
| `src/ui_agent/run/plan_runner.py` | 开 session 改用 `case_identity(plan)` | 4 |
| `tests/test_explore.py`（新） | 骨架导出、轨迹脱敏、`checks: []` 编译失败、不写缓存 | 2,3 |
| `tests/test_case_id.py`（新） | id 透传/回写；**旧用例哈希不变**；同 id 改 goal 键不变 | 4 |
| `tests/test_cache_lifecycle.py`（新） | 仅断言失败不淘汰；动作失败仍淘汰；`--relearn` | 5 |
| `scripts/smoke_explore.py`（新） | 真 Chromium：探索→出 YAML→录制跑→零 token 回放全绿 | 2–5 |
| `README.md` / `AGENTS.md` / `SKILL.md` | 四段工作流、`id` 用法、断言改动语义 | 7 |

---

## 9. 验收（每步可独立验证）

1. **explore 不污染缓存**：跑一次 `explore`，`<cache-dir>` 文件数不变。
2. **骨架必被拒**：`case compile` 对 `explored.yaml` 返回 1，`code=checks_empty`。
3. **旧用例不受影响**：任取一条现有用例，改造前后 `plan_case_hash` 与 `cache_key` **完全相同**（单测钉死）。
4. **改 goal 不丢录像**：同 `id`、改 goal 文本 → `cache_key` 不变。
5. **改断言不删录像**：`id` 固定，先录一份，再把断言改严 → 跑 `--decider off` 得
   `check_failed`（退出码 1），**缓存文件仍在**且内容未变。
6. **动作失败仍淘汰**：构造动作失败 → 该变体被删（回归现有行为）。
7. **零 token 闭环**：③ 录制跑 ok → ④ `--decider off --supervisor off` 全命中、
   trace 零 `model` 行。
8. **`bridge wait` 冷启动 ≤ 0.2s**（延迟导入生效）。
9. 既有 359 单测 + 九套冒烟全绿。

---

## 10. 待定

| 项 | 建议 | 说明 |
|---|---|---|
| 多轮 charter（e2e 式「规划一步→执行一步」） | v1 不做 | 单 goal 循环已满足「探索出用例」；需要时在外层套，不改 Runner |
| `explore` 的默认有无头 | 本地默认**有头** | 用户要看实时步骤；CI/平台不调 `explore` |
| 桥单次 ask 超时默认值 | **180s**（`UI_AGENT_BRIDGE_TIMEOUT_S`） | 对话模型答一次通常秒级；600s 会让 agent 崩了时白等 10 分钟 |
| MCP 适配器 | **后置** | 桥是协议，MCP 是零内核改动的薄壳 |
| `--relearn` 是否本期做 | 建议做 | 与「断言可改」配套，工作量小 |
