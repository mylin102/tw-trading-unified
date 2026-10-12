"""2026-10-12 Codex: actual init/JSON restore of the cross-day PAPER incident."""
import json

import pytest

from core import runtime_paths
from strategies.plugins.futures.active import tmf_spread as mod
from tests.strategies.test_mts_z_state_roundtrip import state_case
from tests.strategies.test_paper_direction_restore import incident


def aged_case(state_case, monkeypatch, evidence="paper"):
    create, path, ledger, state = incident(state_case, monkeypatch, evidence=evidence)
    state.update(near_side="LONG", far_side="SHORT",
                 _updated="2026-10-08T17:57:09.284694",
                 heartbeat_pid=70248, writer_identity="mts_spread:70248")
    path.write_text(json.dumps(state))
    return create, path, ledger, state


def test_actual_init_restores_aged_attested_paper_position(state_case, monkeypatch):
    create, path, ledger, state = aged_case(state_case, monkeypatch)
    before = ledger.read_bytes()
    strategy = create()  # actual startup init invokes the real restore hook
    assert strategy._has_position is True
    assert strategy._mts_recovery_state is mod.RecoveryState.RECOVERED
    assert strategy._trade_id == state["trade_id"]
    assert (strategy._near_side, strategy._far_side) == ("LONG", "SHORT")
    assert strategy._entry_spread_z == state["entry_spread_z"]
    published = json.loads(path.read_text())
    assert published["writer_identity"] != "mts_spread:70248"
    assert published["trade_id"] == state["trade_id"]
    assert published["lifecycle"] == state["lifecycle"]
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("evidence", ["live", "other_trade", "wrong_price", "wrong_side", "missing", "anomaly"])
def test_aged_restore_never_accepts_unproven_inventory(state_case, monkeypatch, evidence):
    create, path, ledger, _ = aged_case(state_case, monkeypatch,
        evidence if evidence in ("live", "other_trade", "wrong_price") else "paper")
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    if evidence == "wrong_side":
        rows[0]["side"] = "SHORT"
    if evidence == "anomaly":
        rows.append(dict(rows[0], qty=5, fill_type="EXIT"))
    if evidence == "missing":
        ledger.unlink()
    else:
        ledger.write_text("".join(json.dumps(row)+"\n" for row in rows))
    before = path.read_bytes()
    strategy = create()
    assert not strategy._has_position
    assert path.read_bytes() == before


def test_live_does_not_gain_cross_day_paper_restore(state_case, monkeypatch):
    create, path, ledger, _ = aged_case(state_case, monkeypatch)
    monkeypatch.setattr(runtime_paths, "mts_data_mode", lambda: "live")
    before = path.read_bytes(), ledger.read_bytes()
    assert not create()._has_position
    assert (path.read_bytes(), ledger.read_bytes()) == before
