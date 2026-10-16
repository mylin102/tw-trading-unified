from datetime import datetime
from types import SimpleNamespace


class _OrderManager:
    def __init__(self):
        self.created = []

    def create_order(self, **kwargs):
        order = SimpleNamespace(
            order_id="LOCAL-1", intent_id="INTENT-1", status="PENDING",
            broker_order_id=None, exchange_order_id=None, seqno=None,
            ordno=None, reject_reason=None, **kwargs)
        self.created.append(order)
        return order


def _monitor():
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.live_trading = False
    monitor.dry_run = False
    monitor.paper_fill_sim = None
    monitor.api = None
    monitor.order_mgr = _OrderManager()
    monitor._pending_lifecycle_orders = {}
    monitor._mts_pending_fills = {}
    monitor.events = []
    monitor._append_mts_event = (
        lambda event, **payload: monitor.events.append((event, payload)))
    monitor._submit_via_gateway = lambda order: True
    monitor._reconcile_pending_mts_combos = (
        lambda **kwargs: {"status": "BROKER_CONFIRMED"})
    return monitor


def _submit(monitor, *, action="SELL_NEAR_BUY_FAR", offset_ticks=2,
            near_age=20, far_age=30, **overrides):
    bar = {
        "spread": -163.0,
        "spread_z": 11.26,
        "bbo_near_bid": 48630.0,
        "bbo_near_ask": 48632.0,
        "bbo_far_bid": 48793.0,
        "bbo_far_ask": 48800.0,
        "bbo_near_age_ms": near_age,
        "bbo_far_age_ms": far_age,
    }
    bar.update(overrides)
    return monitor._submit_mts_combo_entry(
        trade_id="T-1", action=action, reason="spread signal",
        near_code="TMFJ6", far_code="TMFK6",
        near_side="sell" if action == "SELL_NEAR_BUY_FAR" else "buy",
        far_side="buy" if action == "SELL_NEAR_BUY_FAR" else "sell",
        near_price=48631.0, far_price=48794.0, bar_dict=bar,
        ts=datetime(2026, 9, 22, 10, 21, 47), snapshot={},
        entry_offset_ticks=offset_ticks, tick_size=1.0)


def _event(monitor, name):
    return next(payload for event, payload in monitor.events if event == name)


def test_sell_entry_uses_fresh_executable_bbo_plus_marketable_offset():
    monitor = _monitor()

    _submit(monitor)

    order = monitor.order_mgr.created[0]
    assert order.price == -172.0  # 48630 - 48800 - 2 ticks
    audit = _event(monitor, "COMBO_ORDER_INTENT_CREATED")
    assert audit["reference_spread"] == -163.0
    assert audit["executable_spread"] == -170.0
    assert audit["entry_offset_ticks"] == 2
    assert audit["limit_price"] == -172.0
    assert audit["near_quote_age_ms"] == 20
    assert audit["far_quote_age_ms"] == 30


def test_buy_entry_uses_opposite_executable_bbo():
    monitor = _monitor()

    _submit(monitor, action="BUY_NEAR_SELL_FAR")

    assert monitor.order_mgr.created[0].price == -159.0  # 48632 - 48793 + 2


def test_stale_submission_bbo_is_rejected_before_order_creation():
    monitor = _monitor()

    _submit(monitor, near_age=501)

    assert monitor.order_mgr.created == []
    rejected = _event(monitor, "ORDER_REJECTED_LOCAL")
    assert rejected["reason"] == "ENTRY_BBO_STALE"
    assert rejected["near_quote_age_ms"] == 501


def test_offset_above_hard_max_is_rejected_before_order_creation():
    monitor = _monitor()

    _submit(monitor, offset_ticks=11)

    assert monitor.order_mgr.created == []
    rejected = _event(monitor, "ORDER_REJECTED_LOCAL")
    assert rejected["reason"] == "ENTRY_PRICE_OFFSET_EXCEEDS_MAX"
    assert rejected["entry_offset_ticks"] == 11
    assert rejected["max_entry_offset_ticks"] == 10


def test_missing_real_bbo_never_falls_back_to_close_or_last():
    monitor = _monitor()

    _submit(monitor, bbo_far_ask=None, far_close=48799.0, far_last=48799.0)

    assert monitor.order_mgr.created == []
    assert _event(monitor, "ORDER_REJECTED_LOCAL")["reason"] == "ENTRY_BBO_MISSING"


