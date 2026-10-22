# uitest-skill

把「浏览器 UI 测试」做成对话大模型能直接驱动的一套东西：一个内核（Playwright 执行 + 独立断言 + 回放缓存）
+ 一个让宿主对话模型接替所有 LLM 缝的文件桥。

```
ui-agent/        发布的 skill 包 —— 自足，整个目录丢进宿主的 skills 目录即可用
  SKILL.md       宿主 agent 的入口（路由表 + 铁律 + 工作流）
  references/    13 篇按需读的说明书
  scripts/       bootstrap.ps1 / bootstrap.sh（幂等）
                 host_loop.py（宿主循环参考实现）
                 parallel_run.py（多路并行编排）
                 check_docs.py（文档与内核一致性校验）
  kernel/        内核本体（本包自带一份，独立 .venv）
kernel/          内核开发树（源码 / 测试 / 冒烟脚本 / spec）
HANDOFF.md       从零接手这份工作要读的全部背景（包含目录边界与硬约束）
spec-explore.md  探索出用例的设计（explore → 写 YAML → 录制 → 零 token 回放）
```

**本仓库没有用例，也不该有。** 用例跟着被测系统走，改得比内核勤，混在一起会让每次升级都撞上一堆用例改动。
用例放在独立的仓库里（示例：<https://github.com/smellship/case_demo>），
skill 只教怎么写、怎么跑、产物放哪。包里唯一带的是一个离线测试页
`ui-agent/kernel/examples/local_fixture.html`——它是**夹具**，不是用例。

`ui-agent/kernel/` 由 `kernel/scripts/build_skill.py` 从开发树同步生成，**不要手改**。
改内核请改 `kernel/src/`，再跑一次同步：

```bash
cd kernel
uv run python scripts/build_skill.py           # 同步
uv run python scripts/build_skill.py --check   # 只校验漂移（CI 用）
```

## 用起来

把 `ui-agent/` 整个目录放进宿主的 skill 目录（Qoder 技能目录 / `.claude/skills/` / 内部 jx-skills 库 /
`npx skills add` 布局）。宿主读 `SKILL.md` 就知道该读哪篇 reference。

它**自足**：文档与内核在一起，不用另装别的东西。依赖与 Chromium 都装在包内
（`ui-agent/kernel/.venv`、`ui-agent/kernel/.browsers`，约 400 MB），不写系统目录。

一条命令自检（幂等，已装的跳过，退出码 0 = 就绪）：

```powershell
cd ui-agent
.\scripts\bootstrap.ps1              # Windows
```
```bash
cd ui-agent
./scripts/bootstrap.sh --check       # macOS / Linux，--check = 只报告
```

## 三层分工

| 层 | 谁做 | 做什么 |
|---|---|---|
| 编排 / 裁决 | 对话大模型（宿主） | 拆步骤、卡住时裁决、给字段填值、逐跳决定点哪 |
| 决策 | Jev 或宿主模型 | 看快照元素表，选一个动作 + 目标 index |
| 执行 | Playwright | 守卫、点击降级阶梯、断言、录像 |

**模型输出永不变成 selector / 坐标 / 可执行 JS**；元素只能用当次快照分配的 index 引用。
断言独立于模型自述——`DONE` 不是成功证据。

## 两种跑法

命令都在 `ui-agent/kernel/` 下执行（或 `uv run --project <本包>/kernel ui-agent ...` 从任意目录调）。

先跑**离线夹具**确认装好了（不联网、零模型调用，页面是包内 `kernel/examples/local_fixture.html`）：

```bash
uv run ui-agent run --goal "在「账号」和「密码」里填入给定值，点击「确 定」，然后点击「查询」" \
  --url "file:///$PWD/examples/local_fixture.html" \
  --check "element_exists=查询" --check "text_contains=查询完成" \
  --decider off --supervisor off --var 账号=demo --var 密码=demo --run-dir /tmp/probe --json
```

第一次会报 `cache_missed`（没录过），这是对的。要录制就换成 `--decider agent`（走桥，宿主作答）
或 `--decider jev`（要 Jev key）。之后再跑上面这条，就是零 token 回放。

