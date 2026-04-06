import os
import sys
from pathlib import Path
from dotenv import load_dotenv
import shioaji as sj

# Ensure project root is in path
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.stocks.scanner import StockScanner # noqa: E402
from strategies.stocks.monitor import StockMonitorDryRun # noqa: E402

def run_stock_simulation():
    load_dotenv(override=True)
    user_id = os.getenv('SHIOAJI_API_KEY') or os.getenv('SHIOAJI_PERSON_ID')
    password = os.getenv('SHIOAJI_SECRET_KEY') or os.getenv('SHIOAJI_PASSWD')

    api = sj.Shioaji()
    try:
        api.login(user_id, password, contracts_timeout=10000)
    except Exception as e:
        print(f"Login failed: {e}")
        return

    # Find stock account
    accounts = api.list_accounts()
    stock_acc = next((acc for acc in accounts if "Stock" in str(acc.account_type)), None)
    
    if not stock_acc:
        print("No stock account found.")
        return

    print(f"--- STARTING DRY RUN (Account: {stock_acc.account_id}) ---")

    # 1. Initialize Scanner
    scanner = StockScanner(api)
    watchlist = ["2330", "2454", "2317", "2303"]
    
    print(f"Scanning Squeeze for {watchlist}...")
    scan_results = scanner.scan_squeeze(watchlist)
    print("\nScan Results:")
    print(scan_results)

    # 2. Initialize Monitor (Dry Run)
    monitor = StockMonitorDryRun(api, stock_acc, capital_limit=20000)
    
    # 3. Simulate a signal for the first found ticker
    if not scan_results.empty:
        target = scan_results.iloc[0]
        monitor.on_signal(target["ticker"], "BUY", target["close"], "SQZ_FIRED")
        
        # Simulate tracking
        monitor.monitor_tick(target["ticker"])
        
        # Simulate exit
        monitor.on_signal(target["ticker"], "SELL", target["close"] * 1.02, "TAKE_PROFIT")

    api.logout()
    print("\n--- DRY RUN COMPLETED ---")

if __name__ == "__main__":
    run_stock_simulation()
