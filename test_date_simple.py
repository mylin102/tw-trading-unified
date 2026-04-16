#!/usr/bin/env python3
import sys
sys.path.append('.')
from core.date_utils import get_trade_day
import datetime

# 測試當前時間（週四凌晨）
now = datetime.datetime.now()
print(f"當前時間: {now}")
print(f"週幾: {now.weekday()} (0=週一, 3=週四)")
print(f"小時: {now.hour}")

trade_day = get_trade_day(now)
print(f"get_trade_day: {trade_day.strftime('%Y%m%d')}")

# 測試不同時間點
test_times = [
    ("2026-04-16 04:59:07", "週四凌晨"),
    ("2026-04-16 08:50:00", "週四日盤"),
    ("2026-04-16 15:30:00", "週四夜盤開始"),
    ("2026-04-17 20:23:12", "週五夜盤"),
    ("2026-04-18 02:42:31", "週六凌晨"),
]

print("\n測試不同時間點:")
for time_str, desc in test_times:
    dt = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
    td = get_trade_day(dt)
    print(f"  {time_str} ({desc}): {td.strftime('%Y%m%d')}")
