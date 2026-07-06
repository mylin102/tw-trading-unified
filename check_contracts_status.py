import shioaji as sj
import os
import time
from core.shioaji_session import get_api

api = get_api()
print("Fetching...")
api.fetch_contracts()

def print_category(name):
    try:
        cat = getattr(api.Contracts.Futures, name)
        print(f"\n=== {name} Contracts ===")
        print(f"Content: {repr(cat)}")
        print(f"List: {[c.code for c in cat]}")
        for c in cat:
             print(f"  {c.code}: name={c.name}, delivery_date={c.delivery_date}")
    except Exception as e:
        print(f"{name} access failed: {e}")

print_category("TMF")
print_category("MXF")

api.logout()
