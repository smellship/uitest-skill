# running：起一轮运行并陪它走完

内核是**前台进程**。宿主（你）必须留在循环里：内核每要一次决策就写一个 `ask`，等你写 `ans`。

## 两种跑法，先选一种

| 场景 | 命令骨架 | 谁决策 | 消耗 |
|---|---|---|---|
| 有 Jev key，要真探索/真录制 | `--decider jev --supervisor off` | Jev（云端） | Jev token |
| 没有 Jev key | `--decider agent --supervisor agent` | 你（经桥） | 宿主 token |
| 已有录像，只想复跑 | `--decider off --supervisor off` | 录像 | 零 |

超时、卡住、路走错时想让人接手 → 上表第三列加 `--supervisor agent`。

## 起运行：后台 + 固定 run-dir

```bash
# 1) 起（后台，agent 模式必须显式给 --run-dir）
uv run ui-agent run --goal "查一张 2024 年的票据" \
  --url https://example.com/invoices \
  --decider agent --supervisor agent \
  --run-dir .artifacts/runs/my-run-1 --json &

# 2) 循环取事件
uv run ui-agent bridge wait --run-dir .artifacts/runs/my-run-1 --json

# 3) 按事件作答
uv run ui-agent bridge answer --run-dir .artifacts/runs/my-run-1 --id 3 \
  --data '{"answer": {"operation": {"choice": "CLICK", "probabilities": {"CLICK": 1.0, ...}, "confidence": 0.9}, "click_target": {...}}}'
```

`--run-dir` 省略时内核自动生成 `.artifacts/runs/<goal 前缀>-<时间戳>`，**但你不知道路径就无法 wait**。
所以 agent 模式一律显式给，并且用固定名字方便人看。

`bridge wait` 默认等 240s。给它 `--timeout 30` 可以更频繁地回来做别的事（比如给用户报进度）。

## `wait` 的四种返回

```json
{"ok": true, "event": "ask", "kind": "decide", "ask": {"id": 3, "kind": "decide", "at": "...", "deadline_s": 180.0, "payload": {...}}}
{"ok": true, "event": "finished", "state": "finished", "detail": "status=ok", "result": {...}}
{"ok": true, "event": "finished", "state": "crashed", "detail": "RuntimeError: ...", "result": null}
{"ok": true, "event": "heartbeat"}
```

- `ask`：有一件事等你。看 `kind` 决定读哪篇 reference：
  - `decide` → [deciding.md](deciding.md)
  - `supervise` → [supervising.md](supervising.md)
  - `text` → 下面「text ask」
- `finished`：`state` 是 `finished` 或 `crashed`；`result` 是 `result.json` 的内容（单目标）
  或 `summary.json`（多步计划）。**`event=finished` 不等于成功**——看 `result.status`。
- `heartbeat`：这段时间没事件。**不是错误**，继续 wait。

`deadline_s` 是内核愿意等多久（默认 180s，`--bridge-timeout` 可调）。超过它内核抛
`BridgeTimeout` 并直接停机（`status=error`），不会重试。

## `answer` 的三种结局

| 返回 | 含义 | 该做什么 |
|---|---|---|
| `{"ok": true, "accepted": true, "id": 3}` | 收下了 | 继续 wait |
| `{"ok": true, "accepted": false, "reason": "stale"}` | 这个 ask 已过期/已答过/运行已结束 | **不是错误，退出码 0**。直接再 wait；如果连着 stale，说明 run 已经结束了 |
| 退出码 2 + `失败：...` | 参数或 JSON 格式错 | 修 `--data` / `--file` |

`--data` 的顶层必须是对象且含 `answer` 对象：`{"answer": {...}}`（可带 `note`、`answered_by`）。
`--file` 走文件，长载荷用它避免命令行转义地狱。

## text ask：内核缺字段值

```json
{"kind": "text", "payload": {"goal": "...", "field": {"label": "请输入帐户名", "role": "textbox", "current_value": ""},
 "page": {"url": "...", "title": "...", "text": "..."}, "recent_actions": [...], "hint": "控件标签「…」不含任何现有 --var 键（…）——键名需为控件标签的子串", "attachments": []}}
```

作答：`{"answer": {"text": "sysadmin"}}`。规矩：

