"""2026-10-08 Codex: real JSON restore must not accept directionless PAPER legs."""
import json

import pytest

from core import runtime_paths
from strategies.plugins.futures.active import tmf_spread as mod
from tests.strategies.test_mts_z_state_roundtrip import state_case, enter


def incident(state_case, monkeypatch, *, evidence="paper", sides=None):
    create, path, _ = state_case
    strategy = enter(create, -5.89, "mts-auto-151001-499")
    state = json.loads(path.read_text())
    state.update(near_side=sides, far_side=sides, current_spread_z=-16.68,
                 spread_z=-16.68)
    path.write_text(json.dumps(state))
    rows = [dict(trade_id=state["trade_id"], leg=leg, side=side, qty=1,
                 price=price, fill_type="ENTRY", mode=evidence,
                 timestamp="2026-10-06T15:10:01.811239")
            for leg, side, price in (("NEAR", "LONG", 49854.),
                                      ("FAR", "SHORT", 50035.))]
    if evidence == "other_trade":
        for row in rows:
            row.update(trade_id="UNRELATED", mode="paper")
    if evidence == "wrong_price":
        rows[0].update(price=1, mode="paper")
        rows[1]["mode"] = "paper"
    ledger = path.parent / "fills.jsonl"
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows))
    monkeypatch.setattr(runtime_paths, "mts_data_mode", lambda: "paper")
    monkeypatch.setattr(mod, "_abandoned_trade_ids", lambda: set())
    return create, path, ledger, state


@pytest.mark.parametrize("missing", [None, "", "INVALID"])
def test_real_restore_recovers_paper_directions_and_preserves_trade(state_case, monkeypatch, missing):
    create, path, ledger, old = incident(state_case, monkeypatch, sides=missing)
    before = ledger.read_bytes()
    strategy = create()
    assert strategy._restore_position_state() is True
    assert (strategy._near_side, strategy._far_side) == ("LONG", "SHORT")
    assert strategy._trade_id == old["trade_id"]
    assert strategy._entry_spread_z == -5.89
    repaired = json.loads(path.read_text())
    assert (repaired["near_side"], repaired["far_side"]) == ("LONG", "SHORT")
    assert repaired["entry_spread_z"] == old["entry_spread_z"]
    assert repaired["lifecycle"] == old["lifecycle"]
    assert repaired["has_position"] is True
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("evidence", ["live", "other_trade", "wrong_price"])
def test_paper_restore_refuses_unmatched_direction_evidence(state_case, monkeypatch, evidence):
    create, path, ledger, _ = incident(state_case, monkeypatch, evidence=evidence)
    before = (path.read_bytes(), ledger.read_bytes())
    strategy = create()
    assert strategy._restore_position_state() is False
    assert not strategy._has_position
    assert (path.read_bytes(), ledger.read_bytes()) == before


def test_live_restore_does_not_take_paper_direction_evidence(state_case, monkeypatch):
    create, path, ledger, _ = incident(state_case, monkeypatch)
    monkeypatch.setattr(runtime_paths, "mts_data_mode", lambda: "live")
    before = (path.read_bytes(), ledger.read_bytes())
    strategy = create()
    strategy._restore_position_state()
    assert strategy._near_side is None and strategy._far_side is None
    assert (path.read_bytes(), ledger.read_bytes()) == before
