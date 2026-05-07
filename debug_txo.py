import shioaji as sj
import os
import datetime
from core.shioaji_session import get_api
from core.broker.shioaji_compat import get_contracts_list

api = get_api()
print("Fetching...")
api.fetch_contracts()

print("\n--- TXO Analysis ---")
txos = get_contracts_list(api, "Options", "TXO")
print(f"Total TXO: {len(txos)}")

# Filter for 2026-05-20
target_date = "2026-05-20"
m5_txos = [c for c in txos if c.delivery_date == target_date]
print(f"TXO for {target_date}: {len(m5_txos)}")

if m5_txos:
    strikes = sorted(list(set([c.strike_price for c in m5_txos])))
    print(f"Strikes: {strikes[:10]} ... {strikes[-10:]}")
    
    # Check 41300
    s41300 = [c for c in m5_txos if c.strike_price == 41300]
    print(f"Contracts at 41300: {[ (c.code, c.option_right) for c in s41300 ]}")

api.logout()
