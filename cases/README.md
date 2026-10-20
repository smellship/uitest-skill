# cases

## 结构

```
cases/
  <场景>/
    <用例>.yaml            # 唯一真源：goal + checks
    others/
      <用例>.plan.json     # case compile 产物，run --plan-file 的输入
      *.txt                # 探索 goal / 素材
```

规则：
- 一场景一目录；用例 YAML 直接放场景目录下，不得再分层。
- `others/` 只放派生物与素材；派生文件可随时由 YAML 重建，不手改。
- **移动/重命名 YAML 文件安全**（缓存键不认文件名与 title）。改 `id:` 才换身份：`id:` 不变则录像继续可用，改 `id:` 必须重录。

## 索引

| 场景 | 用例 | 入口 |
|---|---|---|
| 大肥鱼 | bing-search-paging.yaml | https://cn.bing.com/ |
| 百望票据 | baiwang-bill-query.yaml | cs-electronic-bills-ng-http.default.hw-bw-prd-1.prd.baiwang.com |

## 流程

1. 写/改 YAML。
2. `uv run ui-agent case compile --in <yaml> --json` → 必须 `ok=true`、`errors=[]`。
3. 取 `plan` 写入 `others/<用例>.plan.json`（并补 `url` 字段）。
4. 首次（录制，耗 token）：`run --plan-file <plan> --decider jev`。
5. 之后（零 token）：`run --plan-file <plan> --decider off --supervisor off`。
6. 改过断言/步骤 → 加 `--relearn` 重录，否则回放会按旧断言判定。

## 断言规则

- 只断言**随步骤变化**的字段。不钉会漂移的具体值。
  - ✅ `url_contains: first=`（翻页后必然出现）
  - ❌ `url_contains: first=10`（实测出现过 10/11/20）
- 不断言**可能不存在**的元素。分页区「上一页」时有时无 → 不能当验收条件。
- 最后一步必须含 `RESULT_KINDS` 之一（`text_contains` / `element_value` / `element_absent` /
  `element_count` / `element_attribute`），否则触发 `weak_final_check` 警告。
- 断言写 YAML 的 `类型: 值`，不是 CLI 的 `类型=值`。

## 运行参数

| 参数 | 场景 | 值 |
|---|---|---|
| `MAX_ELEMENTS` | 元素 >100 的页面（如必应首页 214 个） | `60` |
| `MAX_TEXT` | 同上 | `2000` |
| `--settle-ms` | 点后 URL 慢变（如必应翻页） | `1500` |

不调 `MAX_*` 的后果：单次决策约 5 万 input token，且触发 `max_tokens_exceeded` 直接报错。

## --var

键名必须是**控件标签的子串**，否则该字段永远取不到值（Jev 会每轮选对目标却一直失败）。

- ✅ `--var 输入搜索词=大肥鱼`（控件标签「输入搜索词」）
- ❌ `--var sa=大肥鱼`

不确定标签时：先 `snapshot` 看元素表，或改用 `--supervisor agent` 让宿主模型填值。

## 已知不可行

- **百度全站**：本机 IP 直接返滑块验证（`wappass.baidu.com/static/captcha`），UI 与直连 URL 均被拦。
  需要搜索/翻页类用例时改用必应。
