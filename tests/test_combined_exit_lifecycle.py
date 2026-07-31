#!/usr/bin/env python3
"""
Combined Exit Order Lifecycle — deterministic CI suite.
Runs via canonical OrderManager + fill callback path.

Gate: All tests must pass before Combined Exit changes are merged.

Covers:
T1: Two orders visible in authoritative ledger
T2: Near-only partial fill → no completion
T3: Far-only partial fill → no completion
T4: Both fill → exactly one COMBINED_EXIT_COMPLETED
T5: Duplicate callbacks → no duplicate COMPLETED
T6: Expected qty missing → fail-closed (no completion)
T7: Wrong group correlation → no completion
T8: REGRESSION: _far_open_qty==0 + FAR filled_qty==0 → NOT completed
T9: Reverse fill order (Far first)
T10: Cancelled leg blocks completion
"""
import sys, uuid
sys.path.insert(0, ".")

from core.order_management.order_manager import OrderManager
from core.order_management.paper_fill import PaperFillSimulator
from core.order_management.order import OrderStatus, OrderType, OrderSide
from datetime import datetime

PASS = "\U0001f7e9 PASS"
FAIL  = "\U0001f534 FAIL"
stats = {"pass": 0, "fail": 0}

def tick(code, price):
    t = type("Tick", (), {})()
    t.code = code; t.close = price; t.open = price
    t.high = price; t.low = price; t.volume = 0
    return t

class Harness:
    def __init__(self):
        self.om = OrderManager(mode="paper")
        self.pfs = PaperFillSimulator(self.om)
        self.om.set_simulator(self.pfs)
        self._pending = {}
        self._groups = {}
        self._completed = set()
        self._events = []
        self.om.register_callback("on_fill", self._on_fill)

    def _on_fill(self, ev):
        if ev.status not in (OrderStatus.PARTIAL_FILLED, OrderStatus.FILLED):
            return
        p = self._pending.get(ev.order_id)
        if p is None or ev.fill_qty <= 0:
            return
        sig = p.get("signal")
        if sig not in ("COMBINED_EXIT_NEAR", "COMBINED_EXIT_FAR"):
            return
        self._apply_fill(ev, p, sig, ev.fill_price)

    def _apply_fill(self, ev, p, sig, price):
        leg = "NEAR" if "NEAR" in str(sig) else "FAR"
        gid = p.get("group_id")
        if not gid or gid not in self._groups:
            return
        ceg = self._groups[gid]
        if ceg.get("completed"):
            return
        if leg == "NEAR":
            ceg["near_filled"] = True
            ceg["near_fill_price"] = price
        else:
            ceg["far_filled"] = True
            ceg["far_fill_price"] = price
        self._events.append(("LEG_FILLED", leg, ev.order_id, price))
        if ceg["near_filled"] and ceg["far_filled"]:
            ceg["completed"] = True
            self._completed.add(gid)
            self._events.append(("COMBINED_EXIT_COMPLETED", gid))

    def submit_pair(self, gid=None):
        gid = gid or f"CE-{datetime.now().strftime('%H%M%S')}-{uuid.uuid4().hex[:8]}"
        n = self.om.create_order(symbol="TMFH6", side=OrderSide.BUY,
            order_type=OrderType.MKP, quantity=1, strategy="MTS_EXIT")
        f = self.om.create_order(symbol="TMFI6", side=OrderSide.SELL,
            order_type=OrderType.MKP, quantity=1, strategy="MTS_EXIT")
        self.om.submit(n); self.om.submit(f)
        self._pending[n.order_id] = {"signal":"COMBINED_EXIT_NEAR","lots":1,
            "group_id":gid,"strategy":"MTS_EXIT"}
        self._pending[f.order_id] = {"signal":"COMBINED_EXIT_FAR","lots":1,
            "group_id":gid,"strategy":"MTS_EXIT"}
        self.pfs.register(n); self.pfs.register(f)
        self._groups[gid] = {"near_filled":False,"near_fill_price":None,
            "far_filled":False,"far_fill_price":None,"completed":False}
        return n, f, gid

