# 并行：同时跑多个用例

一次跑一个用例是最慢的用法。多数时候你手上有 5 到 20 个用例，它们之间没有依赖——
这时候该并行。本文讲清**什么能并行、什么会互相踩、子 agent 怎么分活**。

---

## 1. 一条铁律：一路一个浏览器，一个 profile 目录

内核用 `launch_persistent_context(user_data_dir=...)` 起浏览器——这是为了**保住登录态**
（不然每个用例都要重新登录）。代价是 Chromium 对同一个 `user-data-dir` 加**单实例锁**：
第二个进程用同一个目录就起不来。

所以并行的最小前提是：**每一路给自己的 profile 目录**。

```bash
# 两个终端，各跑一个用例
uv run ui-agent run --plan-file "$PLAN_A" --url "$URL" \
  --run-dir "$OUT/a" --profile-dir "$PROF/a" --decider agent --supervisor off

uv run ui-agent run --plan-file "$PLAN_B" --url "$URL" \
  --run-dir "$OUT/b" --profile-dir "$PROF/b" --decider agent --supervisor off
```

`--profile-dir` 也吃环境变量 `UI_AGENT_PROFILE_DIR`（CLI 优先）。

忘了给会怎样：后起的那一路立刻失败，报的是

```
profile 目录已被另一个浏览器占用：<目录>。并发跑多个用例时每个进程要给自己的 --profile-dir（或设 UI_AGENT_PROFILE_DIR）
```

看到这句就是撞锁了，不是页面问题、不是网络问题——**加 `--profile-dir` 重跑**即可。真撞到时
底层 Chromium 的原始报错是一句很误导的 `Target page, context or browser has been closed`，
内核把它翻译成了上面那句。

### profile 目录要不要复用

- **同一个用例重跑** → 复用同一个 profile，登录态能留下，第二次可能少几步。
- **不同用例并行** → 必须分开。
- **要干净环境**（怀疑残留 cookie 影响结果）→ 删掉那个 profile 目录再跑，或换一个新路径。
- **目录从哪来** → 统一放 `<用例库>/.profiles/<用例或 lane 名>/`，并把它写进用例库的 `.gitignore`。
  那里面是一团 Chromium 用户数据（cookie、LocalStorage、缓存），几十上百 MB，且与这台机器绑定——
  提交进库既撑仓库，换台机器也用不上。`parallel_run.py` 默认就把 profile 放在 `<out>/.profiles/<name>/`。

---

## 2. 三样东西必须每路独立

| 东西 | 怎么给 | 撞了会怎样 |
|---|---|---|
| 浏览器 profile | `--profile-dir` | 后起的那路**起不来**（单实例锁） |
| 产物目录 | `--run-dir` | 两路的 trace / result / 截图**互相覆盖**，事后无法复盘 |
| 桥目录 | 随 `--run-dir` 走（`<run-dir>/bridge`） | 宿主答错 lane 的 ask，等于让 A 的决策落到 B 上 |

桥目录默认就是 `<run-dir>/bridge`，所以只要 `--run-dir` 不撞，桥也不会撞。
除非你显式设了 `UI_AGENT_BRIDGE_DIR`——**并行时不要设那个变量**，它会让所有路共用一个桥。

录像（cache）**不用**分开：`UI_AGENT_CACHE_DIR` 是共享的，不同用例的键本来就不同；
同一个用例被两路同时录制时，后写的赢（`os.replace` 原子替换，不会写坏文件）。
但**同一个用例并行录制没有意义**——两路录的是同一段动作，纯浪费 token。

---

## 3. 用 `scripts/parallel_run.py` 一次拉起多路

手写 N 个终端不现实。包里带了一个编排器：

```bash
# 清单文件最省事
python scripts/parallel_run.py --manifest jobs.json --max-parallel 4
```

`jobs.json` 的形状（路径相对清单文件所在目录）：

```json
{
  "max_parallel": 4,
  "defaults": {
    "policy": "fixture",
    "decider": "agent",
    "supervisor": "off"
  },
  "jobs": [
    {
      "name": "login-query",
      "plan_file": "场景A/others/login-query.plan.json",
      "url": "https://example.internal/login",
      "vars": {"账号": "demo", "密码": "demo"},
      "relearn": true
    },
    {
      "name": "bill-query",
      "plan_file": "场景B/others/bill-query.plan.json",
      "url": "https://example.internal/bill",
      "vars": {"单据号": "A0001"}
    }
  ]
}
```

- **相对路径按清单文件所在目录解析**，不按你当前 cwd——把清单和用例库放一起最省心。
- 每个作业一个 `name`，决定它的产物目录 `<out>/<name>/` 与 profile `<out>/.profiles/<name>/`。
- 作业级字段覆盖 `defaults`；`vars` 是对象，不是字符串数组。
- `extra` 是原样透传给 `host_loop.py` 的参数数组（放 `defaults` 里就是全体共用），
  比如 `["--explore"]`、`["--settle-ms=300"]`。
- `--max-parallel` 是并发上限，不写就全上。

不用清单也可以，命令行直给：

```bash
python scripts/parallel_run.py --out /tmp/runs --max-parallel 2 \
  --job a=场景A/others/login-query.plan.json \
  --job b=场景B/others/bill-query.plan.json \
  --url https://example.internal/login --var 账号=demo --var 密码=demo
```

（`--job` 模式下 `--url` / `--var` / `--decider` 之类是**所有作业的公共值**，
要每个作业不同就用清单。）

### 它做了什么、不做什么