- **只回 `text` 一个键**，非空字符串，≤2000 字符。多一个键、空串、超长都会被拒（`未给出可用的字段值`）后被当成运行时错误。
- **`hint` 里说「键名不匹配」时优先改命令行 `--var` 键名重跑**，不要瞎猜值。
- secret 字段**永远不会**经桥发问（内核直接报错 `敏感字段「…」只能通过 --var 提供`）。所以密码等一律 `--var`。

`attachments` 在 v1 恒为空数组：截图不进任何模型输入。

## 运行结束看什么

`<run-dir>/result.json`（单目标）或 `<run-dir>/summary.json`（计划）里的 `status` 决定成败：

| status | 含义 | 下一步 |
|---|---|---|
| `ok` | 全部断言通过 | 收工，或复跑验证 |
| `done_unverified` | 模型报 DONE，但没配断言 | **补断言**，别当成功 |
| `check_failed` | 断言没过 | 看失败证据；可能是断言写错也可能是真缺陷 |
| `blocked` | 决策模型说推不动 | 看 trace 最后几条；常是权限/数据缺失 |
| `stuck` | 触发升级信号且没救回来 | 见 [debugging.md](debugging.md) |
| `aborted` | 监督模型判定无法继续 | 看 `reason` |
| `replanned` | 监督给了新步骤序列，本次停在原处 | 收下新计划再跑一次 |
| `budget` | 动作/决策/监督预算耗尽 | 拆小目标或调预算 |
| `cache_missed` | 零模型模式下没有可用录像 | 见下面「复跑」 |
| `skipped` | 前序步骤没过 | 先修前一步 |
| `error` | 运行时错误 | 看 `detail` 与 `kernel.stderr` |

`--json` 时 stdout 只有一行 JSON（`ok/status/status_label/detail/run_dir/report/steps/checks/defect_candidates`）。
不加 `--json` 是人读的进度日志。

## 复跑省 token

```bash
uv run ui-agent run --plan-file cases/x/others/x.plan.json \
  --decider off --supervisor off --run-dir .artifacts/runs/rep-1 --json
```

`--decider off` 时每一步都靠录像；任何一处对不上就 `cache_missed` 停下（**不会偷偷问模型**）。

看录像命中情况：**根 `trace.jsonl` 的 `cache` 行**（倒数第二行）里的 `mode` / `write` /
`hits` / `misses` / `miss_reasons`。`summary.json` 与 `result.json` 不含这些字段。

```json
{"kind": "cache", "mode": "self-finalized", "write": "kept", "hits": 9, "misses": 0,
 "miss_reasons": {}, "kept_steps": 3, "saved_steps": 0, "evicted_steps": 0}
```

`mode` 三值：`self-finalized`（断言全过，录像已定稿）、`agent-concluded`（模型说完了）、`missed`（对不上）。
`write` 六值：`saved` / `kept` / `unconfirmed` / `evicted` / `no-change` / `not-written`。

失配时二选一：

- 页面真的变了 → `--relearn` 重录（花 token）。
- 断言写错/太严 → 改 YAML 再跑，**不要重录**（断言变了录像仍在，见 [cases.md](cases.md)）。

`--cache-strict` 让命中失败即硬停；默认行为已是「对不上就停」，一般不用开。

## explore：还不知道该点哪儿的时候

```bash
uv run ui-agent explore --url https://example.com --goal "找到 2024 年的票据并打开详情" \
  --decider agent --run-dir .artifacts/runs/exp-1 --json &
```

产物在 `<run-dir>/explore/`：`goal.txt`、`trajectory.md`（按页面分组走过的动作）、
`explored.yaml`（**故意 `checks: []` 的骨架，编译必失败**）、`explore.json`。

explore **不写录像也不写缓存**——探索路径里有大量无用页面，录下来会污染回放。
正确顺序：explore 摸路 → 你照着 `trajectory.md` 写正经验收断言 → 录制那次才进缓存。

## 别做的事

- 别在同一 `--run-dir` 上并行起两个 run：桥的 `ask-0001.json` 会互相覆盖。
- 别在 `wait` 返回 `heartbeat` 时判定「卡死了」：冷启动到第一个决策实测可到 60–90 秒。
- 别杀掉内核进程后继续 `wait`：`status.json` 会停在 `crashed`，`wait` 返回 `event=finished`。
- 别用 `--decider off` 跑从没录过的用例，指望它给你探索：它只会 `cache_missed`。
