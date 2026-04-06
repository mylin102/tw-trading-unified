import os
import sys
from pathlib import Path
from dotenv import load_dotenv
import shioaji as sj

# Ensure project root is in path
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.stocks.downloader import StockDownloader # noqa: E402

def add_to_watchlist(ticker: str, api=None):
    """
    1. 驗證代號是否存在
    2. 下載歷史 5分K 資料
    3. 返回執行狀態
    """
    load_dotenv(override=True)
    
    # 如果外部沒有傳入現有的 API session，則自行登入
    internal_api = False
    if api is None:
        user_id = os.getenv('SHIOAJI_API_KEY') or os.getenv('SHIOAJI_PERSON_ID')
        password = os.getenv('SHIOAJI_SECRET_KEY') or os.getenv('SHIOAJI_PASSWD')
        api = sj.Shioaji()
        api.login(user_id, password, contracts_timeout=10000)
        internal_api = True

    try:
        # 驗證代號
        contract = api.Contracts.Stocks.get(ticker)
        if not contract:
            return False, f"Ticker {ticker} not found in Shioaji Contracts."

        # 執行下載
        downloader = StockDownloader(api)
        downloader.update_ticker(ticker)
        
        return True, f"Successfully added and updated {ticker}."
    
    except Exception as e:
        return False, str(e)
    finally:
        if internal_api:
            api.logout()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        success, msg = add_to_watchlist(sys.argv[1])
        print(msg)
    else:
        print("Usage: python3 scripts/add_watchlist.py <ticker>")
