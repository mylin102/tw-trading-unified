import json
import os
from datetime import datetime

# 1. Send brute-force exit flag
# We need to exit MXF_NEAR (Short at 41800)
# Our new close_all will handle this if we improve it, 
# but let's send a specific signal if possible.

flag = {
    "action": "close_all",
    "force_symbols": ["MXF_NEAR", "MXF_FAR"],
    "reason": "MANUAL_FIX"
}

with open("/tmp/futures_manual_trade.flag", "w") as f:
    json.dump(flag, f)

print("Brute-force close_all flag sent.")
