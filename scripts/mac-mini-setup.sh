#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# tw-trading-unified — Mac Mini 一鍵安裝腳本
# 適用於全新 Mac Mini (Apple Silicon) 或 git pull 後的環境
# 相依：Tailscale (兩台 Mac 皆安裝，用於穩定連線)
# 安裝後 Dashboard 可透過 http://[MacMini-Tailscale-IP]:8500 存取
# ─────────────────────────────────────────────────────────────
set -euo pipefail

# ── 顏色 ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${CYAN}[$(date +%H:%M:%S)]${NC} $*"; }
ok()   { echo -e "  ${GREEN}✅${NC} $*"; }
warn() { echo -e "  ${YELLOW}⚠️${NC} $*"; }
fail() { echo -e "  ${RED}❌${NC} $*"; exit 1; }

# ── 路徑 ──
REPO_DIR="$HOME/Documents/mylin102/tw-trading-unified"
VENV_DIR="$REPO_DIR/.venv"
PYTHON_VER="3.12"
HERMES_DIR="$HOME/.hermes"

log "🚀 tw-trading-unified 安裝腳本"
log "目標: $REPO_DIR"
echo ""

# ════════════════════════════════════════════
# Phase 1: 前置檢查
# ════════════════════════════════════════════
log "📋 Phase 1: 前置檢查"

if [[ "$(uname)" != "Darwin" ]]; then
    fail "此腳本僅支援 macOS"
fi
ok "macOS $(sw_vers -productVersion)"
ARCH=$(uname -m)
[[ "$ARCH" != "arm64" ]] && warn "非 Apple Silicon ($ARCH)，部分套件可能不相容"
ok "Architecture: $ARCH"

if ! xcode-select -p &>/dev/null; then
    log "安裝 Xcode Command Line Tools..."
    xcode-select --install
    warn "請在彈出視窗中按「安裝」，完成後重新執行此腳本"
    exit 1
fi
ok "Xcode CLI tools"

# Tailscale 檢查
if ! command -v tailscale &>/dev/null; then
    warn "Tailscale 未安裝 — 建議安裝以確保穩定連線"
    warn "下載: https://tailscale.com/download/mac"
else
    ok "Tailscale $(tailscale version 2>/dev/null | head -1)"
fi

# ════════════════════════════════════════════
# Phase 2: uv (Python 套件管理)
# ════════════════════════════════════════════
log "📦 Phase 2: 安裝 uv"

if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    # 加入 shell profile
    for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
        if [ -f "$rc" ] && ! grep -q '.local/bin' "$rc" 2>/dev/null; then
            echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$rc"
        fi
    done
    ok "uv $(uv --version)"
else
    ok "uv $(uv --version)"
fi

# ════════════════════════════════════════════
# Phase 3: Node.js + PM2
# ════════════════════════════════════════════
log "📟 Phase 3: 安裝 Node.js + PM2"

if ! command -v node &>/dev/null; then
    log "下載 Node.js 20..."
    curl -fsSL https://nodejs.org/dist/v20.18.0/node-v20.18.0-darwin-arm64.tar.gz | \
        tar xz -C /tmp
    mkdir -p "$HOME/.local/bin"
    cp /tmp/node-v20.18.0-darwin-arm64/bin/node "$HOME/.local/bin/"
    rm -rf /tmp/node-v20.18.0-darwin-arm64
    ok "Node.js v20.18.0"
else
    ok "Node.js $(node --version)"
fi

export PATH="$HOME/.local/bin:$PATH"
if ! command -v pm2 &>/dev/null; then
    npm install -g pm2
fi
ok "PM2 $(pm2 --version 2>/dev/null || echo 'installed')"

# ════════════════════════════════════════════
# Phase 4: Repository
# ════════════════════════════════════════════
log "📂 Phase 4: Repository"

mkdir -p "$REPO_DIR"

if [ -f "$REPO_DIR/pyproject.toml" ]; then
    ok "Repository 已存在"
elif [ -d "$(dirname "$REPO_DIR")" ] && [ "$(ls -A "$REPO_DIR" 2>/dev/null)" ]; then
    ok "目錄非空，假設為現有專案"
else
    warn "Repository 不存在"
    echo ""
    echo "   請選擇一種方式提供原始碼："
    echo ""
    echo "   1) git clone:"
    echo "      git clone <REPO_URL> $REPO_DIR"
    echo ""
    echo "   2) rsync 從舊 Mac:"
    echo "      rsync -avz --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' --exclude='.git/objects' --exclude='.DS_Store' \\"
    echo "        /path/to/old/tw-trading-unified/ $REPO_DIR/"
    echo ""
    exit 1
fi

cd "$REPO_DIR"

# ════════════════════════════════════════════
# Phase 5: Python venv + Dependencies
# ════════════════════════════════════════════
log "🐍 Phase 5: Python 虛擬環境"

# 安裝 Python (uv)
if ! uv python list 2>/dev/null | grep -q "$PYTHON_VER"; then
    log "下載 Python $PYTHON_VER..."
    uv python install "$PYTHON_VER"
