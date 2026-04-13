#!/bin/bash
# 緊急停止交易系統腳本
# 使用方式: ./emergency_stop.sh

echo "🛑 緊急停止交易系統..."
echo "時間: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# 停止所有交易相關進程
echo "停止交易系統..."
pkill -f "python3 main.py" 2>/dev/null
sleep 1

echo "停止監控儀表板..."
pkill -f "streamlit" 2>/dev/null
sleep 1

# 檢查是否還有殘留進程
echo "檢查殘留進程..."
ps aux | grep -E "python3.*main.py|streamlit.*dashboard" | grep -v grep

if [ $? -eq 0 ]; then
    echo "⚠️ 發現殘留進程，強制停止..."
    pkill -9 -f "python3 main.py" 2>/dev/null
    pkill -9 -f "streamlit" 2>/dev/null
fi

echo ""
echo "✅ 系統已完全停止"
echo "📝 日誌位置: logs/"
echo "🚀 重啟指令: ./quick_start.sh"
