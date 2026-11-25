"""2026-11-25 Hermes Agent: EXIT_LOG.pnl is TRIGGER-time P&L, not realised.

Observed on 2026-11-25 trade mts-auto-165954-628:

    EXIT_LOG  INDEPENDENT_LEG_BRACKET_TAKE_PROFIT  pnl=+100   -> fill +126
    EXIT_LOG  INDEPENDENT_LEG_BRACKET_STOP_LOSS    pnl=-34    -> fill -32

The event is a *decision* record written before submission, so its `pnl` can
never be the realised figure. Reading it as realised under-reported the trade
by 28 points. The basis must be stated in the record itself.
"""
from strategies.plugins.futures.active.tmf_spread import TMFSpread


def _strategy():
    s = TMFSpread.__new__(TMFSpread)
    s._mfe_pts = 0.0
    s._mae_pts = 0.0
    s._near_max = s._near_min = s._far_max = s._far_min = None
    s._post_release_anchor_price = None
    s._mtf_score_at_release = None
    s._get_risk_meta = lambda bar: {}
    s._build_decision_evidence = lambda *a, **k: {}
    return s


def test_exit_log_declares_trigger_basis():
    s = _strategy()
    data = s._log_exit_decision("INDEPENDENT_LEG_BRACKET_TAKE_PROFIT", 100.0, {})
    assert data["pnl"] == 100.0                  # value preserved for consumers
    assert data["pnl_basis"] == "TRIGGER_PTS"    # but the basis is explicit


def test_exit_log_basis_is_constant_across_reasons():
    s = _strategy()
    for reason, pnl in (
        ("INDEPENDENT_LEG_BRACKET_STOP_LOSS", -34.0),
        ("TRAIL_STOP", 12.5),
        ("EMERGENCY_EXIT", -120.0),
    ):
        data = s._log_exit_decision(reason, pnl, {})
        assert data["pnl_basis"] == "TRIGGER_PTS"
        assert data["pnl"] == round(pnl, 2)
