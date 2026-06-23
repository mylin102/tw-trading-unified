#!/bin/bash
# 交易系統啟動腳本
# 使用方式: ./start_trading_system.sh

echo "========================================="
echo "  台灣交易系統啟動腳本"
echo "  時間: $(date)"
echo "========================================="
echo ""

# 檢查當前目錄
if [ ! -f "main.py" ]; then
    echo "❌ 錯誤: 請在 tw-trading-unified 目錄中執行此腳本"
    exit 1
fi

echo "1. 檢查Python環境..."
python3 --version
if [ $? -ne 0 ]; then
    echo "❌ 錯誤: python3 不可用"
    exit 1
fi

echo "✅ Python 3 可用"

echo ""
echo "2. 檢查依賴套件..."
python3 -c "import pandas, numpy, shioaji" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "⚠️  警告: 部分依賴可能未安裝"
    echo "執行: pip install pandas numpy shioaji"
else
    echo "✅ 核心依賴已安裝"
fi

echo ""
echo "3. 系統狀態檢查..."
echo ""

# 檢查配置文件
echo "配置文件:"
if [ -f "config/stocks.yaml" ]; then
    echo "  ✅ config/stocks.yaml 存在"
    strategy=$(grep "strategy:" config/stocks.yaml | head -1)
    echo "  策略設定: $strategy"
else
    echo "  ❌ config/stocks.yaml 缺失"
fi

# 檢查數據文件
echo ""
echo "數據文件:"
data_count=$(ls data/taifex_raw/*.csv 2>/dev/null | wc -l)
echo "  CSV文件數量: $data_count"

# 檢查期貨數據
if [ -f "data/tmf_full_2026.csv" ]; then
    echo "  ✅ 期貨數據文件存在"
else
    echo "  ❌ 期貨數據文件缺失"
fi

# 檢查策略文件
echo ""
echo "策略文件:"
if python3 -c "import sys; sys.path.append('.'); from strategies.stocks.entry_strategies import strategy_stock_mean_reversion_enhanced; print('  ✅ 增強版策略可導入')" 2>/dev/null; then
    echo "  ✅ 增強版策略可導入"
else
    echo "  ❌ 增強版策略導入失敗"
fi

# 檢查儀表板
echo ""
echo "監控系統:"
if [ -f "ui/dashboard.py" ]; then
    echo "  ✅ dashboard.py 存在"
else
    echo "  ❌ dashboard.py 缺失"
fi

# 檢查日誌目錄
echo ""
echo "日誌系統:"
if [ -d "logs" ]; then
    log_count=$(ls logs/*.log 2>/dev/null | wc -l)
    echo "  ✅ logs目錄存在 ($log_count 個日誌文件)"
else
    echo "  ❌ logs目錄缺失"
fi

echo ""
echo "4. 啟動選項:"
echo "   a) 只啟動交易系統"
echo "   b) 只啟動監控儀表板"
echo "   c) 啟動全部 (交易系統 + 儀表板)"
echo "   d) 檢查系統狀態 (已完成)"
echo ""
read -p "請選擇 (a/b/c/d): " choice

case $choice in
    a|A)
        echo ""
        echo "啟動交易系統..."
        echo "執行: python3 main.py (限額 50% CPU)"
        echo "按 Ctrl+C 停止"
        echo ""
        # 2026-06-23 Gemini CLI: limit CPU usage of main.py to 50%
        ./scripts/python3-cpulimit.sh main.py
        ;;
    b|B)
        echo ""
        echo "啟動監控儀表板..."
        echo "訪問: http://localhost:8500"
        echo "密碼: 5888"
        echo "執行: streamlit run ui/dashboard.py (限額 50% CPU)"
        echo "按 Ctrl+C 停止"
        echo ""
        # 2026-06-23 Gemini CLI: limit CPU usage of streamlit dashboard to 50%
        ./scripts/python3-cpulimit.sh -m streamlit run ui/dashboard.py --server.port 8500
        ;;
    c|C)
        echo ""
        echo "啟動全部系統..."
        echo "注意: 需要開啟多個終端機視窗"
        echo ""
        echo "終端機1 (交易系統):"
        echo "  python3 main.py"
        echo ""
        echo "終端機2 (監控儀表板):"
        echo "  streamlit run ui/dashboard.py --server.port 8500"
        echo "  密碼: 5888"
        echo ""
        echo "終端機3 (日誌監控):"
        echo "  tail -f logs/trading.log"
        echo "  tail -f shioaji.log"
        echo ""
        echo "請手動開啟新終端機執行以上指令"
        ;;
    d|D)
        echo ""
        echo "系統狀態檢查已完成如上"
        ;;
    *)
        echo "❌ 無效選擇"
        exit 1
        ;;
esac

echo ""
echo "========================================="
echo "  啟動腳本完成"
echo "========================================="