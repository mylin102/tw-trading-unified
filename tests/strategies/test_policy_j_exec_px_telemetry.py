"""2026-11-03 Hermes Agent: the executable-price profit target must leave
evidence. b3a0176b judges POLICY_J_FIXED_TARGET on bid/ask, but nothing
recorded the executable value, so on Mini (trade mts-auto-085334-230, mark
net +238 vs target 200, no exit) it was impossible to tell whether the new
gate was holding the exit or the mark had only touched the target briefly.

Contract:
  * _record_exit_px(...) stores the latest executable snapshot on the strategy
    and emits POLICY_J_TARGET_EXEC_GAP when mark net >= target but executable
    net < target (fixed_target mode only), at most once per trade per 60s.
  * POLICY_J_LIVE_TRIGGERED carries the executable snapshot fields
    (checked against the real source so the wiring cannot silently drop them).
"""
import ast
from pathlib import Path

import pytest

import strategies.plugins.futures.active.tmf_spread as tmf

SRC = Path(tmf.__file__)
CAPTURED = []


@pytest.fixture(autouse=True)
def _capture(monkeypatch):
    CAPTURED.clear()
    monkeypatch.setattr(tmf, "_append_event", lambda ev, **kw: CAPTURED.append((ev, kw)))


def _s(mode="fixed_target"):
    s = object.__new__(tmf.TMFSpread)
    s._params = {"combined_upl_exit_mode": mode, "combined_upl_activation_net_pnl_twd": 200.0,
                 "enable_combined_upl_trail": True}
    s._trade_id = "mts-test"
    return s


BAR = {"near_bid": 100.0, "near_ask": 101.0, "far_bid": 200.0, "far_ask": 201.0}


def _gap_events():
    return [kw for ev, kw in CAPTURED if ev == "POLICY_J_TARGET_EXEC_GAP"]


def test_gap_event_when_mark_clears_target_but_executable_does_not():
    s = _s()
    # mark (16+17.8)*10-92 = 246 ; executable (14+14)*10-92 = 188
    s._record_exit_px(BAR, 16.0, 17.8, 14.0, 14.0, "EXIT_SIDE_BBO", now_mono=1000.0)
    ev = _gap_events()
    assert len(ev) == 1
    e = ev[0]
    assert e["mark_net_twd"] == pytest.approx(246.0)
    assert e["exec_net_twd"] == pytest.approx(188.0)
    assert e["target_twd"] == 200.0
    assert e["exit_px_basis"] == "EXIT_SIDE_BBO"
    assert e["near_bid"] == 100.0 and e["far_ask"] == 201.0
    assert e["trade_id"] == "mts-test"


def test_gap_event_rate_limited_per_trade():
    s = _s()
    for t in (1000.0, 1010.0, 1059.0):
        s._record_exit_px(BAR, 16.0, 17.8, 14.0, 14.0, "EXIT_SIDE_BBO", now_mono=t)
    assert len(_gap_events()) == 1
    s._record_exit_px(BAR, 16.0, 17.8, 14.0, 14.0, "EXIT_SIDE_BBO", now_mono=1061.0)
    assert len(_gap_events()) == 2


def test_no_gap_event_when_both_clear_or_both_miss_or_giveback_mode():
    s = _s()
    s._record_exit_px(BAR, 20.0, 20.0, 19.0, 19.0, "EXIT_SIDE_BBO", now_mono=1.0)   # both clear
    s._record_exit_px(BAR, 5.0, 5.0, 4.0, 4.0, "EXIT_SIDE_BBO", now_mono=2.0)       # both miss
    _s("giveback")._record_exit_px(BAR, 16.0, 17.8, 14.0, 14.0, "EXIT_SIDE_BBO", now_mono=3.0)
    assert _gap_events() == []


def test_snapshot_stored_for_trigger_event():
    s = _s()
    s._record_exit_px(BAR, 20.0, 20.0, 19.0, 19.0, "EXIT_SIDE_BBO", now_mono=1.0)
    f = s._exec_px_fields()
    assert f["exec_net_twd"] == pytest.approx(288.0)
    assert f["mark_net_twd"] == pytest.approx(308.0)
    assert f["exit_px_basis"] == "EXIT_SIDE_BBO"


def test_exec_px_fields_safe_before_any_snapshot():
    assert _s()._exec_px_fields() == {}


def test_live_triggered_event_includes_exec_px_fields():
    tree = ast.parse(SRC.read_text())
    found = False
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_append_event"
                and node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "POLICY_J_LIVE_TRIGGERED"):
            for kw in node.keywords:
                if kw.arg is None and "_exec_px_fields" in ast.unparse(kw.value):
                    found = True
    assert found, "POLICY_J_LIVE_TRIGGERED must spread **self._exec_px_fields()"


def test_on_bar_records_exit_px_at_the_real_callsite():
    src = SRC.read_text()
    i = src.index("self._exit_side_pnl(\n                bar, near_close, far_close)")
    assert "self._record_exit_px(" in src[i:i + 900], "_record_exit_px must follow the executable-pnl computation"
