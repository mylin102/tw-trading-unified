#!/bin/bash
# 策略監控腳本

echo "🕒 $(date)"
echo "📊 策略監控報告"
echo "================"

# 檢查期貨策略
echo "1. 期貨策略狀態:"
grep -A1 "score=" logs/pm2-trading-combined-*.log 2>/dev/null | tail -3

# 檢查股票策略
echo ""
echo "2. 股票策略狀態:"
python3 -c "
import yaml
try:
    with open('config/stocks.yaml') as f:
        config = yaml.safe_load(f)
        score = config.get('stocks', {}).get('entry_score', '未知')
        print(f'當前 entry_score: {score}')
except:
    print('無法讀取配置')
" 2>/dev/null

# 檢查交易記錄
echo ""
echo "3. 今日交易統計:"
echo "期貨: $(find exports/trades/TMF_$(date +%Y%m%d)*.json -type f 2>/dev/null | xargs cat 2>/dev/null | grep -c 'action' || echo 0) 筆"
echo "股票: $(tail -n +2 exports/trades/STOCK_$(date +%Y%m%d)*.csv 2>/dev/null | wc -l || echo 0) 筆"

# 檢查數據品質
echo ""
echo "4. 數據品質:"
python3 -c "
import pandas as pd
try:
    df = pd.read_csv('data/tmf_full_2026.csv')
    latest = pd.to_datetime(df['timestamp']).max()
    from datetime import datetime
    now = datetime.now()
    diff = (now - latest).total_seconds() / 60
    print(f'期貨數據最新: {latest}')
    print(f'數據延遲: {diff:.1f} 分鐘')
except Exception as e:
    print(f'數據檢查失敗: {e}')
" 2>/dev/null

echo ""
echo "✅ 監控完成"
