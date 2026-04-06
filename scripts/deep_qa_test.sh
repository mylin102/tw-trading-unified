#!/bin/bash
# scripts/deep_qa_test.sh
# 執行深度 UI 整合測試：點擊按鈕並偵測 Python 錯誤

export PATH="$HOME/.bun/bin:$PATH"
B="/Users/mylin/.kiro/skills/gstack/browse/dist/browse"

echo "🚀 Starting Deep UI Validation..."

# 1. 測試排行榜 (最容易觸發 Numba 類型錯誤)
echo "--- Testing Strategy Leaderboard ---"
$B goto http://localhost:8501
sleep 2
$B click "text=Strategy Leaderboard"
sleep 1
$B click "button:has-text('Run Comparison')"
echo "Waiting for computation (10s)..."
sleep 10

# 偵測錯誤
ERROR_COUNT=$($B text | grep -cE "Traceback|KeyError|TypeError|ValueError")
if [ $ERROR_COUNT -gt 0 ]; then
    echo "❌ FAILED: Found $ERROR_COUNT errors on Leaderboard page!"
    $B screenshot "design-audit/screenshots/error_leaderboard.png"
    exit 1
else
    echo "✅ SUCCESS: Leaderboard ran cleanly."
fi

# 2. 測試參數掃描
echo "--- Testing Parameter Sweep ---"
$B click "text=Parameter Sweep"
sleep 1
$B click "button:has-text('Run Grid Sweep')"
echo "Waiting for sweep (10s)..."
sleep 10

ERROR_COUNT=$($B text | grep -cE "Traceback|KeyError|TypeError|ValueError")
if [ $ERROR_COUNT -gt 0 ]; then
    echo "❌ FAILED: Found $ERROR_COUNT errors on Sweep page!"
    $B screenshot "design-audit/screenshots/error_sweep.png"
    exit 1
else
    echo "✅ SUCCESS: Parameter Sweep ran cleanly."
fi

# 3. 測試實戰 Dashboard (Port 8500)
echo "--- Testing Live Dashboard (8500) ---"
$B goto http://localhost:8500
sleep 5

ERROR_COUNT=$($B text | grep -cE "Traceback|KeyError|TypeError|ValueError")
if [ $ERROR_COUNT -gt 0 ]; then
    echo "❌ FAILED: Found $ERROR_COUNT errors on Live Dashboard!"
    $B screenshot "design-audit/screenshots/error_live.png"
    exit 1
else
    # 額外檢查：是否正確顯示今日指數走勢 (檢查特定的 plotly class)
    if $B html | grep -q "js-plotly-plot"; then
        echo "✅ SUCCESS: Live Dashboard and Charts are healthy."
    else
        echo "⚠️ WARNING: Live Dashboard loaded but Plotly charts not detected."
    fi
fi

echo "🏁 All UI modules (8500 & 8501) verified successfully."
