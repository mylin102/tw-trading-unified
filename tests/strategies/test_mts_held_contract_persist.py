"""N2: the state file must carry the contracts the position was opened on.

2026-10-22: release23 restarted while a paper spread was open.  The state file
had has_position=True but near_contract/far_contract = None, so N1's restart
hydration had to fall back to ENTRY_AUDIT.  The periodic sync (write_state) is
what overwrites the file every tick; it must persist the held pair.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from strategies.plugins.futures.active import tmf_spread as mod


def _strategy(near=None, far=None, has_position=True):
    s = MagicMock()
    s._has_position = has_position
    s._trade_id = "N2-TRADE"
    s._near_entry = 46316.0
    s._far_entry = 46446.0
    s._near_side = "SHORT"
    s._far_side = "LONG"
    s._released_leg = None
    s._release_price = 0.0
    s._ticker = "TMF"
    s._last_atr = 70.0
    s._peak_net_exit_pnl_twd = 0.0
    s._entry_spread_z = -3.1
    s._held_near_contract = near
    s._held_far_contract = far
    s._current_lifecycle_state.return_value = {}
    return s


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def _fake(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(mod, "_write_mts_state", _fake)
    return calls


def test_sync_persists_the_held_pair(captured):
    s = _strategy(SimpleNamespace(code="TMFK6", delivery_date="2026/11/18"),
                  SimpleNamespace(code="TMFL6", delivery_date="2026/12/16"))
    mod.TMFSpread.write_state(s, "SYNC", "near_pnl=1.0", near_last=46400.0, far_last=46500.0)
    assert captured, "write_state did not call the state writer"
    kw = captured[-1]
    assert kw["near_contract"] == "TMFK6"
    assert kw["far_contract"] == "TMFL6"
    assert kw["near_delivery_date"] == "2026/11/18"
    assert kw["far_delivery_date"] == "2026/12/16"


def test_explicit_codes_win_over_the_held_pair(captured):
    s = _strategy(SimpleNamespace(code="TMFK6", delivery_date="2026/11/18"),
                  SimpleNamespace(code="TMFL6", delivery_date="2026/12/16"))
    mod.TMFSpread.write_state(s, "SYNC", "x", near_contract="TMFJ6", far_contract="TMFK6")
    kw = captured[-1]
    assert kw["near_contract"] == "TMFJ6"
    assert kw["far_contract"] == "TMFK6"


def test_unknown_held_pair_stays_none(captured):
    s = _strategy(None, None)
    mod.TMFSpread.write_state(s, "SYNC", "x")
    kw = captured[-1]
    assert kw["near_contract"] is None
    assert kw["far_contract"] is None
