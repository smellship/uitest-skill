# cases：用例 YAML 的写法与编译口径

YAML 是唯一真源。`case compile` 只信 YAML 本身，不信任何模型；报错带行号。

## 最小可用形状

```yaml
id: bing-search-paging          # 缓存身份，定了别改
title: 必应搜索并翻页
vars:
  输入搜索词: 大肥鱼              # 键名必须是控件标签的子串
steps:
  - goal: 在搜索框输入大肥鱼并回车，等待结果出现
    checks:
      - text_contains: 大肥鱼
      - element_exists: 搜索结果
  - goal: 点击「下一页」，停在第二页
    checks:
      - url_contains: first=
      - element_exists: 下一页
```

字段全集：`id` / `title`（或 `name`）/ `notes` / `url` / `vars` / `steps[].{goal,checks,url,note}`。
**没有别的字段**——`priority`、`owner`、`env` 这些平台表头不在这里。

**步骤级 `url` 可写站内路径**：以 `/` 开头时按计划入口（`--url` / 顶层 `url`）的 origin 补全。

```yaml
- goal: 回到发票查验页
  url: /bw-main-ui/incomeAlone-ui/income/invoiceCheck     # 换环境只改 --url
```

这个能力是给「登录后不回跳」的站内应用用的：登录成功后停在工作台，菜单项又不是可点角色，
只能用显式导航把页面拉回目标页。写死域名的用例换个环境就废了，所以写站内路径。
解析逻辑在 `run/plan_runner.py` 的 `resolve_step_url()`；`//` 开头（协议相对）与绝对地址原样透传。

## 五条硬规则

1. **`id` 是缓存身份。** 有 `id` 时缓存键 = `id:<id>`；没有才退回 `plan_case_hash`（对内容哈希，改一个字的 goal 就换键）。
   所以：**写了 `id`，改 goal 措辞不会让录像失效**；改 `id` 则必须重录。
   移动或重命名 YAML 文件**安全**（缓存键不认文件名与 title）。
2. **步骤数上限 12**（超过给 `too_many_steps` 警告），**每步断言最多 3 条**（`too_many_checks` 警告）。
3. **每步必须有 `checks`**，空数组直接 `checks_empty` 错误。没有断言 = 未验收。
4. **最后一步必须含一条「结果类」断言**，否则 `weak_final_check` 警告：
   `text_contains` / `element_value` / `element_absent` / `element_count` / `element_attribute`。
   （`url_contains` / `title_contains` / `element_exists` 只证明「到了某个界面」，不证明结果对。）
5. **`${{secret.X}}` / `${{env.X}}` 只能写在 `vars` 槽位**，写进 `goal` 是 `ref_in_goal` 错误。

## 8 种断言

| kind | 单键写法 | 语义 | 证据来自 |
|---|---|---|---|
| `text_contains` | `text_contains: 大肥鱼` | 页面正文含该串 | `state.page.text` |
| `url_contains` | `url_contains: first=` | 当前 URL 含该串 | `state.page.url` |
| `title_contains` | `title_contains: 搜索结果` | 页面标题含该串 | `state.page.title` |
| `element_exists` | `element_exists: 下一页` | 有元素**标签含**该串 | 元素表 label |
| `element_absent` | `element_absent: 加载中` | 没有元素标签含该串 | 元素表 label |
| `element_value` | `element_value: 输入搜索词\|大肥鱼` | 标签含左值的控件，其 `value` **恰好等于**右值 | 元素表 value |
| `element_count` | `element_count: 模拟行\|3` 或 `element_count: 3` | 标签含左值的元素个数恰好 3；只给数字 = 不限标签 | 元素表（`omitted` 时证据会注明「快照截断」）|
| `element_attribute` | 只能 JSON | 标签含 `value` 的元素，属性 `attr` **恰好等于** `expected` | 实时读 DOM（`get_attribute`）|

`element_attribute` 的完整写法（YAML 里也是这个形状）：

```yaml
- kind: element_attribute
  value: 下载使用手册
  attr: href
  expected: /manual.pdf
```

可选修饰：`role`（限定元素 role，如 `role: button`）、`timeout_ms`（单条加宽轮询窗口）。

