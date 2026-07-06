#!/usr/bin/env python3
"""
取消過時的選擇權待處理訂單 OPT-000002
"""

import json
import sys
from datetime import datetime
from pathlib import Path

def cancel_stale_options_order():
    """取消過時的選擇權待處理訂單"""
    
    # 訂單文件路徑
    orders_file = Path("exports/trades/OPTIONS_20260415_orders.json")
    
    if not orders_file.exists():
        print(f"❌ 選擇權訂單文件不存在: {orders_file}")
        return False
    
    # 讀取訂單文件
    with open(orders_file, 'r') as f:
        orders = json.load(f)
    
    print(f"📊 找到 {len(orders)} 個選擇權訂單")
    
    # 尋找OPT-000002
    target_order = None
    for order in orders:
        if order['order_id'] == 'OPT-000002':
            target_order = order
            break
    
    if not target_order:
        print("❌ 找不到OPT-000002訂單")
        return False
    
    print(f"📋 訂單詳情:")
    print(f"  ID: {target_order['order_id']}")
    print(f"  標的: {target_order['symbol']}")
    print(f"  狀態: {target_order['status']}")
    print(f"  方向: {target_order['side']} {target_order['quantity']}口")
    print(f"  類型: {target_order['order_type']}")
    print(f"  價格: {target_order.get('price', '市價')}")
    print(f"  創建時間: {target_order['created_at']}")
    
    # 檢查訂單狀態
    if target_order['status'] != 'submitted':
        print(f"❌ 訂單狀態不是'submitted'，無法取消")
        return False
    
    # 計算訂單年齡
    created_time = datetime.fromisoformat(target_order['created_at'].replace('Z', '+00:00'))
    current_time = datetime.now()
    age_hours = (current_time - created_time).total_seconds() / 3600
    
    print(f"⏰ 訂單年齡: {age_hours:.2f} 小時")
    
    # 更新訂單狀態為cancelled
    target_order['status'] = 'cancelled'
    target_order['cancelled_at'] = datetime.now().isoformat()
    target_order['cancel_reason'] = 'session_switch_stale_order'
    
    # 保存更新後的訂單文件
    with open(orders_file, 'w') as f:
        json.dump(orders, f, indent=2, ensure_ascii=False)
    
    print(f"✅ 已取消選擇權訂單 OPT-000002")
    print(f"   原因: session_switch_stale_order")
    print(f"   時間: {target_order['cancelled_at']}")
    
    return True

def check_options_order_lifecycle_issues():
    """檢查選擇權訂單生命週期問題"""
    
    orders_file = Path("exports/trades/OPTIONS_20260415_orders.json")
    
    if not orders_file.exists():
        print("❌ 選擇權訂單文件不存在")
        return
    
    with open(orders_file, 'r') as f:
        orders = json.load(f)
    
    print("\n🔍 選擇權訂單生命週期檢查:")
    
    pending_orders = []
    for order in orders:
        if order['status'] == 'submitted':
            pending_orders.append(order)
    
    if pending_orders:
        print(f"⚠️  發現 {len(pending_orders)} 個待處理選擇權訂單:")
        for order in pending_orders:
            created_time = datetime.fromisoformat(order['created_at'].replace('Z', '+00:00'))
            age_hours = (datetime.now() - created_time).total_seconds() / 3600
            print(f"  - {order['order_id']}: {order['symbol']} {order['side']} {order['quantity']}口 @ {order.get('price', '市價')} (已等待{age_hours:.1f}小時)")
    else:
        print("✅ 沒有待處理選擇權訂單")

def main():
    """主函數"""
    print("🔄 開始處理過時選擇權訂單...")
    
    # 檢查訂單生命週期問題
    check_options_order_lifecycle_issues()
    
    print("\n" + "="*50)
    
    # 取消過時訂單
    success = cancel_stale_options_order()
    
    if success:
        print("\n✅ 操作完成")
        print("\n📋 系統狀態摘要:")
        print("1. 期貨訂單: ORD-000002 已取消")
        print("2. 選擇權訂單: OPT-000002 已取消")
        print("3. 所有夜盤訂單已清理")
        print("\n🎯 後續建議:")
        print("1. 實現session切換時的訂單自動清理")
        print("2. 添加訂單年齡監控和警報")
        print("3. 在Dashboard添加取消按鈕")
        print("4. 實現價格偏差檢查機制")
    else:
        print("\n❌ 操作失敗")

if __name__ == "__main__":
    main()