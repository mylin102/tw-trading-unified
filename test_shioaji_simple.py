#!/usr/bin/env python3
"""
最小化 Shioaji 測試腳本
只測試：登入 + 訂閱一檔期貨
"""
import os
import sys
import time
import traceback
import shioaji as sj
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

def main():
    print("=" * 60)
    print("Shioaji 最小化測試")
    print(f"時間: {datetime.now()}")
    print(f"Shioaji 版本: {sj.__version__}")
    print("=" * 60)
    
    # 初始化 API
    api = sj.Shioaji()
    
    try:
        # 登入
        api_key = os.getenv("SHIOAJI_API_KEY")
        secret_key = os.getenv("SHIOAJI_SECRET_KEY")
        
        if not api_key or not secret_key:
            print("❌ 缺少 API 金鑰或密鑰")
            return
        
        print(f"🔑 使用 API 金鑰: {api_key[:8]}...")
        
        # 登入
        print("🔄 登入中...")
        api.login(api_key=api_key, secret_key=secret_key, fetch_contract=True)
        print("✅ 登入成功")
        
        # 獲取合約
        print("📋 獲取合約中...")
        contracts = api.Contracts.Futures["TMF"]
        
        # 選擇最近月合約
        valid_contracts = []
        for contract in contracts:
            if hasattr(contract, 'delivery_date'):
                try:
                    # 簡單檢查合約是否有效
                    if contract.delivery_date >= "2026-04-14":
                        valid_contracts.append(contract)
                except:
                    continue
        
        if not valid_contracts:
            print("❌ 找不到有效合約")
            return
        
        # 按到期日排序
        valid_contracts.sort(key=lambda x: x.delivery_date)
        contract = valid_contracts[0]
        
        print(f"📈 選擇合約: {contract.code} (到期日: {contract.delivery_date})")
        
        # 定義回調函數
        tick_count = [0]
        def on_tick(exchange, tick):
            tick_count[0] += 1
            if tick_count[0] <= 5:  # 只顯示前5個tick
                print(f"📥 Tick #{tick_count[0]}: {tick.code} = {tick.close}")
            elif tick_count[0] == 10:
                print(f"📊 已接收 {tick_count[0]} 個tick，測試成功！")
        
        # 訂閱
        print("📡 訂閱行情中...")
        api.quote.subscribe(
            contract,
            quote_type=sj.constant.QuoteType.Tick,
            callback=on_tick
        )
        print("✅ 訂閱成功")
        
        # 運行一段時間
        print("\n⏳ 運行30秒接收行情...")
        for i in range(30):
            sys.stdout.write(f"\r⏱️  剩餘: {30-i}秒 | Tick數: {tick_count[0]}")
            sys.stdout.flush()
            time.sleep(1)
        
        print("\n\n🔄 取消訂閱...")
        api.quote.unsubscribe(contract)
        
        print("🔒 登出中...")
        api.logout()
        
        print("✅ 測試完成！")
        print(f"總共接收 {tick_count[0]} 個tick")
        
    except Exception as e:
        print(f"\n❌ 發生錯誤:")
        print(traceback.format_exc())
        
        # 記錄錯誤到檔案
        with open("shioaji_error.log", "a") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"時間: {datetime.now()}\n")
            f.write(f"錯誤: {e}\n")
            f.write(traceback.format_exc())
            f.write(f"\n{'='*60}\n")
        
        # 嘗試清理
        try:
            api.logout()
        except:
            pass
    
    finally:
        print("\n" + "=" * 60)
        print("測試結束")

if __name__ == "__main__":
    main()