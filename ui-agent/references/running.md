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
  --run-dir "$OUT" --json &

# 2) 循环取事件
uv run ui-agent bridge wait --run-dir "$OUT" --json

# 3) 按事件作答
uv run ui-agent bridge answer --run-dir "$OUT" --id 3 \
  --data '{"answer": {"operation": {"choice": "CLICK", "probabilities": {"CLICK": 1.0, ...}, "confidence": 0.9}, "click_target": {...}}}'
```

`$OUT` 是这次运行的产物目录。**在用例库里它必须是 `<用例库>/<场景>/outputs/<时间戳>`**
（见 [cases-repo.md](cases-repo.md)）：不给 `--run-dir` 时内核自己生成
`.artifacts/runs/<goal 前缀>-<时间戳>`，那个路径跟着 skill 包走——你既没法 `bridge wait`，
产物也进不了版本控制。

**先起 run 再 wait，不需要 sleep。** `bridge wait` 发现桥目录还没出现时，会自己等最多
`--wait-dir`（默认 20s）——内核起浏览器要几秒，这段就靠它吸收。所以照上面 1→2 顺序直接连着敲即可。

`bridge wait` 默认等 240s。给它 `--timeout 30` 可以更频繁地回来做别的事（比如给用户报进度）。

**循环的骨架**（伪代码，宿主侧实现）：

```
起 run（后台，--run-dir 固定）
loop:
    ev = bridge wait --run-dir R --timeout 30 --json
    if ev.event == "heartbeat": continue
    if ev.event == "finished": break        # 再看 result.status，别当成功
    if ev.event == "ask":
        data = 按 ev.kind 造答复             # decide / supervise / text
        bridge answer --run-dir R --id ev.ask.id --data '{"answer": ...}'
等 run 进程退出，读 result.json
```

只有两个终止条件：`event=finished`，或 `bridge wait` 退出码非 0（真的没桥）。

### 不想自己写循环：直接用 `scripts/host_loop.py`

包内自带一份**可运行的**宿主循环，只用标准库、不 import 内核，照抄改 policy 即可：

```bash
# 先看它怎么用
python scripts/host_loop.py --help

# 拿包内离线夹具跑一遍（默认 policy=fixture，只对 local_fixture.html 成立）
python scripts/host_loop.py \
  --run-dir "$OUT/rec" --relearn \
  --plan-file "$CASE_DIR/others/login-query.plan.json" \
  --url "$FIXTURE" \
  --var 账号=demo --var 密码=demo

# 零 token 回放：换 --decider off，宿主根本不用作答
python scripts/host_loop.py \
  --run-dir "$OUT/rep" --decider off --supervisor off \
  --plan-file "$CASE_DIR/others/login-query.plan.json" \
  --url "$FIXTURE" --var 账号=demo --var 密码=demo
