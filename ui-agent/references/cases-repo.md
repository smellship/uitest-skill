# cases-repo：用例放在你自己的 git 仓库里

**本 skill 包不含任何用例。** 用例是一个独立的 git 仓库，由用户给地址、分支（可选给本地路径）。
理由：用例跟着被测系统走，改得比内核勤；混在一起会让 skill 每次升级都撞上一堆用例改动。

## 用户必须给你三件事

| 项 | 例子 | 缺了怎么办 |
|---|---|---|
| 仓库地址 | `https://git.example.com/qa/ui-cases.git` | 问用户。本地目录也行（`file:///` 或直接路径） |
| 分支 | `main` / `release/2.3` | 默认 `main`，但**问一句**再 clone |
| 本地路径 | `D:\work\ui-cases` 或 `~/work/ui-cases` | 不给就 `<你的工作目录>/ui-cases` |

拿到后：

```bash
git clone -b <分支> <地址> <本地路径>
cd <本地路径> && git status          # 确认拿到的是干净工作树
```

已经 clone 过就 `git -C <本地路径> pull --ff-only`，不要 `reset --hard`——库里可能有别人刚提的用例。

## 目录结构（照这个建，别自己发明）

```
<用例库>/
  README.md                     # 场景索引 + 入口地址 + 要网络吗
  .gitignore
  <场景>/
    <用例>.yaml                 # 唯一真源
    others/
      <用例>.plan.json          # case compile 产物
      *.txt                     # 探索用 goal / 素材
    outputs/                    # 该场景所有运行产物，默认不进库
      <用例>/
        <时间戳>/
          trace.jsonl  actions.json  result.json  summary.json
          report.html  snapshots/  screenshots/  steps/
```

四条规矩：

1. **一场景一目录**，用例 YAML 直接放场景目录下，**不得再分层**（`<场景>/<子类>/<用例>.yaml` 不行）。
2. `others/` 只放派生物与素材。派生文件随时可由 YAML 重建，**不手改**。
3. `outputs/` 是**唯一**输出位置：单用例跑、计划跑、探索产物、HTML 报告全在它下面。
   内核默认往 `kernel/.artifacts/` 写；进用例库必须显式给 `--run-dir`（见下）。
4. **移动 / 重命名 YAML 安全**（缓存键不认文件名与 title）。改 `id:` 才换身份——`id:` 不变则录像继续可用，改 `id:` 必须重录。

## outputs：每次运行一个时间戳目录

```bash
# bash / Git Bash
STAMP=$(date +%Y%m%d-%H%M%S)
uv run ui-agent run --plan-file <场景>/others/<用例>.plan.json \
  --url <入口> --decider off --supervisor off \
  --run-dir <场景>/outputs/<用例>/$STAMP --json
```

```powershell
# PowerShell
$STAMP = Get-Date -Format "yyyyMMdd-HHmmss"
uv run ui-agent run --plan-file <场景>\others\<用例>.plan.json `
  --url <入口> --decider off --supervisor off `
  --run-dir <场景>\outputs\<用例>\$STAMP --json
```

报告就在同一个目录里（`run` 结束自动写 `report.html`）；要重出：`uv run ui-agent report --run-dir <那个目录>`。

探索也一样落到 `outputs/`：

```bash
uv run ui-agent explore --url <入口> --goal "<一句话目标>" \
  --decider agent --run-dir <场景>/outputs/explore-$STAMP --json
```

产物在 `<run-dir>/explore/`（`goal.txt` / `trajectory.md` / `explored.yaml` / `explore.json`）。

### `.gitignore`（用例库根目录）

```gitignore
# 运行产物：可随时重跑，不进库
**/outputs/
# 录像：体积大、和本机浏览器版本相关
.cache/
# 临时
*.log
```

想让某次报告留档就 `git add -f <场景>/outputs/<用例>/<时间戳>/report.html`——但**默认别把 outputs 提交**：
里面有截图（几百 KB 起）、有页面快照（可能含用户数据）。

## 录像也放用例库

内核默认把录像写在 `kernel/.artifacts/cache/`，换机器就没了。用例库自带录像才叫「克隆下来零 token 复跑」：

```bash
export UI_AGENT_CACHE_DIR="<用例库>/.cache"        # bash
$env:UI_AGENT_CACHE_DIR = "<用例库>\.cache"        # PowerShell
```

同一个场景一套录像，别混（不同站点的录像混在一个目录不会错乱，但淘汰日志会互相干扰）。
录像**必须入库**才能跨机复用；它和本机 Chromium 版本相关，升级浏览器后可能 `wrong-context`，那时 `--relearn` 重录。

## 提交节奏

```bash
git -C <用例库> add <场景>/<用例>.yaml <场景>/others/<用例>.plan.json
git -C <用例库> commit -m "case: <场景>/<用例> — <改了什么>"
git -C <用例库> push
```

- 只提交 YAML + plan.json + 录像；`outputs/` 默认不提交。
- 一次提交一个用例的改动，别把「新增用例」和「改断言」混在一个 commit 里。
- **不主动 push**，除非用户要求。推之前 `git -C <用例库> pull --rebase`。

## 换成单目标模式：不用建用例文件

还没到「值得固化成用例」的程度时，直接给 goal：

```bash
uv run ui-agent run --goal "在「账号」和「密码」里填入给定值，然后点击「确 定」" \
  --check "element_exists=查询" \
  --url <入口> --decider agent --supervisor off \
  --var 账号=demo --var 密码=demo --run-dir <场景>/outputs/probe-1 --json
```

`--check` 可重复，写法是 `类型=值`（与 YAML 里的 `类型: 值` 不同）。
单目标的缓存键是 `goal + checks` 的内容哈希——**改一个字就换键**，所以它适合探路，不适合长期维护。
探通了再固化成 YAML（`id:` 一写，录像就跟着 id 走，改措辞不丢）。

## 库在哪、没有库怎么办

1. 用户给了地址 → clone。
2. 用户只给了个本地目录（已存在 YAML）→ 直接用，别 clone。
3. 用户什么都没给 → **问一句**「用例放哪个仓库/目录？」。别自己造一个塞进 skill 包目录。
4. 用户说「随便」→ 在 `<工作目录>/ui-cases` 建库并 `git init`，把结构树和 `.gitignore` 先放进去，
   然后告诉用户「库建在 X，要接到远端就 `git remote add origin <地址>`」。
