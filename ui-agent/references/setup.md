# setup：装依赖、装浏览器、确认能跑

全部命令都在 `kernel/` 目录下执行。下面用 `uv run` 前缀，它同时管虚拟环境和依赖解析。

## 0. 一条命令搞定（推荐先跑这个）

```powershell
# Windows（在本包根目录）
.\scripts\bootstrap.ps1
```

```bash
# macOS / Linux
./scripts/bootstrap.sh
```

它做四件事并把结果逐条打出来：查 `uv` → `uv sync` → 把 chromium 装进 `kernel/.browsers`
→ 验 `ui-agent --help` 出子命令。浏览器已装就跳过（幂等），退出码 0 = 就绪。

**装的东西只落本包内**：`kernel/.venv` 与 `kernel/.browsers`（约 400 MB）。不写系统目录、不装全局。

常用变体：

| 参数 | 干什么 |
|---|---|
| `-CheckOnly` / `--check` | 只报告不安装，用来确认环境 |
| `-Force` / `--force` | 强制重装 chromium（怀疑二进制坏了时） |

**它不碰密钥**：只报告 `.env` 里 `TYPESAFE_API_KEY` / `TEXT_MODEL_API_KEY` 配没配，不校验有效性。

装完顺手验一下浏览器真能起（不联网、不花 token）：

```bash
uv run python scripts/smoke_browser.py
```

输出里有 `executable: ...\.browsers\chromium-<rev>\chrome-win64\chrome.exe` 就对了。
下面几节是 bootstrap 失败时逐段排查用。

## 1. 手动安装

```bash
# 依赖
uv sync

# 浏览器二进制：必须装到 kernel/.browsers，不要装到用户目录
uv run python -m playwright install chromium
```

**装到哪很重要。** `src/ui_agent/config.py:15` 在导入时执行：

```python
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BROWSERS_DIR))
```

`BROWSERS_DIR = PROJECT_ROOT / ".browsers"`，`PROJECT_ROOT` 是 `kernel/`。
所以内核永远去 `kernel/.browsers/chromium-<rev>/chrome-win64/chrome.exe` 找浏览器。
装到别处（例如默认的 `%LOCALAPPDATA%\ms-playwright`）内核就找不到，报
`BrowserType.launch: Executable doesn't exist at ...\.browsers\chromium-...\chrome.exe`。

装之前先把变量导到当前会话，否则 `playwright install` 会装到用户目录：

```powershell
. .\scripts\env.ps1      # 设 PLAYWRIGHT_BROWSERS_PATH / PYTHONUTF8 / PYTHONIOENCODING
uv run python -m playwright install chromium
```

```bash
# Linux / macOS
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers"
uv run python -m playwright install chromium
```

只想用已有的浏览器目录（共享一份二进制）：在启动前显式设 `PLAYWRIGHT_BROWSERS_PATH`
到那个目录。`config.py` 用的是 `setdefault`，已有的值不会被覆盖。

## 2. 密钥：只有跑真模型才需要

写到 `kernel/.env`（`.env.example` 是模板，照抄改名）。四个变量分两组：

| 变量 | 用途 | 什么时候必需 |
|---|---|---|
| `TYPESAFE_API_KEY` / `TYPESAFE_MODEL` | Jev 决策模型 | `--decider jev` |
| `TEXT_MODEL_API_KEY` / `TEXT_MODEL_BASE_URL` / `TEXT_MODEL` | HTTP 文本模型（监督 / 填值 / planner） | `--supervisor on`、`case draft`、`plan` |

`--decider agent` 和 `--supervisor agent` **不需要任何 key**：模型在宿主那边。
零 token 回放（`--decider off --supervisor off`）也不需要。

`.env` 已在 `.gitignore` 里。**不要**把 key 写进命令、YAML、issue、对话。

## 3. 自检（30 秒）

以下命令都在 `kernel/` 下执行。