### 断言的唯一判据：它在步骤前是假的、步骤后是真的

达不到这条的断言比没有断言更糟——它让人以为验过了。

- ❌ `element_exists: 查询`（查询按钮点之前就在页面上）
- ❌ `element_exists: 登录`（登录成功后「退出登录」仍然匹配）
- ✅ `text_contains: 已登录：sysadmin`
- ✅ `element_exists: 票据类型`（只有结果表才有）

### 别钉会漂移的值

- ✅ `url_contains: first=`（翻页后必然出现，具体值实测出现过 10/11/20/90）
- ❌ `url_contains: first=10`
- 分页区「上一页」时有时无 → 不能当验收条件。

## vars 规范

`vars` 只放**要填进控件的值**（或上传文件路径），键名必须是控件标签的**子串**：

- ✅ `输入搜索词: 大肥鱼`（控件标签就是「输入搜索词」）
- ❌ `sa: 大肥鱼`（任何标签都不含 `sa`，该字段永远取不到值；Jev 会每轮选对目标却一直失败）

`match_var` 取**最长匹配键**，所以「账号」不会抢「确认账号」。

敏感值用引用，别写字面量（字面量给 `literal_secret` 警告，密码类键名一律触发）：

```yaml
vars:
  账号: ${{secret.账号}}
  密码: ${{secret.密码}}
```

`case compile --secret-keys 账号,密码` 会校验引用是否登记过；不给 `--secret-keys` 则跳过该项校验。

## 编译

```bash
uv run ui-agent case compile --in <你的用例>.yaml --json
# 想顺手把计划落成能直接跑的文件，加 --out
uv run ui-agent case compile --in <你的用例>.yaml --out <计划>.json --json
```

`--json` 输出 `{ok, errors[], warnings[], plan}`（给了 `--out` 且编译通过时多一个 `out`）。
`errors` 非空时 `plan` 为 null、退出码 **1**、`--out` 不写文件。

### 错误码

| code | 触发 | 修法 |
|---|---|---|
| `empty_file` | 内容为空 | 写点东西 |
| `yaml_error` | YAML 语法错（带行号） | 修语法 |
| `bad_root` | 顶层不是映射 | 加 `steps:` |
| `duplicate_key` | 同名键出现两次 | 删掉后写的那份 |
| `bad_name` | `id`/`title`/`name` 含 `/ \ : * ? " < > \|` | 换字符 |
| `bad_vars` / `bad_var` | `vars` 不是映射 / 值不是标量 | 改成 `键: 标量` |
| `unknown_secret` | 引用了 `--secret-keys` 未登记的键 | 补登记或改键名 |
| `steps_empty` | 没有步骤 | 加 `steps:` |
| `bad_step` | 某步不是映射 | 每步要有 `goal:` 和 `checks:` |
| `goal_empty` | 某步缺 `goal` | 补上 |
| `ref_in_goal` | `goal` 里有 `${{secret.…}}` / `${{env.…}}` | 移到 `vars` |
| `checks_empty` | 某步没有断言 | 写断言（这是最容易反复踩的一条）|
| `bad_check` | 断言既非单键、又不含 `kind` | 二选一写法 |
| `bad_kind` | 未知断言类型 | 用上表 8 种 |
| `empty_check` | 断言没给值 | 补值 |
| `plan_invalid` | 编译结果过不了 pydantic | 看 message |

### 警告码（不阻塞，但要看）

`hardcoded_url`（用例里写了 `url`，会覆盖环境地址）、`too_many_steps`、`too_many_checks`、
`literal_secret`（疑似明文敏感值）、`weak_final_check`（最后一步证明不了结果）。

目标不是「编译通过」，是 **0 error 且 0 无关 warning**。

## 从 YAML 到跑：plan.json

落 plan 只有一种正确姿势——`--out`：

```bash
uv run ui-agent case compile --in "$CASE" --out "$CASE_DIR/others/<用例>.plan.json" --json
```

