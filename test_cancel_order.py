
import sys
sys.path.append('.')
import json
from datetime import datetime
from core.order_management.order_manager import OrderManager
from core.order_management.order import OrderStatus

# 讀取當前委託單
try:
    with open('exports/trades/TMF_20260415_orders.json', 'r') as f:
        orders = json.load(f)
except Exception as e:
    print(f"讀取委託單文件失敗: {e}")
    sys.exit(1)

print(f"找到 {len(orders)} 筆委託單")

# 尋找需要取消的委託單
pending_orders = [o for o in orders if o['status'] == 'submitted']
print(f"排隊中的委託單: {len(pending_orders)} 筆")

for order in pending_orders:
    print(f"\n訂單ID: {order['order_id']}")
    print(f"狀態: {order['status']}")
    print(f"方向: {order['side']}")
    print(f"價格: {order['price']}")
    print(f"創建時間: {order['created_at']}")
    
    # 檢查是否為夜盤留下的委託單
    created_str = order['created_at']
    if 'T' in created_str:
        created = datetime.fromisoformat(created_str.replace('Z', '+00:00'))
    else:
        created = datetime.strptime(created_str, "%Y-%m-%d %H:%M:%S")
    
    now = datetime.now()
    time_diff = now - created
    hours = time_diff.total_seconds() / 3600
    
    print(f"已存在時間: {hours:.2f} 小時")
    
    if hours > 2:  # 超過2小時
        print(f"⚠️ 建議取消: 委託單已存在超過2小時")
        print(f"   夜盤委託單在日盤時段應自動取消")
    else:
        print(f"✓ 時間正常")

print("\n注意: 需要通過監控系統的OrderManager實例來取消委託單")
print("建議方案:")
print("1. 修改monitor.py添加session切換清理邏輯")
print("2. 添加委託單過期機制")
print("3. 重啟監控系統以清理pending orders")
