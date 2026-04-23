#!/bin/bash
# 僅停止交易引擎，不影響 Dashboard
ps aux | grep -E "main.py|stock_runner.py" | grep -v grep | awk '{print $2}' | xargs kill -9 2>/dev/null || true
echo "✅ 交易引擎已清理 (不影響儀表板)"
