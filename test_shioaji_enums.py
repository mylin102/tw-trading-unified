#!/usr/bin/env python3
"""
測試 Shioaji 的 OrderState 和 Status 枚舉
"""

import shioaji as sj

print("Shioaji version:", sj.__version__)
print()

# 檢查 OrderState
print("檢查 sj.constant.OrderState:")
try:
    print("  OrderState attributes:")
    for attr in dir(sj.constant.OrderState):
        if not attr.startswith('_'):
            print(f"    {attr}")
except Exception as e:
    print(f"  錯誤: {e}")

print()

# 檢查 Status
print("檢查 sj.constant.Status:")
try:
    print("  Status attributes:")
    for attr in dir(sj.constant.Status):
        if not attr.startswith('_'):
            print(f"    {attr}")
except Exception as e:
    print(f"  錯誤: {e}")

print()

# 檢查是否有 Submitted 屬性
print("檢查 Submitted 屬性:")
try:
    if hasattr(sj.constant.OrderState, 'Submitted'):
        print("  OrderState.Submitted 存在")
    else:
        print("  OrderState.Submitted 不存在")
except Exception as e:
    print(f"  錯誤: {e}")

try:
    if hasattr(sj.constant.Status, 'Submitted'):
        print("  Status.Submitted 存在")
    else:
        print("  Status.Submitted 不存在")
except Exception as e:
    print(f"  錯誤: {e}")

print()

# 測試實際使用
print("測試實際使用:")
try:
    status = sj.constant.Status.Submitted
    print(f"  Status.Submitted = {status}")
except Exception as e:
    print(f"  Status.Submitted 錯誤: {e}")

try:
    order_state = sj.constant.OrderState.Submitted
    print(f"  OrderState.Submitted = {order_state}")
except Exception as e:
    print(f"  OrderState.Submitted 錯誤: {e}")