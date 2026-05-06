#!/usr/bin/env python3
"""
Single-thread tick test for shioaji 1.3.3 stability verification.

No options monitor, no futures monitor, no threading.
Just: login -> fetch contracts -> subscribe MXFE6 tick -> print ticks for 5 min.
"""
import os
import sys
import time
from datetime import datetime
from dotenv import load_dotenv

# Add project root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
load_dotenv()

import shioaji as sj

# ── Config ──
SYMBOL = "MXFE6"
DURATION_MINUTES = 5
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "stability_test")
os.makedirs(OUT_DIR, exist_ok=True)

# ── Test State ──
tick_count = 0
start_time = None
test_log = []

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    test_log.append(line)

def on_tick(exchange, tick):
    global tick_count
    tick_count += 1
    if tick_count == 1:
        log(f"✅ FIRST TICK: {tick.code} close={tick.close}")
    elif tick_count % 100 == 0:
        elapsed = time.time() - start_time
        log(f"📊 {tick_count} ticks in {elapsed:.0f}s ({tick_count/elapsed:.1f} ticks/s)")

# ── Main Test ──
def main():
    global start_time
    log("=" * 60)
    log("Shioaji Single-Thread Tick Stability Test")
    log(f"Version: {sj.__version__}")
    log(f"Symbol: {SYMBOL}")
    log(f"Duration: {DURATION_MINUTES} minutes")
    log("=" * 60)

    # 1. Login
    api_key = os.getenv("SHIOAJI_API_KEY")
    secret_key = os.getenv("SHIOAJI_SECRET_KEY")
    if not api_key or not secret_key:
        log("❌ Missing SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY in .env")
        return False

    api = sj.Shioaji()
    log("📡 Logging in...")
    try:
        api.login(api_key=api_key, secret_key=secret_key, contracts_timeout=30000)
        log("✅ Login OK")
    except Exception as e:
        log(f"❌ Login failed: {e}")
        return False

    # 2. Activate CA for production mode
    ca_path = os.getenv("SHIOAJI_CA_PATH", "")
    ca_name = os.getenv("SHIOAJI_CA_NAME", "")
    ca_passwd = os.getenv("SHIOAJI_CA_PASSWD", "")
    if ca_path and ca_name and os.path.exists(os.path.join(ca_path, ca_name)):
        try:
            api.activate_ca(ca_path=os.path.join(ca_path, ca_name),
                            ca_passwd=ca_passwd,
                            person_id=os.getenv("SHIOAJI_PERSON_ID", api_key))
            log("✅ CA activated")
        except Exception as e:
            log(f"⚠️ CA activation failed: {e}")

    # 3. Wait for contracts
    log("⏳ Waiting for contracts...")
    time.sleep(5)

    # 4. Get MXF contract
    try:
        # shioaji 1.3.3: attribute access
        mxf_list = list(api.Contracts.Futures.MXF)
        log(f"✅ Found {len(mxf_list)} MXF contracts")
        if not mxf_list:
            log("❌ No MXF contracts found")
            api.logout()
            return False
        contract = mxf_list[0]
        log(f"   Front-month: {contract.code} (delivers {contract.delivery_date})")
    except Exception as e:
        log(f"❌ Failed to get MXF contracts: {e}")
        # Try bracket access as fallback
        try:
            contract = api.Contracts.Futures["MXFE6"]
            log(f"✅ Got MXFE6 via bracket access")
        except Exception as e2:
            log(f"❌ Also failed bracket access: {e2}")
            api.logout()
            return False

    # 5. Register callback with bind=False (receives exchange + tick)
    from core.broker.shioaji_compat import set_tick_callback
    set_tick_callback(api, on_tick)
    log("✅ Tick callback registered")

    # 6. Subscribe
    log(f"📡 Subscribing {contract.code} tick...")
    try:
        api.quote.subscribe(contract, quote_type="tick")
        log("✅ Subscribe OK")
    except Exception as e:
        log(f"❌ Subscribe failed: {e}")
        api.logout()
        return False

    # 7. Collect ticks for DURATION_MINUTES
    start_time = time.time()
    end_time = start_time + DURATION_MINUTES * 60
    log(f"📊 Collecting ticks for {DURATION_MINUTES} minutes...")
    log(f"   Started at: {datetime.now().strftime('%H:%M:%S')}")
    log(f"   Will end at: {datetime.fromtimestamp(end_time).strftime('%H:%M:%S')}")

    while time.time() < end_time:
        time.sleep(1)
        elapsed = time.time() - start_time
        if int(elapsed) % 30 == 0:
            log(f"⏳ {tick_count} ticks so far ({elapsed:.0f}s elapsed)")

    # 8. Summary
    elapsed = time.time() - start_time
    log("=" * 60)
    log("TEST COMPLETE")
    log(f"Duration: {elapsed:.0f}s")
    log(f"Total ticks: {tick_count}")
    if elapsed > 0:
        log(f"Avg rate: {tick_count/elapsed:.1f} ticks/s")
    log(f"Result: {'✅ PASS' if tick_count > 10 else '❌ FAIL'}")
    log("=" * 60)

    api.logout()
    log("🔌 Logged out")
    return tick_count > 10


if __name__ == "__main__":
    success = main()
    # Write log file
    log_path = os.path.join(OUT_DIR, f"stability_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    with open(log_path, "w") as f:
        f.write("\n".join(test_log))
    print(f"\n📝 Log saved to: {log_path}")
    sys.exit(0 if success else 1)
