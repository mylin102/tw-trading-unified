import shioaji as sj
import os
import time
from core.shioaji_session import get_api

api = get_api()
print("Fetching...")
api.fetch_contracts()
print("Categories:", dir(api.Contracts))
print("Futures content:", repr(api.Contracts.Futures))
try:
    print("MXF content:", repr(api.Contracts.Futures.MXF))
    print("MXF type:", type(api.Contracts.Futures.MXF))
    print("MXF list:", [c.code for c in api.Contracts.Futures.MXF])
except Exception as e:
    print("MXF access failed:", e)

api.logout()
