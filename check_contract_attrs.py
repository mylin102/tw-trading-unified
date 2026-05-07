import shioaji as sj
import os
from core.shioaji_session import get_api
from core.broker.shioaji_compat import get_contracts_list

api = get_api()
api.fetch_contracts()
txos = get_contracts_list(api, "Options", "TXO")
if txos:
    c = txos[0]
    print(f"Code: {c.code}")
    print(f"Delivery: {c.delivery_date}, type: {type(c.delivery_date)}")
    print(f"Right: {c.option_right}, type: {type(c.option_right)}")
api.logout()