真实流程（用例来自你自己的用例库）：

```bash
# 全自动（要 Jev key，按步付费）
uv run ui-agent run --plan-file "$PLAN" --url "$URL" --decider jev --supervisor off \
  --run-dir "$OUT/rec" --json

# 宿主模型逐跳作答（token 花在对话里，内核零 key）。--run-dir 必填：宿主得先知道桥在哪
uv run ui-agent run --plan-file "$PLAN" --url "$URL" \
  --decider agent --supervisor agent --run-dir "$OUT/rec" --json &
#   宿主侧循环（起 run 后直接 wait 即可，桥目录出现前 wait 会自己等）：
uv run ui-agent bridge wait   --run-dir "$OUT/rec" --json
#   → {"ok":true,"event":"ask","kind":"decide","ask":{"id":3,...}}
uv run ui-agent bridge answer --run-dir "$OUT/rec" --id 3 --data '{"answer":{...}}'

# 零 token 复跑（录像已存在）
uv run ui-agent run --plan-file "$PLAN" --url "$URL" --decider off --supervisor off \
  --run-dir "$OUT/rep" --json
```

不想自己写 `wait`/`answer` 循环：`python ui-agent/scripts/host_loop.py --help`，
它是同一套协议的参考实现（只用标准库，不 import 内核），照抄改 policy 即可。

## 并行：一次跑多个用例

内核支持多进程同时各跑一个用例（一路一个浏览器）。并行的最小前提是
**每路一个 profile 目录**——Chromium 对同一个 `user-data-dir` 加单实例锁，共用就起不来：

```bash
uv run ui-agent run --plan-file "$PLAN_A" --url "$URL" --run-dir "$OUT/a" \
  --profile-dir "$PROF/a" --decider agent --supervisor off
uv run ui-agent run --plan-file "$PLAN_B" --url "$URL" --run-dir "$OUT/b" \
  --profile-dir "$PROF/b" --decider agent --supervisor off
```

`--run-dir` 也必须每路不同（桥目录跟着它走，撞了就会答错 lane）。
编排放 `ui-agent/scripts/parallel_run.py`（写一份 `jobs.json` 一次拉起 N 路），
宿主也可以**创建子 agent 并行**——并发上限跟随宿主自己的设置，每路一个子 agent、一个用例。

细节见 `ui-agent/references/parallel.md`。

## skill 四段工作流

1. **探索**（可乱走，**不录制**）→ `<run-dir>/explore/` 出轨迹与用例骨架
2. **宿主模型读轨迹写 YAML 断言**（骨架的 `checks: []` 故意过不了编译，逼你真写）
3. **跑这份 YAML**——这一次既录制，又证明断言真的成立
4. **`--decider off`** 零 token 复跑

第 2 步到第 3 步之间那次真跑不能省，否则断言只是模型自称。

## 当前状态

- 内核：`ruff` 无告警、单测 **597 passed**、11 个真实 Chromium 冒烟脚本（`kernel/scripts/`）全绿
- 已落地：Agent 桥（四档开关）、explore 子命令、YAML `id:` 作缓存身份、仅断言失败保留录像、
  `--mask-vars`、5 个浏览器开销开关、可选 MCP 适配器（`extra_args` 透传其余 run 开关）
- skill 包：`ui-agent/`（自足，含内核副本；`check_docs.py` 可验文档与内核是否脱节，
  `build_skill.py --check` 可验内核副本漂移）
- 未知：MCP 只在装了 `mcp` 包时可用（`uv sync --extra mcp`）；closed shadow root 与
  网络请求/响应拿不到（见 `kernel/AGENTS.md` 待办节）

## 开发上手

```bash
cd kernel
uv sync
uv run python scripts/smoke_cli.py        # 零模型调用，验证装好了
```

细节都写在 `kernel/README.md`（命令全集与产物布局）、`kernel/AGENTS.md`（硬约束与待办）、
`ui-agent/SKILL.md`（给 agent 的入口）。