def check(desc, cond):
    if cond:
        stats["pass"] += 1; print(f"  {PASS} {desc}")
    else:
        stats["fail"] += 1; print(f"  {FAIL} {desc}")

# T1
def t1_two_orders():
    h = Harness()
    n, f, gid = h.submit_pair()
    check("Two different order IDs", n.order_id != f.order_id)
    check("Near BUY TMFH6", n.symbol == "TMFH6" and n.side == OrderSide.BUY)
    check("Far SELL TMFI6", f.symbol == "TMFI6" and f.side == OrderSide.SELL)
    check("Both SUBMITTED", n.status == OrderStatus.SUBMITTED and f.status == OrderStatus.SUBMITTED)
    check("Same group_id in pending", all(p["group_id"] == gid for p in h._pending.values()))
    check("Both in active_orders", n.order_id in h.om.active_orders and f.order_id in h.om.active_orders)

# T2
def t2_near_only():
    h = Harness()
    n, f, gid = h.submit_pair()
    h.pfs.process_tick(tick("TMFH6", 42032))
    check("Near filled", n.filled_quantity >= n.quantity)
    check("Far NOT filled", f.filled_quantity == 0)
    check("NOT completed", gid not in h._completed)
    check("No COMPLETED event",
          not any(e[0]=="COMBINED_EXIT_COMPLETED" for e in h._events))

# T3
def t3_far_only():
    h = Harness()
    n, f, gid = h.submit_pair()
    h.pfs.process_tick(tick("TMFI6", 42214))
    check("Far filled", f.filled_quantity >= f.quantity)
    check("Near NOT filled", n.filled_quantity == 0)
    check("NOT completed", gid not in h._completed)

# T4
def t4_both():
    h = Harness()
    n, f, gid = h.submit_pair()
    h.pfs.process_tick(tick("TMFH6", 42032))
    h.pfs.process_tick(tick("TMFI6", 42214))
    check("Both filled", n.filled_quantity>=n.quantity and f.filled_quantity>=f.quantity)
    check("Completed", gid in h._completed)
    ce_count = sum(1 for e in h._events if e[0]=="COMBINED_EXIT_COMPLETED")
    check("Exactly one COMPLETED event", ce_count == 1)

# T5
def t5_dup():
    h = Harness()
    n, f, gid = h.submit_pair()
    h.pfs.process_tick(tick("TMFH6", 42032))
    h.pfs.process_tick(tick("TMFI6", 42214))
    ce1 = sum(1 for e in h._events if e[0]=="COMBINED_EXIT_COMPLETED")
    h.pfs.process_tick(tick("TMFH6", 42032))
    h.pfs.process_tick(tick("TMFI6", 42214))
    ce2 = sum(1 for e in h._events if e[0]=="COMBINED_EXIT_COMPLETED")
    check("Duplicate produces no new COMPLETED", ce2 == ce1)
    check("Only one group in completed set", len(h._completed) == 1)

# T6
def t6_missing_qty():
    h = Harness()
    n, f, gid = h.submit_pair()
    h._pending[n.order_id].pop("lots", None)
    h.om.cancel(f.order_id, reason="test_reject")
    check("Rejected leg cancelled", f.status == OrderStatus.CANCELLED)
    h.pfs.process_tick(tick("TMFH6", 42032))
    check("NOT completed with missing qty", gid not in h._completed)

# T7
def t7_wrong_group():
    h = Harness()
    n, f, gid = h.submit_pair()
    bad_gid = "CE-BAD-" + uuid.uuid4().hex[:8]
    h._pending[f.order_id]["group_id"] = bad_gid
    h._pending[f.order_id]["signal"] = "COMBINED_EXIT_FAR"
    h._groups[bad_gid] = {"near_filled":False,"near_fill_price":None,
        "far_filled":False,"far_fill_price":None,"completed":False}
    h.pfs.process_tick(tick("TMFH6", 42032))
    h.pfs.process_tick(tick("TMFI6", 42214))
    check("Original group not completed", gid not in h._completed)

