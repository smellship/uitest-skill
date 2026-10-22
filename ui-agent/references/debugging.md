# debugging：跑挂了看什么

先读 `result.json`（单目标）或 `summary.json`（计划）的 `status` + `detail`。
`status` 只有 11 个取值，每个对应一种确定的停法——不要靠猜。

## 排障的顺序：先回放定位，再录制验证

**别一上来就重录**（那是付费动作）。先用免费的手段把问题框小：

```
① 零 token 回放一次（--decider off --supervisor off）
   → 挂在哪一步？summary.json 里第一个非 ok 的 step，读 steps/NN-*/trace.jsonl
② 拿这个结论去录制（--decider jev --relearn）
   → Jev 判断对了、录下正确录像 ⇒ 是录像过期，用例本身没问题，收工
   → Jev 也卡住（stuck / blocked / low_confidence）⇒ 监督层来找你，由你兜底
```

② 里 Jev 表现得比第一次差是正常现象——同样的页面状态它可能选不同的路。
**判据是「它有没有把这条流程走对并录下来」，不是「它和我预期的动作序列一模一样」。**

你是最后一层：`kind=supervise` 的 ask 到了，按 [supervising.md](supervising.md) 选 HINT/RECOVER/REPLAN/ABORT。
经验分界——模型**知道该干什么但选错了控件** → HINT；**根本不知道下一步是什么** → RECOVER。

## 停机状态 → 该做什么

| status | 含义 | 先看 | 处置 |
|---|---|---|---|
| `ok` | 全部断言通过 | — | 收工 |
| `done_unverified` | 模型报 DONE，但这一步没有断言 | `summary.json` 该步 `checks: []` | **补断言**，不要当成功上报 |
| `check_failed` | 断言没过 | `trace.jsonl` 的 `assert` 行（含每条的 `evidence`） | 分三种：断言写错 / 真缺陷 / 断言太严（详见下）|
| `blocked` | 决策模型说推不动 | 最后一条 `decision` 行的 `operation: BLOCKED` | 看 `recent_actions`；常是权限、数据缺失、验证码 |
| `stuck` | 升级信号触发且监督没救回来 | `decision` 行的 `signal` 与 `supervisor` | `repeat_no_progress` 改 goal 或 `--var`；`low_confidence` 加监督 |
| `budget` | 动作/决策/监督预算耗尽 | `detail` 里写着是哪个预算 | 拆小目标；或调 `--max-actions` |
| `aborted` | 监督模型判 ABORT，或 agent 自己 abort | `detail` / `reason` | 这页面真做不到，别重试 |
| `replanned` | 监督给了新步骤序列，本次停在原处 | `result.json` 的 `replan` 数组 | 收下新计划再跑一次 |
| `cache_missed` | 零模型模式下录像对不上 | `cache` 行的 `miss_reasons` | **直接走录制流程 `--relearn` 重录**，再跑一次零 token 回放确认（不要手工修录像） |
| `skipped` | 前序步骤没过，这步没跑 | 上一层的 `status` | 先修前一步 |
| `error` | 运行时异常 | `detail`（异常类名 + 消息） | 看是不是环境/参数问题 |

## 产物地图

单目标（`run --goal`）：

```
<run-dir>/
  trace.jsonl        每行一个事件，主证据
  actions.json       实际执行过的动作（含 mask 后的 text）
  result.json        最终结果 + 断言 + 缺陷候选
  report.html        从上面渲染的单页报告
  snapshots/000.json 每次决策前的 state 快照
  screenshots/       debug 模式逐步；ci 模式只留 failure.png
```

计划（`run --plan-file`）：

```
<run-dir>/
  trace.jsonl        根 trace：run_start / step_start / step_end / cache / run_end
  plan.json          实际执行的计划（含 case_id）
  summary.json       逐步结果 + 汇总
  actions.json       全计划动作汇总
  steps/01-<标题>/
    trace.jsonl      这一步的完整 trace（decision / assert / model 都在这里）
    result.json      这一步的结果
    actions.json     这一步的动作
    report.html      这一步的报告
    snapshots/
    screenshots/
```

**记住分层**：根 trace 只有边界的聚合行，真正的 `decision` 行在 `steps/*/trace.jsonl` 里。

## trace 行类型

`ROW_KINDS = ("run_start", "run_end", "step_start", "step_end", "decision", "assert", "model", "cache")`

| kind | 有什么 | 什么时候看 |
|---|---|---|
| `run_start` | 运行身份 + 配置快照（`decider` / `supervisor_mode` / `bridge_dir` / `bridge_timeout_s` / `cache` / `vars` 键名） | 怀疑「跑的不是我以为的档」|
| `decision` | 一次决策往返 + 落地动作（**默认行**） | 90% 的排障都在这 |
| `assert` | DONE 后验收：`ok` / `attempts` / `elapsed_ms` / 逐条 `checks` | 断言为什么没过 |
| `model` | 没产出决策的模型调用（如 planner） | 少见 |
| `cache` | 录像结局：`mode` / `write` / `hits` / `misses` / `miss_reasons` | 回放对不上 |
| `step_start` / `step_end` | 计划模式的步边界 | 定位是哪一步崩 |

`decision` 行的关键字段：`step` / `url` / `title` / `operation` / `target` / `target_element` /
`confidence` / `model` / `latency_ms` / `usage` / `action`（含 `ok` / `detail` / `level`）/
`signal`（卡住信号）/ `supervisor`（裁决）/ `via`（`cache`|`agent`|`http`|`""`）/ `text_model`。

