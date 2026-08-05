# Model C Canary collector tests (2026-08-03).
import json
import os
import tempfile

import pytest

from core.model_c_collector import ModelCCollector


def _mk(tmp_path):
    import os as _os
    p = _os.path.join(str(tmp_path), "model_c.jsonl")
    raw = _os.path.join(str(tmp_path), "bbo_raw.jsonl")
    return ModelCCollector(p, bbo_raw_path=raw), p


def _q(collector, leg, bid, ask, age_ms=100, skew_shift=None, ts=None, sizes=(1, 1),
      exchange_ts=None):
    """Inject a quote with controlled receive timestamps (ISO).

    2026-08-04 envelope contract: quote_age_ms requires a VALID exchange ts
    (same UTC epoch contract). Pass exchange_ts to exercise the age path;
    leave None to exercise EXCHANGE_TS_UNAVAILABLE semantics.
    """
    import datetime
    base = datetime.datetime.now()
    _age = age_ms if age_ms is not None else 0
    rts = (base - datetime.timedelta(milliseconds=_age)).isoformat()
    if skew_shift and skew_shift == leg:
        rts = (base - datetime.timedelta(milliseconds=_age + 1000)).isoformat()
    if exchange_ts is None and age_ms is not None:
        # exchange ts at receive time minus age_ms (quote already old on arrival)
        exchange_ts = base - datetime.timedelta(milliseconds=2 * age_ms)
    return collector.on_quote(leg, bid, ask, bid_size=sizes[0], ask_size=sizes[1],
                              receive_ts=rts, exchange_ts=exchange_ts,
                              contract_code=f"TMF{leg}")


def _read(p):
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p) if l.strip()]


def test_long_uses_bid_for_exit_price():
    c, p = _mk(tempfile.mkdtemp())
    _q(c, "NEAR", 100.0, 102.0)
    _q(c, "FAR", 200.0, 202.0)
    c.mark_position("LONG", "SHORT", 95.0, 205.0, 1, 1)
    acc = c.latest_accepted
    assert acc["near_executable_exit_price"] == 100.0  # LONG -> bid
    assert acc["far_executable_exit_price"] == 202.0   # SHORT -> ask


def test_short_uses_ask_for_exit_price():
    c, p = _mk(tempfile.mkdtemp())
    _q(c, "NEAR", 100.0, 102.0)
    _q(c, "FAR", 200.0, 202.0)
    c.mark_position("SHORT", "LONG", 105.0, 195.0, 1, 1)
    acc = c.latest_accepted
    assert acc["near_executable_exit_price"] == 102.0  # SHORT -> ask
    assert acc["far_executable_exit_price"] == 200.0   # LONG -> bid


def test_pnl_sign_near_short():
    c, p = _mk(tempfile.mkdtemp())
    _q(c, "NEAR", 100.0, 102.0)
    _q(c, "FAR", 200.0, 202.0)
    c.mark_position("SHORT", "LONG", 105.0, 195.0, 1, 1, point_value=10)
    acc = c.latest_accepted
    # SHORT: (entry - ask) * qty * pv = (105-102)*10 = +30
    assert acc["near_executable_gross_pnl"] == pytest.approx(30.0)


def test_combined_equals_legs_sum():
    c, p = _mk(tempfile.mkdtemp())
    _q(c, "NEAR", 100.0, 102.0)
    _q(c, "FAR", 200.0, 202.0)
    c.mark_position("LONG", "SHORT", 95.0, 205.0, 1, 1, point_value=10)
    acc = c.latest_accepted
    assert acc["executable_combined_gross_pnl"] == pytest.approx(
        acc["near_executable_gross_pnl"] + acc["far_executable_gross_pnl"])


def test_missing_far_bbo_rejected():
    c, p = _mk(tempfile.mkdtemp())
    _q(c, "NEAR", 100.0, 102.0)
    rec = c.latest_pair
    assert rec["event_type"] == "MODEL_C_PAIR_REJECTED"
    assert rec["reason"] == "FAR_BBO_MISSING"


def test_stale_far_rejected():
    c, p = _mk(tempfile.mkdtemp())
    _q(c, "NEAR", 100.0, 102.0, age_ms=100)
    _q(c, "FAR", 200.0, 202.0, age_ms=5000)  # > 2000 max age
    assert c.latest_pair["reason"] == "FAR_STALE"


def test_pair_skew_rejected():
    c, p = _mk(tempfile.mkdtemp())
    _q(c, "NEAR", 100.0, 102.0, age_ms=100)
    _q(c, "FAR", 200.0, 202.0, age_ms=100, skew_shift="FAR")  # +2000ms skew
    assert c.latest_pair["reason"] == "PAIR_SKEW_EXCEEDED"


def test_invalid_crossed_book_rejected():
    c, p = _mk(tempfile.mkdtemp())
    _q(c, "NEAR", 105.0, 102.0)  # crossed: bid > ask
    _q(c, "FAR", 200.0, 202.0)
    assert c.latest_pair["reason"] == "INVALID_NEAR_BOOK"


