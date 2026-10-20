# exploring：还不知道该点哪儿的时候

`explore` 不是新引擎，就是**一次没有断言的单目标运行**：`checks=[]`、`cache=None`。
所以模型一说 DONE 就落到 `done_unverified` —— 那正是想要的结果：它说走完了，但没有任何东西证明。

## 什么时候该 explore

- 拿到一个不熟悉的站点，要先摸清入口和路径。
- 用例的 goal 不知道该怎么说才踩得中控件。

**不该 explore 的时候**：已经知道该怎么点（直接写 YAML 跑 run）；已有能用的录像（直接回放）。

## 跑

```bash
# Jev 探索（有 key，快，花 Jev token）
uv run ui-agent explore --url https://cn.bing.com/ \
  --goal "搜索大肥鱼，翻到第二页结果就停" \
  --decider jev --supervisor off \
  --var 输入搜索词=大肥鱼 \
  --run-dir .artifacts/runs/exp-1 --json

# 你自己探索（没 key；--decider agent 是 explore 的默认值）
uv run ui-agent explore --url https://cn.bing.com/ \
  --goal "搜索大肥鱼，翻到第二页结果就停" \
  --decider agent --supervisor agent \
  --run-dir .artifacts/runs/exp-2 --json &
# 之后照 running.md 的循环 wait / answer
```

`--decider agent` 时 `--run-dir` 必填（宿主得在浏览器起来前知道桥在哪）。
`--supervisor` 在 explore 里可以 `on` / `off` / `agent`：卡住时要不要人救。
`--decider` 只接受 `jev` / `agent`（不会给你 `off` —— 没录像可回放）。

## 产物

`<run-dir>/explore/` 下四件：

| 文件 | 内容 | 用途 |
|---|---|---|
| `goal.txt` | 这次的目标原文 | 对照用 |
| `trajectory.md` | 按页面分组的动作序列 | **主要给 agent 读**，比 `actions.json` 省 context |
| `explored.yaml` | 用例骨架：`id` + `title` + `goal`，`checks: []` | 起点，断言必须你补 |
| `explore.json` | `{goal, status, steps_explored, bridge_asks, trajectory, case_skeleton, decider}` | 机器读的索引 |

另外照旧有 `trace.jsonl` / `actions.json` / `result.json` / `snapshots/` / `screenshots/`。

`trajectory.md` 长这样：

```markdown
## https://cn.bing.com/「必应搜索」
 01. TYPE_TEXT「输入搜索词」← 值来自 --var（不落盘、不进模型）
 02. CLICK「百度一下」 → 页面有变化
## https://cn.bing.com/search?q=...
 03. SCROLL_DOWN （level 1）
 04. CLICK「下一页」 → 页面有变化
```

**轨迹里永远不出现 `--var` 的值**，只写「值来自 --var」。secret 字段写「敏感字段（已掩码）」。
这是刻意的：探索产物要能安全地贴给人和 agent 看。

`bridge_asks` 是本次探索的桥问答次数。agent 模式下 token 花在宿主循环里，内核只能数这个。

## 从探索到用例：四段，别跳

```
① explore            乱走，不录，不写缓存
② 你写 YAML          照 trajectory.md 的控件标签写 goal，自己写断言
③ 跑那次 YAML        这一次才录制，同时第一次真正证明断言成立
④ --decider off      之后零 token 复跑
```

**② 和 ③ 之间那次真跑不能省。** 探索里模型说「我看到了结果」，那只是自称；
只有 ③ 让断言真的对着 DOM 过一遍，录像才有意义（`mode=self-finalized` 才会落盘）。

也**不能把 ① 直接录下来**：探索会点开一堆用不到的页面（下拉、日历、误入的详情页），
录进回放就会在下次重放这些垃圾动作。

## `explored.yaml` 为什么编译不过

```yaml
# 探索产物骨架（status=done_unverified）：checks 必须由 agent 依据轨迹补全
id: explore-case
title: 搜索大肥鱼，翻到第二页结果就停

steps:
  - goal: |
      搜索大肥鱼，翻到第二页结果就停
    checks: []
```

`checks: []` 会撞 `checks_empty` 错误，退出码 1。这是**故意**的：
不替模型编断言，否则等于把它的自称当证据。

你要做的三件事：

1. 把 `id: explore-case` 改成有意义的用例编号（`id` 是缓存身份，定了就别再改）。
2. 把大目标拆成 2–4 步，每步一个可验收的页面状态变化。
3. 每步补 `checks`，最后一步必须含结果类断言（见 [cases.md](cases.md)）。

## 探索跑不好时

| 症状 | 原因 | 修法 |
|---|---|---|
| `repeat_no_progress`：同一操作连 3 次 | goal 里的值填不进控件 | 检查 `--var` 键名是否是控件标签的**子串** |
| `max_tokens_exceeded` | 页面元素太多 | `MAX_ELEMENTS=60`、`MAX_TEXT=2000`（见 [setup.md](setup.md)） |
| `blocked`：跳验证码/风控 | 站点反爬 | 换站点；本机 IP 对百度全站返回滑块验证，必应可用 |
| `low_confidence`：连续 3 次置信度 < 0.55 | 页面没有可推进的控件，模型在瞎猜 | 加 `--supervisor agent` 让人救，或改 goal |
| 走了很多步但没到目标 | goal 太笼统 | goal 里写清判据（「停在第二页」而不是「翻页」） |

实测数字：必应首页 214 元素 / 29K 字符，一次探索吃掉 60 万 input token
（12 次调用 × 均 5 万/次）。同样的动作在百度首页（34 元素 / 3.6K 字符）只要零头。
**页面越大，每次决策重发的 state 越贵** —— 探索前先把 `MAX_ELEMENTS` 收小。

## 探索之后要不要留 `explore/`

留。它是「这次为什么这么写用例」的唯一记录，也是下次页面改版时对照的基线。
`.artifacts/` 本来不进版本库。