```

它负责四件容易写错的事：起内核并把 stdout/stderr 落到 `<run-dir>/host_loop.stdout.log`（**不用管道，避免死锁**）、
按 `st_mtime` 判断哪些 ask 还没答（同一个 `--run-dir` 复跑时不会被上一轮的 `ans-*` 骗）、
把 `--plan-file` 解析成绝对路径（子进程 cwd 是 `kernel/`，相对路径会错）、
以及读终态后按 `status` 决定退出码（`ok` → 0，`crashed` → 2，其余 → 1）。

`--decider off` / `--decider jev` **不建桥目录**，它识别这种情况后改为直接等进程收尾、读 `result.json`，
不会白等 20s 再报「桥目录还没出现」。

`--policy fixture` 只认离线夹具。换成 `--policy <你的.py>` 指向自己写的策略模块，
里面给 `answer(ask, ctx)`（或分开的 `decide` / `supervise` / `text` 三个函数）。
**decide 的返回值要多包一层**：`{"answers": {...}}`，不是直接把 answers 交回去。

另外两个开关：

- `--profile-dir <目录>`：浏览器登录态目录。并发跑多路时**每路必须不同**
  （Chromium 对同一个 `user-data-dir` 加单实例锁），见 [parallel.md](parallel.md)。
- `--goal-file <文件>`：目标写在 UTF-8 文件里。**中文长目标一律用它**——PowerShell 传中文参数会按
  ANSI 重编码，内核收到乱码后报 `unrecognized arguments`。
- `--explore`：走 `explore` 而不是 `run`（不录制、不验收），见下面「explore」。

## 不想写 policy：用 `scripts/ask.py` 一问一答

策略模块适合「规则已经清楚」的用例。**还在摸路、每次该点哪儿都要看页面**的时候，
逐条手动作答更直接——`ask.py` 把一条 ask 压成几十行的摘要（原始 `ask-*.json` 可以有
300 KB / 250 个元素，**永远不要直接读它**）：

```bash
python scripts/ask.py show --run-dir "$OUT"                 # 当前待答的是什么、有哪些候选
python scripts/ask.py show --run-dir "$OUT" --grep "run query"   # 只在候选里搜关键字
python scripts/ask.py answer --run-dir "$OUT" --op CLICK --target 36
python scripts/ask.py answer --run-dir "$OUT" --done
python scripts/ask.py answer --run-dir "$OUT" --op TYPE_TEXT --target 7 --text "2026-10-21"
python scripts/ask.py answer --run-dir "$OUT" --error "权限不足，打不开这个菜单"
```

`show` 打印：目标、页面标题与截断 URL、正文前 600 字、可选操作、每个目标 head 的候选
（`键  标签` 两列，`--limit` 默认 40）、以及**该敲哪条 `answer` 命令**。
`answer` 自己造一份能过 `validate_choice` 的答复（覆盖全部候选键、和=1、被选概率最大），
所以不用手写 `probabilities`。`--id` 与当前待答 ask 不符会直接拒绝，防止答到上一轮变 `stale`。

它只认文件协议、**不 import 内核**，所以哪个 python 都能跑。

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
- `wait` 退出码 **2** 才是真错（桥目录等满 `--wait-dir` 仍不存在 / 参数不合法）。这种情况 run 多半没起来。

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

**`detail` 里有一句话值得单独认**：`动作中途断言已成立：全部断言通过`。
意思是某一步的动作（比如点「查验」）已经让该步断言全过，内核**不再向模型要下一个决策**、
直接收尾 —— 典型场景是点完按钮弹出结果浮层，模型看不到「这就是结果」，只会继续往浮层下面点。
它是成功（`status=ok`），只是收尾方式不同：录像里会补一条 `DONE`，`trace.jsonl` 里**没有**
对应的 decision 行（那不是任何模型的决策），收尾理由由 `assert` 行与 `run_end.detail` 交代。

## 复跑省 token

```bash
uv run ui-agent run --plan-file "$PLAN" \
  --url "$URL" --decider off --supervisor off \
  --var 账号=demo --var 密码=demo \
  --run-dir "$OUT" --json
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
  --decider agent --run-dir "$OUT" --json &
```

产物在 `<run-dir>/explore/`：`goal.txt`、`trajectory.md`（按页面分组走过的动作）、
`explored.yaml`（**故意 `checks: []` 的骨架，编译必失败**）、`explore.json`。

explore **不写录像也不写缓存**——探索路径里有大量无用页面，录下来会污染回放。
正确顺序：explore 摸路 → 你照着 `trajectory.md` 写正经验收断言 → 录制那次才进缓存。
`$OUT` 同样落在该场景的 `outputs/` 下（见 [cases-repo.md](cases-repo.md)）。

## 别做的事

- 别在同一 `--run-dir` 上并行起两个 run：桥的 `ask-0001.json` 会互相覆盖。
- 别在 `wait` 返回 `heartbeat` 时判定「卡死了」：冷启动到第一个决策实测可到 60–90 秒。
- 别杀掉内核进程后继续 `wait`：`status.json` 会停在 `crashed`，`wait` 返回 `event=finished`。
- 别用 `--decider off` 跑从没录过的用例，指望它给你探索：它只会 `cache_missed`。