def test_quote_age_from_receive_ts():
    c, p = _mk(tempfile.mkdtemp())
    _q(c, "NEAR", 100.0, 102.0, age_ms=100)
    _q(c, "FAR", 200.0, 202.0, age_ms=100)
    # exchange ts = receive - age_ms -> quote_age ~= age_ms (100ms)
    assert c.latest_accepted["near_quote_age_ms"] == pytest.approx(100, abs=50)
    assert c.latest_accepted["near_timestamp_quality"] == "VALID"


def test_exchange_ts_missing_marks_receive_only():
    c, p = _mk(tempfile.mkdtemp())
    _q(c, "NEAR", 100.0, 102.0, exchange_ts=None, age_ms=None)
    _q(c, "FAR", 200.0, 202.0, exchange_ts=None, age_ms=None)
    acc = c.latest_accepted
    assert acc["near_exchange_ts"] is None  # no exchange ts -> RECEIVE_ONLY semantics
    assert acc["near_timestamp_quality"] == "EXCHANGE_TS_UNAVAILABLE"
    assert acc["near_quote_age_ms"] is None  # no fabricated age


def test_episode_not_inflated_by_attempts():
    c, p = _mk(tempfile.mkdtemp())
    for _ in range(5):
        _q(c, "NEAR", 100.0, 102.0, age_ms=100)   # far missing
    rejs = [r for r in _read(p) if r.get("event_type") == "MODEL_C_PAIR_REJECTED"]
    ep_ids = {r["episode_id"] for r in rejs}
    assert len(ep_ids) == 1, "same stale state must be ONE episode"
    assert c.counters["episodes"] == 1


def test_stale_update_creates_new_episode():
    c, p = _mk(tempfile.mkdtemp())
    _q(c, "FAR", 200.0, 202.0, age_ms=5000)  # stale far (missing near)
    _q(c, "NEAR", 100.0, 102.0, age_ms=100)
    ep1 = c.latest_pair["episode_id"]
    # far updates (still stale vs near fresh)
    _q(c, "FAR", 199.0, 201.0, age_ms=5000)
    ep2 = c.latest_pair["episode_id"]
    assert ep2 != ep1, "stale quote update must create new episode"


def test_accepted_pairs_are_shadow_only():
    c, p = _mk(tempfile.mkdtemp())
    _q(c, "NEAR", 100.0, 102.0)
    _q(c, "FAR", 200.0, 202.0)
    acc = c.latest_accepted
    assert acc["shadow_only"] is True
    assert acc["model_version"] == "MODEL_C_V1"


def test_null_never_zero():
    c, p = _mk(tempfile.mkdtemp())
    _q(c, "NEAR", 100.0, 102.0)
    rec = c.latest_pair  # rejected (far missing)
    assert "near_executable_gross_pnl" not in rec or rec["near_executable_gross_pnl"] is None
    # accepted without position -> executable pnl must be None, not 0
    _q(c, "FAR", 200.0, 202.0)
    acc = c.latest_accepted
    assert acc["executable_combined_gross_pnl"] is None or acc["near_executable_gross_pnl"] is None


def test_no_unknown_reasons():
    c, p = _mk(tempfile.mkdtemp())
    _q(c, "NEAR", 100.0, 102.0, age_ms=50)
    _q(c, "FAR", 200.0, 202.0, age_ms=6000)
    _q(c, "NEAR", 101.0, 103.0, age_ms=7000)
    rejs = [r for r in _read(p) if r.get("event_type") == "MODEL_C_PAIR_REJECTED"]
    for r in rejs:
        assert r["reason"] not in ("UNKNOWN", "OTHER", "INVALID")
        assert r["reason"] in ("NEAR_BBO_MISSING", "FAR_BBO_MISSING", "NEAR_STALE",
                               "FAR_STALE", "BOTH_STALE", "PAIR_SKEW_EXCEEDED",
                               "INVALID_NEAR_BOOK", "INVALID_FAR_BOOK",
                               "TIMESTAMP_MISSING", "POSITION_STATE_INCOMPLETE")


def test_bbo_raw_telemetry_written():
    c, p = _mk(tempfile.mkdtemp())
    _q(c, "NEAR", 100.0, 102.0)
    raw_path = c._bbo_raw_path
    assert os.path.exists(raw_path)
    rows = [json.loads(l) for l in open(raw_path) if l.strip()]
    assert rows[0]["event_type"] == "BBO_UPDATE"
    assert rows[0]["leg"] == "NEAR"


def test_mark_position_sets_executable_pnl():
    c, p = _mk(tempfile.mkdtemp())
    _q(c, "NEAR", 100.0, 102.0)
    _q(c, "FAR", 200.0, 202.0)
    c.mark_position("LONG", "SHORT", 95.0, 205.0, 1, 1, point_value=10)
    acc = c.latest_accepted
    assert acc["near_executable_gross_pnl"] == pytest.approx(50.0)   # (100-95)*10
    assert acc["far_executable_gross_pnl"] == pytest.approx(30.0)    # (205-202)*10
    assert acc["executable_combined_gross_pnl"] == pytest.approx(80.0)


