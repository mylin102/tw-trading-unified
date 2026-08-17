from datetime import datetime
from unittest.mock import MagicMock, patch

from core.order_management.order_manager import OrderManager
from core.order_management.order import OrderStatus
from core.signal import Signal
from strategies.futures.monitor import FuturesMonitor


class _Contract:
    def __init__(self, code):
        self.code = code


class _Strategy:
    _trade_id = "mts-wiring-001"
    _near_side = "SHORT"
    _far_side = "LONG"
    _near_qty = 1
    _far_qty = 1
    _has_position = True
    _released_leg = None
    _side = None


def _monitor(tmp_path):
    api = MagicMock()
    mon = FuturesMonitor(api, "config/futures_night.yaml", dry_run=True)
    mon.ticker = "TMF"
    mon.contract = _Contract("TMFH6")
    mon.far_contract = _Contract("TMFI6")
    mon.order_mgr = OrderManager(api)
    mon._use_order_manager = True
    mon._leg_lock_store = str(tmp_path / "mts_leg_locks.json")
    mon._claimed_execution_keys = set()
    return mon


def _common_patches(mon):
    return (
        patch.object(mon, "_validate_exit_only_position",
                     return_value=(True, None, None)),
        patch.object(mon, "_pre_submit_exit_only_proof",
                     return_value=(True, None)),
        patch.object(mon, "_authorize_intent",
                     return_value=(True, {}, "ok")),
        patch.object(mon, "_hydrate_exit_only_position"),
        patch("strategies.futures.monitor.is_taifex_futures_market_open",
              return_value=True),
    )


def test_partial_release_wiring_locks_before_submit_and_blocks_duplicate(tmp_path):
    mon = _monitor(tmp_path)
    strategy = _Strategy()
    signal = Signal("PARTIAL_EXIT", "RELEASE_NEAR")
    bar = {"near_close": 46000.0, "far_close": 46100.0}
    receipts = []

    def submit(order, **_kwargs):
        order.broker_order_id = "BROKER-NEAR"
        order.seqno = "11"
        receipts.append(order)
        return {"broker_order_id": "BROKER-NEAR", "seqno": "11"}

    with _common_patches(mon)[0], _common_patches(mon)[1], \
            _common_patches(mon)[2], _common_patches(mon)[3], \
            _common_patches(mon)[4], \
            patch.object(mon, "_submit_via_gateway", side_effect=submit):
        mon._submit_mts_order_signal(signal, strategy, bar, datetime.now())
        mon._submit_mts_order_signal(signal, strategy, bar, datetime.now())

    assert len(receipts) == 1
    assert mon._leg_lock_check({
        "trade_id": strategy._trade_id,
        "session_generation": "",
        "contract": "TMFH6",
        "closing_side": "BUY",
        "qty": 1,
    }) is True


def test_combined_exit_pair_conflict_blocks_before_order_creation(tmp_path):
    mon = _monitor(tmp_path)
    strategy = _Strategy()
    signal = Signal("EXIT", "TMF_COMBINED_EXIT")
    bar = {"near_close": 46000.0, "far_close": 46100.0}

    with patch.object(mon.order_mgr, "create_order",
                      wraps=mon.order_mgr.create_order) as create_order, \
            _common_patches(mon)[0], _common_patches(mon)[1], \
            _common_patches(mon)[2], _common_patches(mon)[3], \
            _common_patches(mon)[4], \
            patch.object(mon, "_mts_ledger_reconstructed_open_qty",
                         return_value=None), \
            patch.object(mon, "_leg_lock_check", return_value=False), \
            patch.object(mon, "_leg_lock_acquire_pair", return_value=False), \
            patch.object(mon, "_submit_via_gateway") as submit:
        mon._submit_mts_order_signal(signal, strategy, bar, datetime.now())

    create_order.assert_not_called()
    submit.assert_not_called()


def test_filled_callback_releases_wired_partial_lock(tmp_path):
    mon = _monitor(tmp_path)
    strategy = _Strategy()
    signal = Signal("PARTIAL_EXIT", "RELEASE_NEAR")
    bar = {"near_close": 46000.0, "far_close": 46100.0}
    order_box = []

    def submit(order, **_kwargs):
        order.broker_order_id = "BROKER-NEAR"
        order.seqno = "12"
        order_box.append(order)
        return {"broker_order_id": "BROKER-NEAR", "seqno": "12"}

    with _common_patches(mon)[0], _common_patches(mon)[1], \
            _common_patches(mon)[2], _common_patches(mon)[3], \
            _common_patches(mon)[4], \
            patch.object(mon, "_submit_via_gateway", side_effect=submit):
        mon._submit_mts_order_signal(signal, strategy, bar, datetime.now())

    order = order_box[0]
    mon._leg_lock_apply_order_event(order, OrderStatus.FILLED, fill_qty=1)
    assert mon._leg_lock_check({
        "trade_id": strategy._trade_id,
        "session_generation": "",
        "contract": "TMFH6",
        "closing_side": "BUY",
        "qty": 1,
    }) is False