### `via` 告诉你这一步谁在开车

| 值 | 意思 |
|---|---|
| `cache` | 回放命中，`model` 也是 `cache` |
| `agent` | 宿主经桥作答（`model == "agent"`）|
| `http` | HTTP 模型（Jev / 代班大模型 / 文本模型 / HTTP 监督）|
| `""` | 没有决策，或测试替身没标 model |

`run_start` 里的 `supervisor` 是**布尔**；`decision` 行里的 `supervisor` 是**裁决对象**（含 trigger/ruling/meta）。别混。

## 断言没过怎么分

看 `assert` 行的 `checks[].evidence`：

| evidence | 判断 |
|---|---|
| `页面文本中没有该内容` | 页面真没出现 → 可能真缺陷，或断言字符串写错（大小写/空格/全半角）|
| `没有匹配元素` | 元素标签变了 → 改断言标签 |
| `实际值：xxx` | `element_value` 期望值不对 → 期望值是实测值吗？ |
| `实际匹配 3 个，期望 2 个（快照截断，实际可能更多）` | 含「快照截断」时这条断言不可靠 |
| `仍存在：xxx` | `element_absent` 失败——这个元素还在，改动没生效 |
| `实际 href：...` | `element_attribute` 失败——属性值不是你以为的那个 |

**`attempts` > 1** 说明断言是靠轮询等到的（页面有延迟），不是一次就成立。`timeout_ms` 可加宽。

### 断言没错，是页面真变了

如果 `snapshots/` 里该步最后一张能看到新页面结构，而断言还在找旧标签：
改 YAML 的断言 → **不要 `--relearn`**（断言变了录像仍在，`--relearn` 会白花 token）。

注意区分：这条说的是**断言**写错了。如果是**页面本身**变了导致录像失配，按上面「排障的顺序」走录制流程重录。
两种情况的判据不同——看 `miss_reasons`：空或只有 `retry` 是断言问题；`wrong-context` / `no-variant` / `truncated` 是录像问题。

## 回放对不上：看 `miss_reasons`

| 值 | 含义 | 处置 |
|---|---|---|
| `no-entry` | 这个用例 id / 哈希在缓存里没有录像 | 先录一次 |
| `no-variant` | 有录像但页码/上下文编号对不上 | 页面步骤数变了 → `--relearn` 重录 |
| `wrong-context` | 录像的起点页面与当前页面不符 | 起点 URL 改了，或登录态不同 → `--relearn` 重录 |
| `invalid-entry` | 录像文件坏了 | 删掉那条录像重录 |
| `truncated` | 录像在上次运行中被截断（中途动作失败）| `--relearn` |
| `retry` | 断言失败但记录被保留（`keep_on_check_failed`）| 断言问题，不是录像问题 |

`mode` 与 `write`：

- `mode`：`self-finalized`（断言全过，已定稿）/ `agent-concluded`（模型说完了）/ `missed`（对不上）
- `write`：`saved` / `kept` / `unconfirmed` / `evicted` / `no-change` / `not-written`

只有 `self-finalized` + `saved` 才是「这次录到了一条新录像」。

## 卡住信号

`signal.kind` 只有两种（`run/stuck.py`）：

| kind | 触发条件 | detail 里有什么 |
|---|---|---|
| `repeat_no_progress` | 同一 `(operation, target)` 连续 3 次，且最近一次真动作 `page_changed == false` | 三个动作的 `page_changed` 序列（旧→新），如 `[true, false, false]` |
| `low_confidence` | 连续 3 次 `confidence < 0.55` | 每次的置信度 |

回放决策（`model == "cache"`）**豁免**卡住判定——照播不算卡。

「前面还在变、从第几次起停了」这个序列是判断「没到目标页」还是「到了但没新数据」的关键。
看到 `[true, false, false, false]` 基本是「点对了但页面没反应」→ 检查 `--settle-ms` 是否太短（默认 700ms，
实测 `下一页` 类跳转要 `--settle-ms 1500`）。

## 死循环类问题的三个旋钮

| 现象 | 旋钮 | 说明 |
|---|---|---|
| 页面重，token 爆 | `MAX_ELEMENTS=60` `MAX_TEXT=2000` | 环境变量；默认 250 / 6000。元素多的大首页会把单次 input 抬到几万 token |
| 点击后页面来不及反应 | `--settle-ms 1500` | 默认 700ms |
| 断言等不到 | `--assert-timeout-ms 15000` | 默认 5000ms |

## 报告

```bash
uv run ui-agent report --run-dir .artifacts/runs/xxx
```

重出 `report.html`。它从 `result.json` / `summary.json` 读 `mask_vars`：
开了掩码的报告里，输入的明文一律显示成 `••••••`（**排障时先关掉 `--mask-vars`**，否则连输入了什么都看不见）。

## 上报缺陷之前

`defect_candidates` 只是**模型主张**，不是验证过的复现。`action.warning` 会进这个列表
（例如点击被遮挡）。上报前必须：

1. 对 `screenshots/failure.png` 亲眼确认（`ci` 模式只留这张）。
2. 确认断言 `evidence` 里写的是实际观测值，不是你期望的值。
3. 如果这条只在 `--decider jev` 下出现，回放不复现——很可能是**录制时**的偶发，不是产品缺陷。
