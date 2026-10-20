# uitest-skill

把「浏览器 UI 测试」做成对话大模型能直接驱动的一套东西：一个内核（Playwright 执行 + 独立断言 + 回放缓存）+ 一个让宿主对话模型接替所有 LLM 缝的文件桥。

```
kernel/          内核（Python，uv 管理）。只按 argv 调 CLI，永不 import 到宿主
cases/           用例库：场景/YAML，一个 YAML 就是一条可复跑的用例
HANDOFF.md       从零接手这份工作要读的全部背景（包含目录边界与硬约束）
spec-explore.md  探索出用例的设计（explore → 写 YAML → 录制 → 零 token 回放）
```

## 三层分工

| 层 | 谁做 | 做什么 |
|---|---|---|
| 编排 / 裁决 | 对话大模型（宿主） | 拆步骤、卡住时裁决、给字段填值、逐跳决定点哪 |
| 决策 | Jev 或宿主模型 | 看快照元素表，选一个动作 + 目标 index |
| 执行 | Playwright | 守卫、点击降级阶梯、断言、录像 |

**模型输出永不变成 selector / 坐标 / 可执行 JS**；元素只能用当次快照分配的 index 引用。断言独立于模型自述——`DONE` 不是成功证据。

## 两种跑法

```bash
# 全自动（要 Jev key，按步付费）
uv run ui-agent run --plan-file cases/大肥鱼/others/bing-search-paging.plan.json --decider jev --supervisor off

# 宿主模型逐跳作答（token 花在对话里，内核零 key）
uv run ui-agent run --plan-file <plan> --decider agent --supervisor agent --run-dir <dir>
#   另一个终端循环：
uv run ui-agent bridge wait   --run-dir <dir> --json
uv run ui-agent bridge answer --run-dir <dir> --ask-id 3 --answer '{"..."}'

# 零 token 复跑（录像已存在）
uv run ui-agent run --plan-file <plan> --decider off --supervisor off
```

## skill 四段工作流

1. **探索**（可乱走，**不录制**）→ `<run-dir>/explore/` 出轨迹与用例骨架
2. **宿主模型读轨迹写 YAML 断言**（骨架的 `checks: []` 故意过不了编译，逼你真写）
3. **跑这份 YAML**——这一次既录制，又证明断言真的成立
4. **`--decider off`** 零 token 复跑

第 2 步到第 3 步之间那次真跑不能省，否则断言只是模型自称。

## 当前状态

- 内核：`ruff` 无告警、单测 **590 passed**、11 个真实 Chromium 冒烟脚本（`kernel/scripts/`）全绿
- 已落地：Agent 桥（四档开关）、explore 子命令、YAML `id:` 作缓存身份、仅断言失败保留录像、`--mask-vars`、5 个浏览器开销开关、可选 MCP 适配器
- 未知：MCP 只在装了 `mcp` 包时可用（`uv sync --extra mcp`）；closed shadow root 与网络请求/响应拿不到（见 `kernel/AGENTS.md` 待办节）

## 上手

```bash
cd kernel
uv sync
uv run python scripts/smoke_cli.py        # 零模型调用，验证装好了
```

细节都写在 `kernel/README.md`（命令全集与产物布局）与 `kernel/AGENTS.md`（硬约束与待办）。
