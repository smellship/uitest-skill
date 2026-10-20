# ui-agent 环境自检 / 一次性安装（Windows PowerShell，幂等）
#
#   .\skills\ui-agent\scripts\bootstrap.ps1            # 检查 + 缺什么装什么
#   .\skills\ui-agent\scripts\bootstrap.ps1 -CheckOnly # 只报告，不装
#   .\skills\ui-agent\scripts\bootstrap.ps1 -Force     # 强制重装浏览器
#
# 退出码：0 = 就绪；1 = 有问题（看输出里的 ✗）

param(
    [switch]$CheckOnly,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$utf8 = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# ---- 定位 kernel 根（脚本在 <kernel>/skills/ui-agent/scripts/）----
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
if (-not (Test-Path (Join-Path $root "pyproject.toml"))) {
    Write-Host "✗ 找不到 kernel 根：$root 下没有 pyproject.toml" -ForegroundColor Red
    exit 1
}
$browsers = Join-Path $root ".browsers"
$env:PLAYWRIGHT_BROWSERS_PATH = $browsers

Write-Host "kernel    = $root"
Write-Host "browsers  = $browsers"
Write-Host ""

$failed = @()

# ---- 1. uv ----
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) {
    $uvVersion = (& uv --version) -join ""
    Write-Host "✓ uv $uvVersion" -ForegroundColor Green
} else {
    Write-Host "✗ 没有 uv。装：winget install --id=astral-sh.uv -e" -ForegroundColor Red
    Write-Host "  或看 https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
}

# ---- 2. Python 版本 ----
$pyVersion = (& uv run --project $root python -c "import sys; print('%d.%d' % sys.version_info[:2])") -join ""
if ($pyVersion.Trim() -eq "3.12" -or $pyVersion.Trim() -eq "3.13") {
    Write-Host "✓ python $($pyVersion.Trim())" -ForegroundColor Green
} else {
    Write-Host "⚠ python $($pyVersion.Trim())（内核按 3.12 开发；其它版本未验证）" -ForegroundColor Yellow
}

# ---- 3. 依赖 ----
$venv = Join-Path $root ".venv"
if (Test-Path $venv) {
    Write-Host "✓ .venv 已存在" -ForegroundColor Green
    if (-not $CheckOnly) {
        Write-Host "  uv sync（补齐 / 校正依赖）..."
        & uv sync --project $root
        if ($LASTEXITCODE -ne 0) { $failed += "uv sync" }
    }
} else {
    Write-Host "· .venv 不存在，需要 uv sync"
    if (-not $CheckOnly) {
        & uv sync --project $root
        if ($LASTEXITCODE -ne 0) { $failed += "uv sync" } else {
            Write-Host "✓ uv sync 完成" -ForegroundColor Green
        }
    } else {
        $failed += "uv sync（未执行）"
    }
}

# ---- 4. 浏览器二进制 ----
function Get-ChromiumExe {
    if (-not (Test-Path $browsers)) { return $null }
    Get-ChildItem -Path $browsers -Filter "chrome.exe" -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
}

$chrome = Get-ChromiumExe
if ($chrome -and -not $Force) {
    $chromiumDir = Split-Path (Split-Path $chrome -Parent) -Parent
    $sizeMb = [math]::Round(((Get-ChildItem $chromiumDir -Recurse -File -ErrorAction SilentlyContinue |
                Measure-Object Length -Sum).Sum) / 1MB)
    Write-Host "✓ chromium 已在 $chrome（约 $sizeMb MB）" -ForegroundColor Green
} else {
    if ($Force) { Write-Host "· -Force：重装浏览器" }
    else { Write-Host "· 没找到 chromium，需要安装" }
    if (-not $CheckOnly) {
        Write-Host "  playwright install chromium → $browsers"
        & uv run --project $root python -m playwright install chromium
        if ($LASTEXITCODE -ne 0) {
            $failed += "playwright install chromium"
        } else {
            $chrome = Get-ChromiumExe
            if ($chrome) {
                Write-Host "✓ chromium 装好了：$chrome" -ForegroundColor Green
            } else {
                Write-Host "✗ 装完仍找不到 chrome.exe，检查 PLAYWRIGHT_BROWSERS_PATH" -ForegroundColor Red
                $failed += "chromium 不可见"
            }
        }
    } else {
        $failed += "chromium（未安装）"
    }
}

# ---- 5. CLI 可用性 ----
if (-not $CheckOnly) {
    $help = & uv run --project $root ui-agent --help 2>&1
    if ($LASTEXITCODE -eq 0 -and ($help -join "`n") -match "explore") {
        Write-Host "✓ ui-agent --help 正常（run / case / plan / snapshot / explore / report）" -ForegroundColor Green
    } else {
        Write-Host "✗ ui-agent --help 异常：" -ForegroundColor Red
        $help | Select-Object -First 10 | ForEach-Object { Write-Host "    $_" }
        $failed += "ui-agent --help"
    }
}

# ---- 6. 密钥（只报告，不校验有效性）----
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    $txt = Get-Content $envFile -Raw -Encoding utf8
    $hasJev = $txt -match "(?m)^\s*TYPESAFE_API_KEY\s*=\s*\S"
    $hasText = $txt -match "(?m)^\s*TEXT_MODEL_API_KEY\s*=\s*\S"
    Write-Host ("· .env 存在：TYPESAFE_API_KEY {0}，TEXT_MODEL_API_KEY {1}" -f `
        $(if ($hasJev) { "已配" } else { "未配" }), $(if ($hasText) { "已配" } else { "未配" }))
} else {
    Write-Host "· 没有 .env（只在跑 Jev / HTTP 文本模型时需要；agent 模式与零 token 回放不需要）"
}

Write-Host ""
if ($failed.Count -gt 0) {
    Write-Host "✗ 未就绪：" -ForegroundColor Red
    $failed | ForEach-Object { Write-Host "    - $_" }
    Write-Host ""
    Write-Host "装好后重跑本脚本：.\skills\ui-agent\scripts\bootstrap.ps1"
    exit 1
}

Write-Host "✓ 环境就绪" -ForegroundColor Green
Write-Host ""
Write-Host "下一步："
Write-Host "  1. 换成你要跑的用例：uv run ui-agent case compile --in cases\<场景>\<用例>.yaml --json"
Write-Host "  2. 读 skills\ui-agent\references\running.md 起第一次 run"
Write-Host ""
Write-Host "只想验证浏览器能起来（不联网、不花 token）："
Write-Host "  uv run python scripts\smoke_browser.py"
exit 0
