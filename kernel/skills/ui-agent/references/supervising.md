# supervising：卡住时给人一次干预机会

`--supervisor agent` 时，内核在**卡住**那一刻写一个 `supervise` ask，等你给一个裁决。
不卡就不问。触发信号是确定性的（重复动作、无进展、决策置信度低、决策不合规），不是模型猜的。

## ask 的形状

```json
{"kind": "supervise", "id": 12, "deadline_s": 180.0, "payload": {
  "stuck": {"trigger": "repeat_no_progress", "detail": "TYPE_TEXT → 13 连续 3 次决策相同"},
  "current_goal": "在搜索框输入大肥鱼并提交",
  "remaining_goals": ["点击下一页", "确认停在第二页"],
  "state": {"page": {...}, "elements": [...], "recent_actions": [...]}}}
```

`trigger` 的全部取值（源码里就这几个，没有别的）：

| trigger | 什么时候 | 说明 |
|---|---|---|
| `repeat_no_progress` | 同一决策连续 N 次，且期间页面没变 | `detail` 里带此前动作的页面变化序列 `[true, false, false]`（旧→新），是判断「没到目标页」还是「到了但没新数据」的关键证据 |
| `low_confidence` | 连续 N 次决策置信度 < `--conf-min`（默认 0.55） | 通常意味着页面上没有能推进的控件，决策方在瞎猜 |
| `decide_invalid` | 决策答复两次不合规 | `detail` 是末次校验错；agent 模式下到这儿就直接升级，不会再问第三次 |
| `check_failed` | 这一步的断言没过 | 先看 `detail` 里的证据片段，可能只是断言写错 |
| `blocked` | 决策方主动选了 `BLOCKED` | 它认为没有任何受支持的操作能推进 |
| `budget_decisions` / `budget_actions` | 决策 / 动作预算耗尽 | `max_decisions` / `max_actions` |

`current_goal` 是**单数**——只救当前这一步，不要替整个计划重新规划。

## 四种裁决，从轻到重

### HINT —— 给一句缺失的操作规则

```json
{"answer": {"ruling": "HINT", "hint": "搜索框在 [17]；输入后必须点联想词才提交，不要按回车。", "reason": "连续对同一控件输入"}}
```

- `hint` 非空，超 600 字符会被截断。
- 给的是**可执行的操作规则**（看什么状态、优先哪个控件、哪一步在空转），不是「再试试」。
- 不消耗动作预算，最安全。**优先用它。**

### RECOVER —— 你替它做一个微动作

```json
{"answer": {"ruling": "RECOVER", "action": {"operation": "CLICK", "element": 42}, "reason": "遮罩未关"}}
```

允许的操作**只有这 8 个**：
`CLICK` / `SELECT` / `HOVER` / `SCROLL_TO` / `PRESS_KEY` / `SCROLL_UP` / `SCROLL_DOWN` / `WAIT`。

明确**不允许**：
- `CLICK_FORCE`（那是缺陷兜底路径，监督模型不能走）
- `TYPE_TEXT` / `UPLOAD`（值和文件不能由监督模型现编）

字段要求：

| 操作 | 必需字段 |
|---|---|
| `CLICK` / `HOVER` / `SCROLL_TO` | `element`（当次快照里的 index） |
| `SELECT` | `element` + `option`（该元素 `options` 里的 key） |
| `PRESS_KEY` | `key`，长度 ≤20，实际只有 `Enter` / `Escape` / `Tab` 有意义 |
| `SCROLL_UP` / `SCROLL_DOWN` / `WAIT` | 无 |

**不存在的 index、禁用的元素、不在选项表里的 option 一律拒绝，且不执行任何动作**——
所以别猜，只从 `payload.state.elements` 里挑。

### REPLAN —— 当前目标本身写错了

```json
{"answer": {"ruling": "REPLAN", "steps": [{"goal": "...", "checks": [{"kind": "text_contains", "value": "..."}]}], "reason": "原目标要求不存在的入口"}}
```

`steps` 会**替换掉剩下的全部工作**（最多 12 步）。这一步本身停在原处，运行状态记为 `replanned`。

纪律：

- 原计划里的凭据、控件标签、值**逐字保留**，不许改写、不许编造。
- 断言只能用那 8 种 kind；超范围整份裁决作废。
- 只在目标真的错了/不可达时用。路径难走不算——那是 HINT 的活。

### ABORT —— 停

```json
{"answer": {"ruling": "ABORT", "reason": "页面要求短信验证码，且没有可用凭据"}}
```

`reason` 必填。运行状态记 `aborted`，不再问。

## 三条硬限制

1. **预算**：每步 2 次、每次运行 6 次（`SUPERVISOR_PER_STEP` / `SUPERVISOR_PER_RUN` 可调）。
   **预算在调用前就扣**——答歪了也不退。用完直接判 `stuck`，不再发问。
2. **只见文本**：`payload.attachments` 在 v1 恒为空数组，截图不进任何模型输入。你判断的依据只有
   元素表、页面文本、最近动作。
3. **不越权**：你的裁决和决策模型一样，只能落到当次快照的 index 上。不能给 selector、坐标、JS。

## 裁决之后

- `HINT`：塞进决策模型下一轮的 `rules`；若仍无进展，下一轮还会再问（预算允许的话）。
- `RECOVER`：内核直接执行你给的动作（`model="supervisor"`），然后继续正常循环。
- `REPLAN`：本次运行结束，状态 `replanned`，`summary.json` 里带新步骤序列。
- `ABORT`：本次运行结束，状态 `aborted`。

## 常见的错法

| 症状 | 原因 | 改法 |
|---|---|---|
| `恢复动作指向的元素 99 不在当次快照里` | 用了历史快照的 index | 只读本次 `payload.state.elements` |
| `恢复动作只能在这些操作里选：...（收到 TYPE_TEXT）` | 想帮它填值 | 值只能走 `--var`；改发 HINT 让它自己选 TYPE_TEXT |
| `元素 [i] x 不支持 CLICK（可能已禁用）` | 挑了 disabled / occluded 的元素 | 换一个，或先 HINT 让它处理遮挡 |
| `监督预算已用完（本步 2/2，本次运行 6/6）` | 一直在救同一步 | 该 REPLAN 或 ABORT 了 |
| `监督模型没有返回 JSON 对象。` | 答复形状不对 | 顶层必须是 `{"answer": {"ruling": ...}}` |

## 不要做的事

- 别用 supervise 通道给文本值（那是 `text` ask 的活，且 secret 永远不经桥）。
- 别把 REPLAN 当「重试」用；它会让本次运行停下。
- 别在没有新信息时反复发同样的 HINT——预算会烧干，结果是 `stuck` 而不是 `ok`。
