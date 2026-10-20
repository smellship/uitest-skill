#!/usr/bin/env bash
# ui-agent 环境自检 / 一次性安装（macOS / Linux，幂等）
#
#   ./bootstrap.sh              # 检查 + 缺什么装什么
#   ./bootstrap.sh --check      # 只报告，不装
#   ./bootstrap.sh --force      # 强制重装浏览器
#
# 退出码：0 = 就绪；1 = 有问题（看输出里的 ✗）

set -u

CHECK_ONLY=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --check|-c) CHECK_ONLY=1 ;;
        --force|-f) FORCE=1 ;;
        -h|--help)
            sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "未知参数：$arg" >&2; exit 2 ;;
    esac
done

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

# ---- 定位内核：本 skill 自带一份（scripts/ 的兄弟目录 kernel/）----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL="$(cd "$SCRIPT_DIR/.." && pwd)"
ROOT="$SKILL/kernel"
if [ ! -f "$ROOT/pyproject.toml" ]; then
    echo "✗ 找不到内核：$ROOT 下没有 pyproject.toml"
    echo "  skill 包应自带 kernel/。若只有文档，说明这份拷贝不完整。"
    exit 1
fi
BROWSERS="$ROOT/.browsers"
export PLAYWRIGHT_BROWSERS_PATH="$BROWSERS"

echo "skill     = $SKILL"
echo "kernel    = $ROOT"
echo "browsers  = $BROWSERS"
echo

FAILED=()

# ---- 1. uv ----
if command -v uv >/dev/null 2>&1; then
    echo "✓ uv $(uv --version 2>&1)"
else
    echo "✗ 没有 uv。装：curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# ---- 2. Python 版本 ----
PYVER="$(uv run --project "$ROOT" python -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "?")"
case "$PYVER" in
    3.12|3.13) echo "✓ python $PYVER" ;;
    *)         echo "⚠ python $PYVER（内核按 3.12 开发；其它版本未验证）" ;;
esac

# ---- 3. 依赖 ----
if [ -d "$ROOT/.venv" ]; then
    echo "✓ .venv 已存在"
    if [ "$CHECK_ONLY" -eq 0 ]; then
        echo "  uv sync（补齐 / 校正依赖）..."
        uv sync --project "$ROOT" || FAILED+=("uv sync")
    fi
else
    echo "· .venv 不存在，需要 uv sync"
    if [ "$CHECK_ONLY" -eq 0 ]; then
        if uv sync --project "$ROOT"; then echo "✓ uv sync 完成"; else FAILED+=("uv sync"); fi
    else
        FAILED+=("uv sync（未执行）")
    fi
fi

# ---- 4. 浏览器二进制 ----
find_chrome() {
    [ -d "$BROWSERS" ] || return 1
    # Windows 的可执行文件带 .exe；macOS/Linux 不带
    find "$BROWSERS" -type f \( -name "chrome" -o -name "chrome.exe" \
        -o -name "Chromium" -o -name "headless_shell" -o -name "headless_shell.exe" \) 2>/dev/null | head -n 1
}

CHROME="$(find_chrome || true)"
if [ -n "${CHROME:-}" ] && [ "$FORCE" -eq 0 ]; then
    echo "✓ chromium 已在 $CHROME"
else
    if [ "$FORCE" -eq 1 ]; then echo "· --force：重装浏览器"; else echo "· 没找到 chromium，需要安装"; fi
    if [ "$CHECK_ONLY" -eq 0 ]; then
        echo "  playwright install chromium → $BROWSERS"
        if uv run --project "$ROOT" python -m playwright install chromium; then
            CHROME="$(find_chrome || true)"
            if [ -n "${CHROME:-}" ]; then
                echo "✓ chromium 装好了：$CHROME"
            else
                echo "✗ 装完仍找不到浏览器可执行文件，检查 PLAYWRIGHT_BROWSERS_PATH"
                FAILED+=("chromium 不可见")
            fi
        else
            FAILED+=("playwright install chromium")
        fi
    else
        FAILED+=("chromium（未安装）")
    fi
fi

# Linux 缺系统库是最常见的启动失败原因
if [ "$(uname -s)" = "Linux" ] && [ "$CHECK_ONLY" -eq 0 ] && [ ${#FAILED[@]} -eq 0 ]; then
    cat <<'EOF'
· Linux：若报 "libnss3.so: cannot open shared object file" 之类，装系统依赖：
    sudo npx playwright install-deps chromium
  （或 sudo apt-get install -y libnss3 libatk-bridge2.0-0 libgbm1 libasound2）
EOF
fi

# ---- 5. CLI 可用性 ----
if [ "$CHECK_ONLY" -eq 0 ]; then
    if HELP="$(uv run --project "$ROOT" ui-agent --help 2>&1)" && echo "$HELP" | grep -q explore; then
        echo "✓ ui-agent --help 正常（run / case / plan / snapshot / explore / report）"
    else
        echo "✗ ui-agent --help 异常："
        echo "$HELP" | head -n 10 | sed 's/^/    /'
        FAILED+=("ui-agent --help")
    fi
fi

# ---- 6. 密钥（只报告）----
ENV_FILE="$ROOT/.env"
if [ -f "$ENV_FILE" ]; then
    if grep -Eq '^[[:space:]]*TYPESAFE_API_KEY[[:space:]]*=[[:space:]]*[^[:space:]]' "$ENV_FILE"; then JEV="已配"; else JEV="未配"; fi
    if grep -Eq '^[[:space:]]*TEXT_MODEL_API_KEY[[:space:]]*=[[:space:]]*[^[:space:]]' "$ENV_FILE"; then TXT="已配"; else TXT="未配"; fi
    echo "· .env 存在：TYPESAFE_API_KEY $JEV，TEXT_MODEL_API_KEY $TXT"
else
    echo "· 没有 .env（只在跑 Jev / HTTP 文本模型时需要；agent 模式与零 token 回放不需要）"
fi

echo
if [ ${#FAILED[@]} -gt 0 ]; then
    echo "✗ 未就绪："
    for f in "${FAILED[@]}"; do echo "    - $f"; done
    echo
    echo "装好后重跑本脚本：./scripts/bootstrap.sh"
    exit 1
fi

cat <<EOF
✓ 环境就绪

下一步：
  1. 编译你自己的用例（路径随意，用例不必放在 skill 目录里）：
     uv run --project "$ROOT" ui-agent case compile --in <用例>.yaml --json
  2. 读 references/running.md 起第一次 run

样例：examples/cases/big-fish.yaml（已编译好的 plan 同名同目录）

提示：产物默认写到 $ROOT/.artifacts。若 skill 目录只读或要换位置，
      设 UI_AGENT_ARTIFACTS_DIR 指向一个可写目录。
EOF
exit 0
