#!/bin/bash
# 系統啟動監控腳本
# 在crontab啟動系統後檢查狀態

echo "🔍 交易系統啟動監控腳本"
echo "=========================="
echo "啟動時間: $(date)"
echo ""

# 檢查日誌目錄
LOG_DIR="/Users/mylin/Documents/mylin102/tw-trading-unified/logs"
echo "檢查日誌目錄: $LOG_DIR"
ls -la "$LOG_DIR/" 2>/dev/null || echo "日誌目錄不存在"

echo ""
echo "📊 進程狀態檢查:"
echo "----------------"

# 檢查main.py進程
echo "1. 交易系統核心 (main.py):"
MAIN_PID=$(pgrep -f "main.py" 2>/dev/null)
if [ -n "$MAIN_PID" ]; then
    echo "   ✅ 運行中 (PID: $MAIN_PID)"
    ps -p "$MAIN_PID" -o pid,time,%cpu,%mem,command 2>/dev/null | tail -1
else
    echo "   ❌ 未運行"
fi

# 檢查stock_runner.py進程
echo ""
echo "2. 股票交易系統 (stock_runner.py):"
STOCK_PID=$(pgrep -f "stock_runner.py" 2>/dev/null)
if [ -n "$STOCK_PID" ]; then
    echo "   ✅ 運行中 (PID: $STOCK_PID)"
    ps -p "$STOCK_PID" -o pid,time,%cpu,%mem,command 2>/dev/null | tail -1
else
    echo "   ❌ 未運行"
fi

# 檢查streamlit儀表板
echo ""
echo "3. 監控儀表板 (streamlit):"
STREAMLIT_PIDS=$(pgrep -f "streamlit" 2>/dev/null)
if [ -n "$STREAMLIT_PIDS" ]; then
    echo "   ✅ 運行中 (PIDs: $STREAMLIT_PIDS)"
    echo "   端口檢查:"
    for port in 8500 8501; do
        if lsof -i :$port -sTCP:LISTEN >/dev/null 2>&1; then
            echo "     ✅ 端口 $port 監聽中"
        else
            echo "     ❌ 端口 $port 未監聽"
        fi
    done
else
    echo "   ❌ 未運行"
fi

echo ""
echo "📝 日誌文件檢查:"
echo "----------------"

# 檢查主要日誌文件
for logfile in "unified.log" "dashboard.log" "backtest_dashboard.log" "trading.log"; do
    if [ -f "$LOG_DIR/$logfile" ]; then
        size=$(ls -lh "$LOG_DIR/$logfile" | awk '{print $5}')
        lines=$(wc -l < "$LOG_DIR/$logfile" 2>/dev/null || echo "0")
        echo "  $logfile: $size, $lines 行"
        
        # 顯示最後5行
        if [ "$lines" -gt 0 ]; then
            echo "  最後5行:"
            tail -5 "$LOG_DIR/$logfile" 2>/dev/null | sed 's/^/    /'
        fi
    else
        echo "  $logfile: 不存在"
    fi
    echo ""
done

echo ""
echo "🔗 網絡連接檢查:"
echo "----------------"

# 檢查本地端口
echo "本地端口監聽狀態:"
netstat -an | grep LISTEN | grep -E "(8500|8501)" 2>/dev/null || echo "  未找到相關端口"

echo ""
echo "📈 系統資源檢查:"
echo "----------------"

# CPU和記憶體使用
echo "系統負載:"
top -l 1 -n 0 | grep "CPU usage" 2>/dev/null || echo "  無法獲取CPU使用率"

echo ""
echo "記憶體使用:"
top -l 1 -n 0 | grep "PhysMem" 2>/dev/null || echo "  無法獲取記憶體使用率"

echo ""
echo "🔄 自動啟動狀態:"
echo "----------------"

# 檢查crontab
echo "crontab設定:"
crontab -l 2>/dev/null | grep -A2 -B2 "tw-trading-unified" || echo "  未找到相關crontab設定"

echo ""
echo "📋 總結:"
echo "------"

# 總結狀態
ERRORS=0
WARNINGS=0

if [ -z "$MAIN_PID" ]; then
    echo "❌ 交易系統核心未運行"
    ERRORS=$((ERRORS + 1))
else
    echo "✅ 交易系統核心運行正常"
fi

if [ -z "$STOCK_PID" ]; then
    echo "⚠️  股票交易系統未運行 (可能非交易時間)"
    WARNINGS=$((WARNINGS + 1))
else
    echo "✅ 股票交易系統運行正常"
fi

if [ -z "$STREAMLIT_PIDS" ]; then
    echo "❌ 監控儀表板未運行"
    ERRORS=$((ERRORS + 1))
else
    echo "✅ 監控儀表板運行正常"
fi

echo ""
echo "📊 統計:"
echo "  錯誤: $ERRORS"
echo "  警告: $WARNINGS"

if [ $ERRORS -eq 0 ]; then
    echo ""
    echo "🎉 系統狀態: 🟢 正常"
    echo "儀表板網址: http://localhost:8500"
    echo "備用儀表板: http://localhost:8501"
else
    echo ""
    echo "⚠️  系統狀態: 🟡 有問題"
    echo "建議檢查日誌文件: $LOG_DIR/"
fi

echo ""
echo "檢查完成時間: $(date)"