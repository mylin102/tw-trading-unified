"""2026-10-06 Codex: real Oct-5 shape through incremental monitor authority."""
import json
from types import SimpleNamespace

import pytest

from strategies.futures.monitor import FuturesMonitor
from strategies.futures.mts_ledger_authority import (
    MtsAuthority, MtsLedgerProjection, project_fills,
)


def incident_rows():
    # The ten actual timestamps/PF identities differ; ordinary receipt dedup
    # cannot solve this. Null-valued provenance fields remain present.
    shapes = [
        ("13:40:00.623279", "NEAR", "LONG", 1, 49915, "ENTRY", "623318"),
        ("13:40:00.639931", "FAR", "SHORT", 1, 50094, "ENTRY", "639952"),
        ("16:49:41.491958", "SETTLED", "", 0, 0, "COMBINED_EXIT_SETTLED", "491979"),
        ("16:49:41.492124", "SETTLED", "", 0, 0, "COMBINED_EXIT_SETTLED", "492136"),
        ("16:49:41.496116", "NEAR", "SELL", 1, 50068, "COMBINED_EXIT", "496128"),
        ("16:49:41.496261", "NEAR", "SELL", 1, 50068, "COMBINED_EXIT", "496271"),
        ("16:49:41.498056", "FAR", "BUY", 1, 50264, "COMBINED_EXIT", "498066"),
        ("16:49:41.498203", "FAR", "BUY", 1, 50264, "COMBINED_EXIT", "498213"),
        ("16:49:41.499912", "NEAR", "NONE", 0, 0, "COMBINED_EXIT_COMPLETED", "499921"),
        ("16:49:41.500052", "NEAR", "NONE", 0, 0, "COMBINED_EXIT_COMPLETED", "500061"),
    ]
    rows = []
    for ts, leg, side, qty, price, ft, micro in shapes:
        entry = ft == "ENTRY"
        row = dict(timestamp="2026-10-05T" + ts, ticker="TMF", leg=leg,
                   contract=leg if entry else "TMFJ6" if leg == "NEAR" else "TMFK6" if leg == "FAR" else "",
                   side=side, qty=qty, price=float(price), fill_type=ft,
                   trade_id="mts-auto-134000-323", mode="paper",
                   execution_source="PAPER_COMBO_FILL" if entry or ft.endswith("COMPLETED") else "PAPER_EXIT_FILL",
                   paper_fill_id="PF-20261005-" + ts[:8].replace(":", "") + "-" + micro + "-" + leg,
                   parent_combo_order_id="PAPER-COMBO-mts-auto-134000-323",
                   leg_role=leg, expected_qty=qty, filled_qty=qty,
                   fill_status="FILLED", fill_price=float(price),
                   attribution_key="mts-auto-134000-323:" + leg + ":" + ft,
                   session_id="20261005_102748" if entry else "20261005_150052",
                   generation_id="78decc9aa941bda8" if entry else "f80bef957d46ed20",
                   release_group_id="MTS-RG-mts-auto-134000-323",
                   session="day" if entry else "night",
                   price_source="PAPER_FILL" if ft == "COMBINED_EXIT" else "UNSET",
                   position_effect="CLOSE" if ft == "COMBINED_EXIT" else None,
                   position_side_before_exit=("LONG" if leg == "NEAR" else "SHORT") if ft == "COMBINED_EXIT" else None)
        row.update({k: None for k in ("settlement_status", "settled_from", "quote_age_ms", "near_pnl", "far_pnl", "spread_pnl")})
        rows.append(row)
    return rows


def monitor_for(tmp_path, monkeypatch, rows):
    path = tmp_path / "fills.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    mon = FuturesMonitor.__new__(FuturesMonitor)
    mon._ledger_projection = MtsLedgerProjection(str(path), source="PAPER")
    monkeypatch.setattr("strategies.plugins.futures.active.tmf_spread._abandoned_trade_ids", lambda: set())
    return mon, path


def test_duplicate_paper_close_cannot_reconstruct_reverse_spread(tmp_path, monkeypatch):
    rows = incident_rows()
    assert len({r["paper_fill_id"] for r in rows}) == 10
    mon, path = monitor_for(tmp_path, monkeypatch, rows)
    assert mon._mts_has_open_position_from_fills() is False
    auth = mon._ledger_projection.snapshot()
    assert auth.status is MtsAuthority.FLAT
    assert (auth.near_qty, auth.far_qty) == (0, 0)
    assert auth.anomalies  # retain anomaly evidence, do not edit the ledger
    assert project_fills(rows) == auth
    assert len(path.read_text().splitlines()) == 10


def test_duplicate_near_close_does_not_erase_unclosed_far(tmp_path, monkeypatch):
    rows = [r for r in incident_rows() if r["fill_type"] == "ENTRY"
            or (r["fill_type"] == "COMBINED_EXIT" and r["leg"] == "NEAR")]
    mon, _ = monitor_for(tmp_path, monkeypatch, rows)
    assert mon._mts_has_open_position_from_fills() is True
    auth = mon._ledger_projection.snapshot()
    assert (auth.near_qty, auth.far_qty) == (0, -1)
    writes = []
    monkeypatch.setattr("strategies.plugins.futures.active.tmf_spread._write_mts_state", lambda **kw: writes.append(kw))
    mon.ticker = "TMF"
    strategy = SimpleNamespace(_released_leg=None, _lifecycle_oca=None)
    mon._reconstruct_position_from_ledger(strategy, auth)
    assert strategy._near_side is None
    assert strategy._far_side == "SHORT"
    assert writes[-1]["has_position"] is True


@pytest.mark.parametrize("source", ["PAPER", "LIVE"])
def test_legitimate_identical_price_partial_closes_are_not_deduplicated(source):
    rows = [r for r in incident_rows() if r["fill_type"] == "ENTRY"
            or r["fill_type"] == "COMBINED_EXIT"]
    for row in rows:
        if row["fill_type"] == "ENTRY":
            row["qty"] = 3
    projection = MtsLedgerProjection(source=source)
    for row in rows:
        projection.apply_fill(row)
    auth = projection.snapshot()
    assert (auth.near_qty, auth.far_qty) == (1, -1)
    assert auth.anomalies == ()


def test_completed_marker_without_leg_closes_is_not_flat(tmp_path, monkeypatch):
    rows = [r for r in incident_rows() if r["fill_type"] in ("ENTRY", "COMBINED_EXIT_COMPLETED")]
    mon, _ = monitor_for(tmp_path, monkeypatch, rows)
    assert mon._mts_has_open_position_from_fills() is True
    assert mon._ledger_projection.snapshot().near_qty == 1
