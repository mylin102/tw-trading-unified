#!/bin/bash
# 快速啟動交易系統腳本
# 使用方式: ./quick_start.sh

echo "🚀 啟動台灣交易系統..."
echo "時間: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# 停止現有進程
echo "停止現有進程..."
pkill -f "python3 main.py" 2>/dev/null
pkill -f "streamlit" 2>/dev/null
sleep 2

# 清理日誌
echo "清理日誌..."
rm -f logs/trading.log logs/shioaji.log 2>/dev/null
mkdir -p logs

# 啟動交易系統
echo "啟動交易系統..."
cd "$(dirname "$0")"
# 2026-06-23 Gemini CLI: limit CPU usage of main.py to 50%
nohup ./scripts/python3-cpulimit.sh main.py > logs/startup.log 2>&1 &
TRADING_PID=$!
echo "交易系統PID: $TRADING_PID"

# 等待5秒
echo "等待系統初始化..."
sleep 5

# 啟動儀表板
echo "啟動監控儀表板..."
# 2026-06-23 Gemini CLI: limit CPU usage of streamlit dashboard to 50%
nohup ./scripts/python3-cpulimit.sh -m streamlit run ui/dashboard.py --server.port 8500 > logs/dashboard.log 2>&1 &
DASHBOARD_PID=$!
echo "儀表板PID: $DASHBOARD_PID"

echo ""
echo "✅ 系統啟動完成!"
echo "📊 儀表板: http://localhost:8500"
echo "📝 日誌監控: tail -f logs/trading.log"
echo "🔄 重啟指令: ./quick_start.sh"
echo "🛑 停止指令: pkill -f 'python3 main.py' && pkill -f 'streamlit'"