fi

# 重建 venv
rm -rf "$VENV_DIR"
uv venv --python "$PYTHON_VER"
source "$VENV_DIR/bin/activate"
ok "Python $(python --version)"

# 安裝 dependencies
log "安裝 Python 套件..."

# 核心 trading
uv pip install shioaji pandas numpy scipy scikit-learn statsmodels \
    pandas_ta pytz rich requests pyyaml python-dotenv psutil joblib numba

# Dashboard
uv pip install streamlit plotly streamlit_autorefresh

# 開發
uv pip install pytest pytest-timeout pytest-asyncio

# 其他依賴 (如果存在舊 requirements)
REQ_FILE="/tmp/clean-requirements.txt"
if [ -f "$REQ_FILE" ]; then
    uv pip install -r "$REQ_FILE" --quiet
fi

ok "套件安裝完成"

# ════════════════════════════════════════════
# Phase 6: .env (API Keys)
# ════════════════════════════════════════════
log "🔑 Phase 6: 環境變數"

ENV_FILE="$REPO_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    ok ".env 已存在"
else
    cat > "$ENV_FILE" <<'ENVEOF'
# Shioaji API Credentials
SHIOAJI_API_KEY=your_api_key_here
SHIOAJI_SECRET_KEY=your_secret_key_here
ENVEOF
    warn "請編輯 $ENV_FILE 填入 Shioaji API Key"
fi

# ════════════════════════════════════════════
# Phase 7: Hermes
# ════════════════════════════════════════════
log "🤖 Phase 7: Hermes Agent"

mkdir -p "$HERMES_DIR"

if [ ! -f "$HERMES_DIR/config.yaml" ]; then
    warn "Hermes config 不存在。手動複製:"
    echo ""
    echo "  rsync -avz user@old-mac:~/.hermes/ $HERMES_DIR/"
    echo ""
fi

# ════════════════════════════════════════════
# Phase 8: PM2 Ecosystem + Startup
# ════════════════════════════════════════════
log "⚙️  Phase 8: PM2 Ecosystem Config"

cat > "$REPO_DIR/ecosystem.config.cjs" <<PM2EOF
module.exports = {
  apps: [
    {
      name: "trading-system",
      cwd: "${REPO_DIR}",
      script: "${REPO_DIR}/.venv/bin/python",
      args: "main.py",
      interpreter: "none",
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 5,
      min_uptime: "30s",
      kill_timeout: 15000,
      time: true,
      out_file: "logs/pm2-trading-out.log",
      error_file: "logs/pm2-trading-error.log",
      env: { PYTHONUNBUFFERED: "1" }
    },
    {
      name: "dashboard",
      cwd: "${REPO_DIR}",
      script: "${REPO_DIR}/.venv/bin/streamlit",
      args: "run ui/dashboard.py --server.port 8500 --server.headless=true",
      interpreter: "none",
      autorestart: true,
      restart_delay: 3000,
      max_restarts: 10,
      min_uptime: "20s",
      time: true,
      out_file: "logs/pm2-dashboard-out.log",
      error_file: "logs/pm2-dashboard-error.log"
    }
  ]
};
PM2EOF
ok "ecosystem.config.cjs created"

log "啟動 PM2 processes..."
pm2 start "$REPO_DIR/ecosystem.config.cjs" 2>/dev/null || true
pm2 save
ok "PM2 processes started"

# Launch Agent for auto-start on boot
LAUNCH_AGENT="$HOME/Library/LaunchAgents/pm2.$USER.plist"
cat > "$LAUNCH_AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>pm2.$USER</string>
    <key>ProgramArguments</key>
    <array>
        <string>$HOME/.local/lib/node_modules/pm2/bin/pm2</string>
        <string>resurrect</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>PM2_HOME</key>
        <string>$HOME/.pm2</string>
    </dict>
</dict>
</plist>
PLIST
launchctl load "$LAUNCH_AGENT"
ok "開機自啟已設定"

# ════════════════════════════════════════════
# Phase 9: 驗證
# ════════════════════════════════════════════
log "🔍 Phase 9: 驗證"

echo ""
echo "  PM2 processes:"
pm2 list 2>&1 | head -5
echo ""

# Dashboard
sleep 3
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8500/ 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    ok "Dashboard: HTTP 200 — http://localhost:8500"
else
    warn "Dashboard: HTTP $HTTP_CODE (可能仍在啟動中)"
fi

echo ""
echo "═══════════════════════════════════════════"
echo "  安裝完成！"
echo ""
echo "  Dashboard:  http://localhost:8500"
echo "  PM2 logs:   pm2 logs trading-system"
echo "              pm2 logs dashboard"
echo ""
echo "  週一開盤前確認:"
echo "    pm2 list → 兩個 process online"
echo "    tail -f logs/pm2-trading-out.log → tick 持續進入"
echo "═══════════════════════════════════════════"