def test_negative_exchange_age_downgrades_quality():
    """TAIFEX server clock leads Mini (~85ms systematic). A negative
    exchange_quote_age_ms must NOT be clamped to 0 (would fake freshness) —
    quality downgrades to EXCHANGE_TS_CLOCK_DOMAIN_UNKNOWN and age is null."""
    import datetime
    c, p = _mk(tempfile.mkdtemp())
    base = datetime.datetime.now()
    # exchange ts 100ms AHEAD of receive ts (server clock leads)
    x_ts = base + datetime.timedelta(milliseconds=100)
    _q(c, "NEAR", 100.0, 102.0, age_ms=0, exchange_ts=x_ts)
    _q(c, "FAR", 200.0, 202.0, age_ms=0, exchange_ts=x_ts)
    acc = c.latest_accepted
    assert acc["near_timestamp_quality"] == "EXCHANGE_TS_CLOCK_DOMAIN_UNKNOWN"
    assert acc["far_timestamp_quality"] == "EXCHANGE_TS_CLOCK_DOMAIN_UNKNOWN"
    assert acc["near_quote_age_ms"] is None
    assert acc["far_quote_age_ms"] is None
    # exchange skew still computable (observation domain, not freshness)
    assert acc["exchange_pair_skew_ms"] is not None


def test_positive_exchange_age_keeps_valid():
    """exchange ts behind receive ts (normal latency) -> VALID + positive age."""
    import datetime
    c, p = _mk(tempfile.mkdtemp())
    base = datetime.datetime.now()
    x_ts = base - datetime.timedelta(milliseconds=50)
    _q(c, "NEAR", 100.0, 102.0, age_ms=0, exchange_ts=x_ts)
    _q(c, "FAR", 200.0, 202.0, age_ms=0, exchange_ts=x_ts)
    acc = c.latest_accepted
    assert acc["near_timestamp_quality"] == "VALID"
    assert acc["near_quote_age_ms"] == pytest.approx(50, abs=20)


# ── 2026-08-05 bounded observation ──────────────────────────────────────

def test_sample_rate_zero_drops_normal_keeps_anomaly(tmp_path):
    import datetime
    c, p = _mk(str(tmp_path))
    c.sample_rate = 0.0
    # normal accepted pair (no anomaly) -> sampled out
    _q(c, "NEAR", 100.0, 102.0, age_ms=None, exchange_ts=None)
    _q(c, "FAR", 200.0, 202.0, age_ms=None, exchange_ts=None)
    acc = c.latest_accepted
    recs = _read(p)
    assert c.counters["accepted"] == 1          # counters always accumulate
    assert c.counters["sampled_out"] == 1       # sampled out
    assert all(r.get("event_type") != "MODEL_C_PAIR_ACCEPTED" for r in recs)  # not written


def test_anomaly_always_written(tmp_path):
    import datetime
    c, p = _mk(str(tmp_path))
    c.sample_rate = 0.0
    # force a rejection (near missing)
    _q(c, "NEAR", 100.0, 102.0, age_ms=None, exchange_ts=None)
    _q(c, "NEAR", 105.0, 107.0, age_ms=None, exchange_ts=None)  # far never sent
    rejs = [r for r in _read(p) if r.get("event_type") == "MODEL_C_PAIR_REJECTED"]
    assert rejs, "rejection must be written even at sample_rate=0"
    assert c.counters["anomaly_written"] >= 1


def test_daily_cap_enforced(tmp_path):
    c, p = _mk(str(tmp_path))
    c.max_records_per_day = 3
    c.sample_rate = 1.0
    for i in range(6):
        _q(c, "NEAR", 100.0 + i, 102.0 + i, age_ms=10)
        _q(c, "FAR", 200.0 + i, 202.0 + i, age_ms=10)
    recs = [r for r in _read(p) if r.get("event_type") == "MODEL_C_PAIR_ACCEPTED"]
    assert len(recs) <= 3, f"daily cap breached: {len(recs)}"


def test_full_capture_flag_overrides_sampling(tmp_path):
    c, p = _mk(str(tmp_path))
    c.sample_rate = 0.0
    flag = os.path.join(str(tmp_path), "full.flag")
    c._full_capture_flag = flag
    open(flag, "w").close()
    _q(c, "NEAR", 100.0, 102.0, age_ms=None, exchange_ts=None)
    _q(c, "FAR", 200.0, 202.0, age_ms=None, exchange_ts=None)
    recs = [r for r in _read(p) if r.get("event_type") == "MODEL_C_PAIR_ACCEPTED"]
    assert recs, "full-capture flag must force write despite sample_rate=0"
