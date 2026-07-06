#!/bin/bash
# 啟動計時器腳本
# 使用方式: ./launch_timer.sh

echo "⏰ 交易系統啟動計時器"
echo "========================"
echo "當前時間: $(date '+%H:%M:%S')"
echo "目標啟動時間: 08:25:00"
echo "市場開盤時間: 09:00:00"
echo ""

# 計算剩餘時間
current_epoch=$(date +%s)
target_epoch=$(date -j -f "%H:%M:%S" "08:25:00" "+%s" 2>/dev/null || date -d "08:25:00" "+%s")
market_open_epoch=$(date -j -f "%H:%M:%S" "09:00:00" "+%s" 2>/dev/null || date -d "09:00:00" "+%s")

if [ $current_epoch -lt $target_epoch ]; then
    seconds_left=$((target_epoch - current_epoch))
    minutes_left=$((seconds_left / 60))
    seconds_left=$((seconds_left % 60))
    
    echo "距離系統啟動還有: ${minutes_left}分${seconds_left}秒"
    echo ""
    
    # 倒數計時
    for ((i=seconds_left; i>=0; i--)); do
        if [ $i -eq 0 ] && [ $minutes_left -gt 0 ]; then
            minutes_left=$((minutes_left - 1))
            i=60
        fi
        
        clear
        echo "⏰ 交易系統啟動計時器"
        echo "========================"
        echo "當前時間: $(date '+%H:%M:%S')"
        echo "目標啟動時間: 08:25:00"
        echo "市場開盤時間: 09:00:00"
        echo ""
        echo "距離系統啟動還有: ${minutes_left}分${i}秒"
        echo ""
        echo "準備指令:"
        echo "  cd ~/Documents/mylin102/tw-trading-unified"
        echo "  ./quick_start.sh"
        echo ""
        echo "按 Ctrl+C 提前啟動"
        
        sleep 1
    done
    
    echo ""
    echo "🚀 時間到! 現在啟動系統..."
    echo ""
    ./quick_start.sh
    
else
    echo "⚠️ 已經超過08:25，建議立即啟動系統!"
    echo ""
    read -p "是否立即啟動系統? (y/n): " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        ./quick_start.sh
    else
        echo "取消啟動。"
    fi
fi