它写出的文件就是 `run --plan-file` 要的计划本体。**不要用 `--json > 文件`**：
`--json` 打的是外壳 `{"ok":…,"errors":…,"warnings":…,"plan":{…}}`，直接喂 `--plan-file` 会被
`TestPlan` 拒（缺 `steps`）；PowerShell 的 `>` / `Set-Content -Encoding UTF8` 还会额外加 BOM。
`--out` 只在编译通过时才写文件（有 error 就什么都不落，免得留下半成品）。

YAML 里刻意不写入口地址（同一份用例要跑在不同环境），所以 `plan.url` 一般是空的，
**入口由 `--url` 给**；`plan.url` 只在你想把地址钉死在计划里时才填（`--url` 仍可临时覆盖）：

```json
{"case_id": "search-paging", "name": "搜索并翻页",
 "url": "", "vars": {"搜索词": "示例"}, "steps": [...]}
```

包里不带任何用例——用例在你自己的用例库（见 [cases-repo.md](cases-repo.md)）。
下面这段是唯一需要记的形状，存成 `<用例库>/<场景>/<用例>.yaml` 即可：

```yaml
id: login-query                 # 缓存身份，定了别改
title: 登录后查询
steps:
  - goal: 在「账号」和「密码」里填入给定值，然后点击「确 定」
    checks:
      - element_exists: 查询
  - goal: 点击「查询」，等结果出现
    checks:
      - text_contains: 查询完成
```

`plan.url` 为空时必须给 `--url`，否则报「计划里没有 url，请用 --url 指定入口」。
给了 `--url` 就覆盖 plan 里的地址（但换地址 = 换缓存键，旧录像不再命中）。

## 首次录制与之后回放

```bash
# 先编译，把 plan 落到同场景的 others/ 下（--out 直接写 plan 本体）
PLAN="$CASE_DIR/others/login-query.plan.json"
uv run ui-agent case compile --in "$CASE" --out "$PLAN" --json

# 首次录制（--decider agent 会停下来问你；也可用 scripts/host_loop.py 代答）
uv run ui-agent run --plan-file "$PLAN" \
  --url "$URL" \
  --decider agent --supervisor off --relearn \
  --var 账号=demo --var 密码=demo --run-dir "$OUT/rec" --json

# 零 token 回放：同一个 plan、同一个 --url、同样那组 --var 键（值可以换）
uv run ui-agent run --plan-file "$PLAN" \
  --url "$URL" \
  --decider off --supervisor off \
  --var 账号=demo --var 密码=demo --run-dir "$OUT/rep" --json
```

先用包内离线夹具把流程走通也行：`$URL` 设成 `$FIXTURE`（拼法见 [setup.md](setup.md)），
夹具页面正好是「账号 + 密码 + 确定 + 查询」的形状。

`$OUT` 必须是该场景的 `outputs/<时间戳>`（见 [cases-repo.md](cases-repo.md)）；
不给 `--run-dir` 时产物落到 `kernel/.artifacts/runs/<名字>-<时间戳>`，跟着 skill 包走，进不了用例库。

`file:///$PWD/...` 在 Git Bash 下会拼成 `file:////d/...`（四斜杠），Chromium 打不开。
Git Bash 用 `cygpath -m "$(pwd)"`，Windows 盘符形式是 `file:///D:/...`（三斜杠 + 正斜杠）。

`--var` 每次都要带同样的**键**：录像只认动作序列，不认变量值（换值照样命中）；但缺了那个键，
输入动作会被判不成立，录像被当失败牵连淘汰（`write: evicted`）。`--url` 同理——缓存键里含环境地址。

录像只在**断言全过**（`mode=self-finalized`）时才落盘。断言没过时录像保留（因为你可能只是改了断言），
但要重录才能更新内容——加 `--relearn`。

## 已知边界

- **closed shadow root** 里的控件拿不到（只覆盖 open shadow root），遇到只能放弃，不许猜 selector。
- **网络请求 / 响应不可见**：内核不挂响应监听，接口返回 500 但页面没变化时，模型和断言都看不见。
- 验证码 / 风控 / 需要人机交互的站点，靠 UI 动作过不去；换环境或用已登录 profile，别在用例里绕。

其余 M3 能力（iframe / open shadow / 遮挡 / 新标签 / 原生对话框）见 [m3.md](m3.md)。