```bash
# ① CLI 能用，且子命令齐全（run / case / plan / snapshot / explore / report）
uv run ui-agent --help

# ② 浏览器能起来（不联网、不调模型）
uv run python scripts/smoke_browser.py

# ③ 离线夹具快照：真 Chromium 读一个本地页面，不联网、不调模型
#    本地文件要用合法 file URL（三斜杠 + 正斜杠）。按所用 shell 拼：
#    PowerShell   "file:///" + ((Get-Location).Path -replace '\\','/') + "/examples/m3_fixture.html"
#    Git Bash     "file:///$(cygpath -m "$(pwd)")/examples/m3_fixture.html"
#    Linux/macOS  "file://$(pwd)/examples/m3_fixture.html"
uv run ui-agent snapshot --url "file:///D:/path/to/ui-agent/kernel/examples/m3_fixture.html" --out .artifacts/s.json --json
```

第 ② 条输出里有 `executable: ...\.browsers\chromium-<rev>\chrome-win64\chrome.exe`
且最后一行 `dom read: ui_agent ok` 就对了。②失败几乎一定是浏览器目录问题（回到第 1 节）。

`kernel/scripts/` 里只带了 `env.ps1` 与 `smoke_browser.py` 两个自检脚本。开发树里的另外 10 个冒烟脚本
（`smoke_snapshot` / `smoke_replay` / `smoke_bridge` / `smoke_explore` …）需要 `tests/` 与额外夹具，
skill 包默认不带——它们校验的是内核自己，不是你的用例。

## 4. 目录约定

```
ui-agent/                本 skill 包（自足，整目录拷走即可用）
  SKILL.md
  references/            8 篇文档
  scripts/               bootstrap.ps1 / bootstrap.sh
  examples/cases/        offline（离线夹具）+ big-fish（真实站点）两份样例
  kernel/                内核本体（自己的一份，独立 .venv）
    src/ui_agent/        内核源码
    examples/            本地夹具页面（local_fixture.html / m3_fixture.html）
    scripts/             env.ps1 + smoke_browser.py
    .artifacts/          运行产物（默认落这里）
      runs/<任务>-<时间戳>/  每次 run 的产物
      cache/             回放录像（跨运行持久）
      chrome-profile/    登录态（默认目录，串行复用）
    .browsers/           浏览器二进制（由 bootstrap 装）
```

**用例放哪由你定**：内核只按 `--in` / `--plan-file` 的路径找文件，不限定用例必须在包内。
`examples/cases/` 里那两份是能直接跑的样例，`offline.*` 那份指向包内夹具、不联网。

产物目录可用 `UI_AGENT_ARTIFACTS_DIR=<可写目录>` 整体挪走（skill 目录只读时必须设）。

## 5. 有头 / 无头

不设 `UI_AGENT_HEADLESS` 时按 `--mode` 推：

| mode | headless | 产物 |
|---|---|---|
| `debug`（默认） | false（看得见窗口） | 逐步截图 |
| `ci` | true | 只留失败那一张 |

强制指定：`UI_AGENT_HEADLESS=true`（服务器一律这么设）。
有头模式下 `--slow-mo`（默认 250ms）是给人看的操作间隔；无头时忽略。

## 6. 常见环境错误

| 报错 | 原因 | 修法 |
|---|---|---|
| `Executable doesn't exist at ...\.browsers\chromium-...` | 浏览器装到了用户目录 | 按第 1 节重装到 `.browsers` |
| `缺少 TYPESAFE_API_KEY` | 用了 `--decider jev` 但没配 key | 配 `.env`，或改 `--decider agent` |
| `缺少 TEXT_MODEL_API_KEY；监督模型不可用` | 用了 `--supervisor on` 但没配 key | 配 `.env`，或改 `--supervisor agent` / `off` |
| `agent 模式必须指定 --run-dir` | `--decider agent` / `--supervisor agent` 没给 `--run-dir` | 显式给一个空目录路径 |
| `桥目录不存在：...（先用 agent 模式启动一次 run）` | `bridge wait` 在 run 起来之前就跑了 | 先起 run，等 `<run-dir>/bridge/` 出现 |
| `UnicodeDecodeError` / 中文乱码 | Windows 控制台代码页 | 设 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`（`env.ps1` 已含） |

## 7. 共享同一份 profile 的限制

默认登录态目录 `.artifacts/chrome-profile` 是**固定**的，串行跑没问题。
多路并发必须用 `UI_AGENT_PROFILE_DIR` 给每一路一个独立目录，否则 Chrome 会因目录被锁而启动失败。

有头跑完留下的 Chrome 进程会锁住这个目录：

```powershell
Get-Process chrome -ErrorAction SilentlyContinue | Stop-Process -Force
```
