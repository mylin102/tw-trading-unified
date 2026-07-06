import shioaji as sj
import os
from dotenv import load_dotenv
from core.shioaji_session import get_api
from core.broker.shioaji_compat import get_contracts_list

load_dotenv(override=True)
api = get_api()
api.fetch_contracts()
txos = get_contracts_list(api, "Options", "TXO")
target_date = "2026/05/20"
m5 = [c for c in txos if c.delivery_date == target_date]
print(f"Total for {target_date}: {len(m5)}")
s41300 = [c for c in m5 if abs(c.strike_price - 41300) < 1]
for c in s41300:
    print(f"Code: {c.code}, Strike: {c.strike_price}, Right: {c.option_right}, Type: {type(c.option_right)}")
api.logout()