做：分配目录 → 并发起 N 个 `host_loop.py` → 收尾按 `status` 汇总（全 `ok` 才 exit 0；
`extra` 里带 `--explore` 的作业没有断言可验，`done_unverified` 也算过）。

不做：不 import 内核、不解析桥、不替你决策。真正回答 ask 的仍是 `host_loop.py` 里的策略
（`--policy fixture` 或你自己的 `.py`）。所以**它只适合策略已经确定、能机械作答的场合**。

现成的策略只有两个：`--policy fixture`（只认包内离线夹具那一个页面）和
`--policy scripts/explore_policy.py`（通用探索策略：找最像「提交/查询/执行」的按钮点一次就收，
见 [exploring.md](exploring.md)）。真实业务页面基本都得自己写一份——照 `explore_policy.py`
的接口 `answer(ask, ctx) -> dict` 抄，decide 的返回值记得包一层 `{"answers": {...}}`。

**探索也能并行**：清单里给每一路 `"extra": ["--explore"]` 加各自的 `goal` + `url`，
一个浏览器探一个页面。实测两路并行墙钟 9s（单路各 8.2s / 8.5s，串起来是 17s）。

`--decider agent` 需要策略能答；如果这一轮要你（宿主模型）亲自逐步决策，
那就别用这个脚本，改用下面第 4 节的子 agent 方式——每路一个你。

---

## 4. 用子 agent 并行（推荐给需要思考的场景）

**宿主 agent 可以创建子 agent 并行工作。** 这不是绕过限制，是这套流程的正常用法：

- 每一路一个子 agent，各自跑一个用例，各自维护自己的 `bridge wait` 循环；
- **并发数上限跟随宿主 agent 自己的设置**——宿主允许几个子 agent，就最多开几个，
  不要为了"跑快点"去超；宿主没开这个能力就串行跑，别自己发明并行；
- 一个子 agent 只负责一个 run，不要让它同时盯两路（桥答复要读 payload、要想，
  分心就会答错 lane）。

分工方式：

```
你（宿主）
├── 子 agent A → 用例 1：写 YAML? → compile → run → 循环 wait/answer → 汇报 status
├── 子 agent B → 用例 2：同上
└── 子 agent C → 用例 3：同上
```

每个子 agent 的指令里必须写清这四件事，否则它一定会踩坑：

1. **只跑这一个用例**，`--run-dir` 用这个绝对路径（给出具体路径，别让它自己起名）；
2. **`--profile-dir` 用这个绝对路径**（同样给出具体的，并说明为什么不能共用）；
3. 决策者档位（`--decider agent` + `--supervisor off`，或其他档）；
4. 跑完只回三样：`status`、每步断言结果、产物目录。**不要把 trace 全文贴回来。**

第 4 条尤其重要：三路 trace 全文回传会把主上下文撑爆，而你真正需要的只是「过没过、哪步没过」。

### 别做的事

- ❌ 一个子 agent 里起两个浏览器跑两个用例 —— 省不下什么，出错时还分不清是哪一路。
- ❌ 让两个子 agent 写同一个用例的 YAML —— 后写的覆盖先写的，且没人知道该信哪个。
- ❌ 多个子 agent 共用 `--run-dir`。
- ❌ 超宿主的子 agent 上限。上限不是建议值：超了要么被拒绝，要么它们互相排队，
  表现出来的现象是「都卡住」，比串行还慢。

---

## 5. 收尾：怎么判断"全过了"

并行跑完，别只看编排器最后那行字。逐路核对：

```bash
# 每路的终态
for d in runs/*/; do
  python -c "import json,sys;d=json.load(open(sys.argv[1]+'/result.json'));print(sys.argv[1], d['status'])" "$d" 2>/dev/null \
    || python -c "import json,sys;d=json.load(open(sys.argv[1]+'/summary.json'));print(sys.argv[1], d['status'])" "$d"
done
```

- `status == "ok"` 才算过。其余 10 个状态都算没过，按 [debugging.md](debugging.md) 查。
- 有一路 `check_failed`，其他路照样是有效的——**别因为一路挂就把整批结果丢掉**。
- 失败那一路的 `screenshots/failure.png` 与 `trace.jsonl` 都在它自己的目录里，互不干扰。

汇报给用户时按用例列，而不是按时间列：哪个过了、哪个没过、没过的停在哪一步、下一步打算怎么办。

---

## 6. 常见症状对照

| 症状 | 原因 | 处置 |
|---|---|---|
| 后起的那路立刻 `profile 目录已被另一个浏览器占用` | 两路共用 profile | 每路一个 `--profile-dir` |
| 两路的 `result.json` 内容一样 / 时间戳错乱 | 共用 `--run-dir` | 每路一个 `--run-dir` |
| 子 agent 答了别的 lane 的 ask | 共用了桥目录（`UI_AGENT_BRIDGE_DIR`） | 取消那个变量，让桥跟着 `--run-dir` 走 |
| 全部卡在 `bridge wait` 不动 | 超过子 agent 上限，或策略没覆盖某个 ask | 降并发；看 `ask-*.json` 的 kind |
| 机器变很慢、浏览器起不来 | 并发太高，每个 Chromium 吃 300 MB 起 | 降到 2–4 路；CI 机器上按内存算 |
| 回放时某一路 `cache_missed` | 那一路没录过，或换了 `--url` | 先单独录一次，再并行回放 |

内存参考：每个 Chromium 实例约 300–500 MB（看页面复杂度）。8 GB 的机器并行 4 路是上限，
再多就开始换页，整体反而更慢。
