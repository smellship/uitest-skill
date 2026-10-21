# 环境变量与配置

两种给法，优先级相同（**命令行 > 环境变量 > `.env` > 内置默认**）：

```bash
export UI_AGENT_SETTLE_MS=1500                      # 环境变量
```
```powershell
$env:UI_AGENT_SETTLE_MS = "1500"
```

`.env` 放在 `kernel/` 根目录（与 `pyproject.toml` 同级）。**密钥只写这里**，别写进命令行历史。

## 1. 密钥（只有这两个是敏感的）

| 变量 | 用途 | 没有它会怎样 |
|---|---|---|
| `TYPESAFE_API_KEY` | Jev 决策模型 | `--decider jev` 报「缺少 TYPESAFE_API_KEY」；`--decider agent` / `off` **不需要** |
| `TEXT_MODEL_API_KEY` | 文本模型（字段值 / HTTP 监督 / planner） | `--supervisor on` 报「缺少 TEXT_MODEL_API_KEY」；`--supervisor agent` / `off` **不需要** |

其余模型参数一般不用改：

| 变量 | 默认 | 说明 |
|---|---|---|
| `TYPESAFE_MODEL` | `jev-latest` | |
| `TYPESAFE_BASE_URL` | `https://api.typesafe.ai/v1/systemone` | |
| `TYPESAFE_AUTH_MODE` | `bearer` | `bearer`=原生；`appcode`=阿里云云市场 |
| `TEXT_MODEL_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容 |
| `TEXT_MODEL` | `deepseek-flash` | |
| `TEXT_MODEL_REASONING` | `none` | |

## 2. 路径：三个「东西放哪」

| 变量 | 默认 | 用途 |
|---|---|---|
| `UI_AGENT_ARTIFACTS_DIR` | `kernel/.artifacts` | **所有运行产物的根**：`runs/`、`cache/`、`chrome-profile/` 都在它下面。skill 目录只读时指到可写位置 |
| `UI_AGENT_CACHE_DIR` | `<artifacts>/cache` | 只改录像位置。用例库要跨机复用录像就指到用例库的 `.cache` |
| `UI_AGENT_PROFILE_DIR` | `<artifacts>/chrome-profile` | 登录态 profile。并发跑必须逐路独立，否则抢 Chromium 单实例锁 |
| `PLAYWRIGHT_BROWSERS_PATH` | `kernel/.browsers` | 浏览器二进制。bootstrap 会设好；**代码里用 `setdefault`，外部设了就听外部的** |

### 用例库场景的典型配法

```bash
export UI_AGENT_ARTIFACTS_DIR="<用例库>/<场景>/outputs"
export UI_AGENT_CACHE_DIR="<用例库>/.cache"
```

这样 `--run-dir <用例库>/<场景>/outputs/<用例>/<时间戳>` 与录像都落在受管位置。
详见 [cases-repo.md](cases-repo.md)。

## 3. 决策与监督：谁在拿主意

| 变量 | 默认 | 取值 |
|---|---|---|
| `UI_AGENT_DECIDER` | `jev` | `jev` / `llm` / `agent` / `off` |
| `UI_AGENT_SUPERVISOR` | `true` | 大模型**总闸**。`false` = 监督缝停用 + 升级点直接停 + `TYPE_TEXT` 只认 `--var` |
| `UI_AGENT_SUPERVISOR_MODE` | `model` | `model`=HTTP 文本模型；`agent`=文件桥 |
| `UI_AGENT_BRIDGE_DIR` | 空（`<run-dir>/bridge`） | 桥目录。一般不用设 |
| `UI_AGENT_BRIDGE_TIMEOUT_S` | **180** | 单次 ask 的内核侧 deadline（秒） |
| `UI_AGENT_CONF_MIN` | `0.55` | 置信度下限。连续 3 次低于它 = `low_confidence` |
| `SUPERVISOR_PER_STEP` | `2` | 每步最多问几次监督 |
| `SUPERVISOR_PER_RUN` | `6` | 整轮最多问几次监督 |

**总闸优先**：`UI_AGENT_SUPERVISOR=false` 时 `UI_AGENT_SUPERVISOR_MODE` 被忽略。

## 4. 缓存与回放

| 变量 | 默认 | 说明 |
|---|---|---|
| `UI_AGENT_CACHE` | `true` | `false` = 本次运行完全不碰缓存（探索用这个） |
| `UI_AGENT_CACHE_STRICT` | `false` | `true` = 命中的记录还必须状态签名一致，否则当失配交还模型 |

缓存文件格式版本 `CACHE_POLICY_VERSION = "v3"`，不要手改录像文件。

## 5. 产物与浏览器

| 变量 | 默认 | 说明 |
|---|---|---|
| `UI_AGENT_MODE` | `debug` | `debug`=逐步截图；`ci`=只留失败那一张。**与有无头解绑** |
| `UI_AGENT_HEADLESS` | 未设 | 未设时按 mode 推（`debug` 有头、`ci` 无头）。服务器固定 `true` |
| `UI_AGENT_VIEWPORT` | `1920x1080` | 可视化与无头必须一致，否则录像判 `wrong-context` |
| `UI_AGENT_MASK_VARS` | `false` | `true` = 非 secret 的 `--var` 值在产物里也掩码 |

## 6. 浏览器开销（回放提速就调这五个）

| 变量 | CLI 开关 | 默认 | 作用 |
|---|---|---|---|
| `SETTLE_MS` | `--settle-ms` | `700` | 每个动作后等多久。**回放提速首选** |
| `SLOW_MO` | `--slow-mo` | `250` | 有头时每步操作间隔 |
| `READY_STABLE_MS` | `--ready-stable-ms` | `2500` | 首帧就绪门：指纹安静这么久才算渲染完 |
| `READY_TIMEOUT_MS` | `--ready-timeout-ms` | `20000` | 就绪门最长等待；超时按当前快照继续，不阻塞 |
| `UI_AGENT_ASSERT_TIMEOUT_MS` | `--assert-timeout-ms` | `5000` | `DONE` 验收轮询窗口。单条断言可用 `timeout_ms` 加宽 |

调 `SETTLE_MS` 会改变动作后的稳定时机，**可能让既有录像判 `wrong-context`**。要提速就先重录。

## 7. 预算与规模

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAX_ACTIONS` | `60` | 单个目标的动作预算。用完 = `budget` |
| `MAX_DECISIONS` | `120` | 决策预算 |
| `MAX_ELEMENTS` | `250` | 进模型的元素上限。**页面越大越贵**，大页面调小 |
| `MAX_TEXT` | `6000` | 进模型的正文上限（字符） |

