#!/usr/bin/env python3
"""Replay ENGINE contract tests — RED first (research-only).

The engine path (run_replay non-dry-run + --authorize) reuses the
CANONICAL clone/execution/classify/A4 pieces — never a duplicate
classifier. Arm PnL requires the event's entries + release_leg; any
missing data fails closed to INDETERMINATE_DATA_QUALITY (no fabricated
Y values).
"""

import json

import pytest

from scripts.research.phase_transition_replay import engine, run_replay
from scripts.research.phase_transition_replay import classify


def _full_event(seq=1, with_entries=True):
    ev = {
        "source_event_seq": seq,
        "exchange_ts": 1786183190000,
        "recv_ts": 1786183190050,
        "decision_ts_ms": 1786183190000,
        "release_leg": "near",
        "quotes": {
            "near": {"bid": 50.0, "ask": 52.0, "age_s": 0.05,
                     "close_action": "LONG", "quote_exchange_ts": 1786183190000},
            "far": {"bid": 25.0, "ask": 27.0, "age_s": 0.05,
                    "close_action": "SHORT", "quote_exchange_ts": 1786183190000},
        },
    }
    if with_entries:
        ev["entries"] = {
            "near": {"price": 45.0, "qty": 2},
            "far": {"price": 30.0, "qty": 2},
        }
    return ev


def _full_event_beneficial(seq=1):
    """Y0=Y3=700 (near LONG closes at bid 120, qty 10), Y1=500, Y2=0 ->
    F_R=[650,750] vs F_N=[500,600] -> RELEASE_BENEFICIAL under M=25
    (with the fee-based residual intervals)."""
    ev = _full_event(seq)
    ev["quotes"]["near"]["bid"] = 120.0
    ev["quotes"]["far"]["ask"] = 15.0
    ev["entries"]["near"]["qty"] = 10
    ev["entries"]["far"]["qty"] = 10
    return ev


FEE = {"fee_assumptions": {"fee-v1": {"per_leg": 50.0, "slippage_ticks": 1}},
       "m_economic": 25.0}


# ── engine.arm_pnl unit ───────────────────────────────────────────────────────

def test_engine_arm_pnl_y0_y3():
    # near LONG closes at bid 50; far SHORT closes at ask 27
    # Y0 (release near only): (50-45)*2 - fee = 10 - 50 = -40
    # Y1 (both legs): (50-45)*2 + (27-30)*2 - 2*fee = 10 - 6 - 100 = -96
    # Y2 (remain): 0
    # Y3 (release + controller, near): same closed-leg economics as Y0
    result = engine.arm_pnl(_full_event(), FEE)
    assert result[0] == "ok", result
    arms = result[1]["arms"]
    assert arms["Y0"] == pytest.approx(-40.0), arms
    assert arms["Y1"] == pytest.approx(-96.0), arms
    assert arms["Y2"] == pytest.approx(0.0), arms
    assert arms["Y3"] == pytest.approx(-40.0), arms


def test_engine_six_pairwise_deltas():
    result = engine.arm_pnl(_full_event(), FEE)
    assert result[0] == "ok", result
    deltas = result[1]["pairwise_deltas"]
    pairs = {("Y0", "Y1"), ("Y0", "Y2"), ("Y0", "Y3"),
             ("Y1", "Y2"), ("Y1", "Y3"), ("Y2", "Y3")}
    assert set(deltas) == pairs, deltas
    assert all(isinstance(v, float) for v in deltas.values()), deltas


def test_engine_arm_pnl_fail_closed_missing_entries():
    result = engine.arm_pnl(_full_event(with_entries=False), FEE)
    assert result[0] == "INDETERMINATE_DATA_QUALITY", result
    assert "entries" in result[1].lower(), result


def test_engine_arm_pnl_fail_closed_missing_release_leg():
    ev = _full_event()
    del ev["release_leg"]
    result = engine.arm_pnl(ev, FEE)
    assert result[0] == "INDETERMINATE_DATA_QUALITY", result


def test_engine_reuses_canonical_classifier(monkeypatch):
    called = {}

    def spy(*a, **k):
        called["ok"] = True
        return "RELEASE_BENEFICIAL"

    monkeypatch.setattr(classify, "classify_outcome", spy)
    result = engine.arm_pnl(_full_event(), FEE)
    assert result[0] == "ok", result
    assert result[1]["classification"] == "RELEASE_BENEFICIAL"
    assert called.get("ok") is True, \
        "engine must call the CANONICAL classifier, never duplicate it"


# ── run_replay --authorize engine path ────────────────────────────────────────

def test_engine_authorize_runs_and_outputs(tmp_path):
    inp = tmp_path / "events.json"
    inp.write_text(json.dumps([_full_event_beneficial(1),
                               _full_event_beneficial(2)]),
                   encoding="utf-8")
    out = tmp_path / "out"
    rc = run_replay.main(["--input", str(inp), "--out-dir", str(out),
                          "--prereg", "prereg-v1", "--authorize"])
    assert rc == 0, rc
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert m["engine_run"] is True, m
    assert m["dry_run"] is False, m
    eng = m["engine"]
    assert eng["n_eligible"] == 2, eng
    assert eng["n_fail_closed"] == 0, eng
    assert set(eng["classifications"]) == {"RELEASE_BENEFICIAL"}, eng
    assert len(eng["pairwise_deltas"]) == 6, eng
    assert len(eng["arms"]) == 4, eng


def test_engine_authorize_fail_closed_reported(tmp_path):
    # events WITHOUT entries -> engine reports INDETERMINATE, zero
    # fabricated arms
    ev = _full_event(1, with_entries=False)
    inp = tmp_path / "events.json"
    inp.write_text(json.dumps([ev]), encoding="utf-8")
    out = tmp_path / "out"
    rc = run_replay.main(["--input", str(inp), "--out-dir", str(out),
                          "--prereg", "prereg-v1", "--authorize"])
    assert rc == 0, rc
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    eng = m["engine"]
    assert eng["n_eligible"] == 0, eng
    assert eng["n_fail_closed"] == 1, eng
    assert eng["arms"] == {}, "no fabricated arm values"
    assert eng["pairwise_deltas"] == {}, "no fabricated deltas"
    assert "entries" in eng["fail_closed_reasons"][0].lower(), eng


def test_engine_authorize_still_requires_prereg(tmp_path):
    inp = tmp_path / "events.json"
    inp.write_text(json.dumps([]), encoding="utf-8")
    out = tmp_path / "out"
    # --prereg is a REQUIRED committed selector — argparse refuses
    with pytest.raises(SystemExit):
        run_replay.main(["--input", str(inp), "--out-dir", str(out),
                         "--authorize"])
    assert not out.exists(), "zero output on refusal"


def test_engine_non_authorize_still_refused(tmp_path):
    # non-dry-run WITHOUT --authorize remains a REFUSED (exit 3)
    inp = tmp_path / "events.json"
    inp.write_text(json.dumps([]), encoding="utf-8")
    out = tmp_path / "out"
    rc = run_replay.main(["--input", str(inp), "--out-dir", str(out),
                          "--prereg", "prereg-v1"])
    assert rc == 3, rc
    assert not (out / "manifest.json").exists(), "zero output on refusal"
