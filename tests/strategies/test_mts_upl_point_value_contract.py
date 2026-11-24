"""2026-11-24 Hermes Agent: regression lock for the near_upl/far_upl TWD contract.

near_upl/far_upl written to MTS telemetry must already be multiplied by the
instrument's point value (TWD), exactly like total_upl and every other writer
(monitor.py, context_builder.py) and like the dashboard's "TWD" labels assume.
A prior bug in TMFSpread.on_bar's live-tick telemetry call passed the two legs
as raw, unmultiplied points while total_upl correctly multiplied — understating
the dashboard UPL cards by the point multiplier (10x for TMF).
"""
from datetime import datetime

from strategies.plugins.futures.active import tmf_spread
from tests.strategies.test_ilb_policy_j_precedence import _build_strategy, _call_on_bar


def _capture_telemetry(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tmf_spread, "_write_mts_telemetry",
        lambda **kwargs: calls.append(kwargs),
    )
    return calls


def test_near_far_upl_are_point_value_multiplied_like_total_upl(tmp_path, monkeypatch):
    s, ctx, _ = _build_strategy(tmp_path, monkeypatch, ilb_enabled=True)
    calls = _capture_telemetry(monkeypatch)

    # NEAR LONG @22000, FAR SHORT @22050; move to 22020/22060 (well inside the
    # +100/-30 bracket so no release/exit fires and telemetry is the live-tick path).
    _call_on_bar(s, ctx, 22020.0, 22060.0, datetime(2026, 11, 24, 9, 10))

    assert calls, "expected at least one _write_mts_telemetry call"
    kw = calls[-1]
    mult = 10.0  # TMF point value
    expected_near = (22020.0 - 22000.0) * mult       # LONG: last - entry
    expected_far = -(22060.0 - 22050.0) * mult        # SHORT: entry - last
    assert kw["near_upl"] == expected_near
    assert kw["far_upl"] == expected_far
    assert kw["total_upl"] == expected_near + expected_far
    # Guard against the regression shape: raw points mistaken for TWD.
    assert kw["near_upl"] != 22020.0 - 22000.0
    assert kw["far_upl"] != -(22060.0 - 22050.0)