def test_offset_five_is_accepted_and_priced_five_ticks_beyond_executable():
    """k=5 must not be rejected and must price 5 ticks beyond the executable spread.

    near_bid 48630 - far_ask 48800 = -170  =>  sell limit = -170 - 5 = -175.
    """
    monitor = _monitor()

    _submit(monitor, offset_ticks=5)

    assert not any(e[0] == "ORDER_REJECTED_LOCAL" for e in monitor.events), monitor.events
    assert monitor.order_mgr.created, "order must be created for k=5"
    assert monitor.order_mgr.created[0].price == -175.0  # near_bid 48630 - far_ask 48800 = -170; -170 - 5 ticks


def test_offset_ten_is_accepted_and_priced_ten_ticks_beyond_executable():
    """Raised cap: entries may be priced up to 10 ticks beyond the executable side.

    near_bid 48630 - far_ask 48800 = -170  =>  sell limit = -170 - 10 = -180.
    """
    monitor = _monitor()

    _submit(monitor, offset_ticks=10)

    assert not any(e[0] == "ORDER_REJECTED_LOCAL" for e in monitor.events), monitor.events
    assert monitor.order_mgr.created, "order must be created for k=10"
    assert monitor.order_mgr.created[0].price == -180.0


def test_entry_wire_price_type_is_day_mkt_night_lmt():
    """Day session must request a market (MKT) wire price type; night stays limit (LMT)."""
    from strategies.futures.monitor import entry_wire_price_type

    on = {"time": "2026-09-23T10:30:00+08:00"}
    off = {"time": "2026-09-23T21:30:00+08:00"}
    cfg = {"entry_price_type_day": "MKT", "entry_price_type_night": "LMT"}
    assert entry_wire_price_type(cfg, on) == "MKT"
    assert entry_wire_price_type(cfg, off) == "LMT"
    # unknown / missing values fall back to the conservative limit type
    assert entry_wire_price_type({}, on) == "LMT"


def test_combo_wire_price_type_is_honoured_and_defaults_to_lmt():
    """The client must send the requested wire price_type (and default to LMT)."""
    from types import SimpleNamespace
    import shioaji as _sj
    from strategies.futures.squeeze_futures.data.shioaji_client import wire_price_type_for

    assert wire_price_type_for(SimpleNamespace(wire_price_type="MKT")) == _sj.FuturesPriceType.MKT
    assert wire_price_type_for(SimpleNamespace(wire_price_type="mkp")) == _sj.FuturesPriceType.MKP
    assert wire_price_type_for(SimpleNamespace(wire_price_type="LMT")) == _sj.FuturesPriceType.LMT
    # missing / unknown -> conservative LMT
    assert wire_price_type_for(SimpleNamespace()) == _sj.FuturesPriceType.LMT
    assert wire_price_type_for(SimpleNamespace(wire_price_type="NOPE")) == _sj.FuturesPriceType.LMT


def test_far_quote_age_within_the_far_limit_is_accepted():
    monitor = _monitor()

    _submit(monitor, near_age=100, far_age=1200)   # far leg is thin: 1.2s must still pass

    assert monitor.order_mgr.created, "a 1.2s-old far quote must be usable"
    audit = _event(monitor, "COMBO_ORDER_INTENT_CREATED")
    assert audit["far_quote_age_ms"] == 1200


def test_far_quote_age_beyond_the_far_limit_is_rejected():
    monitor = _monitor()

    _submit(monitor, near_age=100, far_age=1600)

    assert monitor.order_mgr.created == []
    rejected = _event(monitor, "ORDER_REJECTED_LOCAL")
    assert rejected["reason"] == "ENTRY_BBO_STALE"
    assert rejected["far_quote_age_ms"] == 1600


def test_near_quote_age_keeps_the_tight_limit():
    monitor = _monitor()

    _submit(monitor, near_age=600, far_age=20)     # near stays at 500ms

    assert monitor.order_mgr.created == []
    assert _event(monitor, "ORDER_REJECTED_LOCAL")["reason"] == "ENTRY_BBO_STALE"


def test_per_leg_limits_are_read_from_config():
    from strategies.futures.monitor import entry_bbo_age_limits

    assert entry_bbo_age_limits(None) == (500.0, 1500.0)
    assert entry_bbo_age_limits({"mts": {}}) == (500.0, 1500.0)
    assert entry_bbo_age_limits(
        {"mts": {"entry_bbo_max_age_ms_near": 250,
                 "entry_bbo_max_age_ms_far": 2000}}) == (250.0, 2000.0)

    monitor = _monitor()
    monitor.cfg = {"mts": {"entry_bbo_max_age_ms_near": 250,
                           "entry_bbo_max_age_ms_far": 2000}}
    _submit(monitor, near_age=300, far_age=1900)   # near 300 > configured 250
    assert monitor.order_mgr.created == []
    assert _event(monitor, "ORDER_REJECTED_LOCAL")["reason"] == "ENTRY_BBO_STALE"