`MAX_ELEMENTS` / `MAX_TEXT` 是**成本旋钮**：它们直接决定每次决策往模型里塞多少字。
页面元素超过 `max_elements` 时快照会截断，`omitted` 反映截了多少，`element_count` 类断言会附带「（快照截断，实际可能更多）」。

这两个没有 CLI 开关，只能用环境变量或 `.env`。

## 8. M3 相关

| 变量 | CLI 开关 | 默认 | 说明 |
|---|---|---|---|
| `UI_AGENT_UPLOAD_MODE` | `--upload-mode` | `input` | `input`=给隐藏 input 注入文件；`drop`=页内合成拖拽事件 |
| `UI_AGENT_DIALOG_POLICY` | `--dialog-policy` | `dismiss` | `dismiss`=关掉；`accept`=点确定（`prompt` 回填页面给的默认值）|
| `UI_AGENT_BLANK_RETRIES` | — | `3` | 快照里一个元素都没有时最多刷新几次。`0`=不刷新 |

两条上传通道**不做静默回退**：设了 `drop` 就不会偷偷改用 `input`。详见 [m3.md](m3.md)。

## 9. 只给 MCP 壳用的

| 变量 | 说明 |
|---|---|
| `UI_AGENT_MCP_KERNEL_CMD` | 覆盖 MCP server 起内核的命令（默认 `python -m ui_agent.entry`）|

## 10. 常用组合

```bash
# 零 token 回放（最快）
UI_AGENT_MODE=ci UI_AGENT_SETTLE_MS=300 uv run ui-agent run ... --decider off --supervisor off

# 录制（要模型决策）
uv run ui-agent run ... --decider agent --supervisor off --relearn --mode debug

# 探索（不碰录像）
uv run ui-agent explore ... --decider agent --supervisor off --mode debug

# 大页面省钱
MAX_ELEMENTS=60 MAX_TEXT=2000 uv run ui-agent explore ...
```

## 不确定某个值是什么

```bash
uv run python -c "from ui_agent.config import Settings; s=Settings(); print(s.ui_agent_bridge_timeout_s, s.settle_ms, s.max_elements)"
```

在 `kernel/` 目录下跑，会读到同一个 `.env` 与环境变量。
