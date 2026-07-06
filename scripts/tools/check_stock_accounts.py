import os
import shioaji as sj
from dotenv import load_dotenv

def check_accounts_final():
    load_dotenv(override=True)
    
    user_id = os.getenv('SHIOAJI_API_KEY') or os.getenv('SHIOAJI_PERSON_ID')
    password = os.getenv('SHIOAJI_SECRET_KEY') or os.getenv('SHIOAJI_PASSWD')

    if not user_id or not password:
        print("❌ 錯誤: .env 中缺少憑證")
        return

    api = sj.Shioaji()
    try:
        if len(user_id) > 15:
            api.login(api_key=user_id, secret_key=password, contracts_timeout=10000)
        else:
            api.login(user_id, password, contracts_timeout=10000)
        print("✅ 登入成功！")
    except Exception as e:
        print(f"❌ 登入失敗: {e}")
        return

    accounts = api.list_accounts()
    stock_account = next((acc for acc in accounts if "Stock" in str(acc.account_type)), None)

    if not stock_account:
        print("❌ 警告: 找不到股票帳戶！")
    else:
        print(f"\n🎯 測試讀取股票帳戶: {stock_account.account_id}")
        
        print("\n--- 讀取股票庫存 (深度分析) ---")
        try:
            positions = api.list_positions(stock_account)
            if not positions:
                print("目前庫存為空。")
            else:
                for pos in positions:
                    # 1. 顯示原始 Dict 內容
                    p_dict = pos.dict()
                    print(f"\n標的: {pos.code}")
                    print(f"  > 原始 Dict: {p_dict}")
                    
                    # 2. 測試零股行情讀取 (使用您提供的建議代碼)
                    try:
                        contract = api.Contracts.Stocks[pos.code]
                        odd_snapshot = api.snapshots([contract])
                        if odd_snapshot:
                            s = odd_snapshot[0]
                            print(f"  > [零股即時行情] 買: {s.buy_price} | 賣: {s.sell_price} | 現: {s.close}")
                    except Exception as se:
                        print(f"  > [零股行情失敗]: {se}")
                        
        except Exception as e:
            print(f"❌ 讀取庫存出錯: {e}")

    api.logout()
    print("\n👋 測試結束。")

if __name__ == "__main__":
    check_accounts_final()
