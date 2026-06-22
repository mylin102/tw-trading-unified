#!/bin/bash
# 自動更新期貨資料腳本
cd /Users/mylin/Documents/mylin102/tw-trading-unified
echo "[$(date +%Y-%m-%d %H:%M:%S)] 開始更新期貨資料..."
./venv/bin/python3 check_and_update_data.py >> logs/auto_update.log 2>&1
echo "[$(date +%Y-%m-%d %H:%M:%S)] 更新完成"

