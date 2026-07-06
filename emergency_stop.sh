#!/bin/bash
echo "🛑 [GSD] 核彈級清理交易系統..."
date

# 1. 殺掉所有 Python 與 Streamlit
ps aux | grep -E "main.py|dashboard.py|streamlit" | grep -v grep | awk '{print $2}' | xargs kill -9 2>/dev/null || true

# 2. 清理線程與緩存
find . -name "*.pyc" -delete
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# 3. 清理臨時 Flag
[ -f ".restart" ] && rm ".restart"

# 4. 檢查端口
lsof -ti:8500 | xargs kill -9 2>/dev/null || true

echo "✅ 環境已完全淨空，無殭屍進程。"
