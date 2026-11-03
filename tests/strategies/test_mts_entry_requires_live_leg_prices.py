"""2026-11-03 Hermes Agent: never enter a spread on a mixed stale/live pair.

Incident (Mini PAPER, trade mts-auto-151203-918, 15:12:03 right after a
restart): ENTRY_AUDIT shows near_price 43230 source BAR_CLOSE
(near_tick_age_ms null) vs far_price 43220 source LIVE_TICK. Live BBO at the
same moment was near 43124/43129, far 43207/43220 (executable_spread -96,
spread_ma -86.25). The stale near bar close made spread_now +10 -> spread_z
15.86 (true z ~ -1.6): a phantom signal that opened a position.

Contract: entry is blocked when either leg's price source is BAR_CLOSE.
A missing source field (backtest/legacy bars) stays permissive, matching the
existing marks_live semantics.
"""
from pathlib import Path

import pytest

import strategies.plugins.futures.active.tmf_spread as tmf

SRC = Path(tmf.__file__).read_text()


@pytest.fixture
def s():
    return object.__new__(tmf.TMFSpread)


def test_incident_pair_is_blocked(s):
    ok, reason = s._entry_price_sources_live(
        {"near_price_source": "BAR_CLOSE", "far_price_source": "LIVE_TICK"})
    assert ok is False
    assert reason == "STALE_LEG_PRICE_SOURCE:near=BAR_CLOSE,far=LIVE_TICK"


@pytest.mark.parametrize("n,f", [("LIVE_TICK", "BAR_CLOSE"), ("BAR_CLOSE", "BAR_CLOSE")])
def test_any_bar_close_leg_blocks(s, n, f):
    assert s._entry_price_sources_live({"near_price_source": n, "far_price_source": f})[0] is False


def test_both_live_allowed(s):
    assert s._entry_price_sources_live(
        {"near_price_source": "LIVE_TICK", "far_price_source": "LIVE_TICK"}) == (True, None)


def test_missing_source_fields_stay_permissive(s):
    assert s._entry_price_sources_live({}) == (True, None)


def test_gate_is_wired_into_on_bar_before_entry_direction():
    i_extreme = SRC.index('skip_reason="SPREAD_Z_NOT_EXTREME"')
    i_gate = SRC.index("self._entry_price_sources_live(bar)")
    i_direction = SRC.index("# ── Direction-aware entry ──")
    assert i_extreme < i_gate < i_direction, \
        "price-source gate must run after the z check and before any entry is built"


def test_blocked_entry_is_recorded():
    i_gate = SRC.index("self._entry_price_sources_live(bar)")
    window = SRC[i_gate:i_gate + 800]
    assert '"ENTRY_BLOCKED"' in window
    assert "skip_reason=" in window