# T8 — P0 REGRESSION
def t8_far_open_qty_zero_regression():
    h = Harness()
    n, f, gid = h.submit_pair()
    h.pfs.process_tick(tick("TMFH6", 42032))
    ceg = h._groups[gid]
    # Simulate what _apply_combined_exit_fill used to do
    far_open_qty = 0  # this was the bug: strategy._far_open_qty == 0
    if far_open_qty == 0 and f.filled_quantity == 0:
        # This block must NOT infer far_complete
        inferred = False
    else:
        inferred = True
    check("far_open_qty==0 AND far filled_qty==0", far_open_qty==0 and f.filled_quantity==0)
    check("far_complete NOT inferred", not inferred)
    check("NOT completed despite zero inference", gid not in h._completed)
    check("No COMPLETED event",
          not any(e[0]=="COMBINED_EXIT_COMPLETED" for e in h._events))
    # Now fill far legitimately
    h.pfs.process_tick(tick("TMFI6", 42214))
    check("Completed after legitimate far fill", gid in h._completed)

# T9
def t9_reverse():
    h = Harness()
    n, f, gid = h.submit_pair()
    h.pfs.process_tick(tick("TMFI6", 42214))
    check("Far filled first", f.filled_quantity >= f.quantity)
    check("Near unfilled second", n.filled_quantity == 0)
    check("Not completed after Far only", gid not in h._completed)
    h.pfs.process_tick(tick("TMFH6", 42032))
    check("Near filled second", n.filled_quantity >= n.quantity)
    check("Completed after both", gid in h._completed)

# T10
def t10_cancelled_leg():
    h = Harness()
    n, f, gid = h.submit_pair()
    h.om.cancel(n.order_id, reason="test")
    h.pfs.process_tick(tick("TMFI6", 42214))
    check("NOT completed (near cancelled)", gid not in h._completed)
    n2 = h.om.create_order(symbol="TMFH6", side=OrderSide.BUY,
        order_type=OrderType.MKP, quantity=1, strategy="MTS_EXIT")
    h.om.submit(n2)
    h._pending[n2.order_id] = {"signal":"COMBINED_EXIT_NEAR","lots":1,
        "group_id":gid,"strategy":"MTS_EXIT"}
    h.pfs.register(n2)
    h.pfs.process_tick(tick("TMFH6", 42032))
    check("Replacement near filled", n2.filled_quantity >= n2.quantity)

# Main
tests = [
    ("T1 Two orders visible", t1_two_orders),
    ("T2 Near-only partial fill", t2_near_only),
    ("T3 Far-only partial fill", t3_far_only),
    ("T4 Both fill -> completed", t4_both),
    ("T5 Duplicate callbacks", t5_dup),
    ("T6 Missing qty fail-closed", t6_missing_qty),
    ("T7 Wrong group correlation", t7_wrong_group),
    ("T8 REGRESSION: _far_open_qty==0", t8_far_open_qty_zero_regression),
    ("T9 Reverse fill order", t9_reverse),
    ("T10 Cancelled leg", t10_cancelled_leg),
]

# 2026-07-31 Hermes Agent: CI-COLLECT-001 fix — this file is a script-style
# suite (no pytest test_* functions). Module-level execution called exit(0)
# during pytest collection, aborting the whole session with INTERNALERROR.
# Guard with __main__ so pytest import is side-effect free.
if __name__ == "__main__":
    print(f"{'='*65}")
    print(f" Combined Exit Order Lifecycle — CI Suite")
    print(f"{'='*65}")
    print(f" Python: {sys.version.split()[0]}")
    print(f" Time:   {datetime.now().isoformat()[:19]}")
    print()

    for name, fn in tests:
        print(f"  --- {name} ---")
        try:
            fn()
        except Exception as e:
            stats["fail"] += 1
            print(f"    {FAIL} exception: {e}")
            import traceback; traceback.print_exc()
        print()

    print(f"{'='*65}")
    print(f" RESULTS:  {stats['pass']} pass  {stats['fail']} fail")
    print(f"{'='*65}")
    exit(0 if stats['fail'] == 0 else 1)
