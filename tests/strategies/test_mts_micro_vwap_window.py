"""VWAP window-filtering regression tests (D3/D7): the micro VWAP must only use
samples within [decision_ts - window_secs, decision_ts] and fail closed when
the window is empty or the newest sample is stale."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from strategies.plugins.futures.active import mts_micro_vwap as V


def _sample(ts_epoch_s, price, vol=5.0):
    return {"ts": ts_epoch_s, "price": price, "volume": vol}


def test_vwap_filters_outside_window():
    # decision at t=10000; window 900s -> only [9100, 10000] kept
    decision_ts = 10000.0
    samples = [
        _sample(8000.0, 100.0),   # before window -> excluded
        _sample(9500.0, 110.0),
        _sample(9800.0, 112.0),
        _sample(9900.0, 113.0),
        _sample(10000.0, 114.0),
    ]
    r = V.compute_micro_vwap(decision_ts, samples, atr_1m=2.0)
    assert r.samples_missing is False
    assert r.n_samples_in_window == 4          # 9500/9800/9900/10000
    assert r.last_price == 114.0


def test_vwap_empty_window_fail_closed():
    # all samples older than window -> UNKNOWN
    decision_ts = 10000.0
    samples = [_sample(7000.0, 100.0), _sample(8000.0, 105.0)]
    r = V.compute_micro_vwap(decision_ts, samples, atr_1m=2.0)
    assert r.samples_missing is True
    assert r.deviation_status == V.VwapDeviation.UNKNOWN


def test_vwap_stale_newest_fail_closed():
    # newest sample 120s before decision (max age 60s) -> fail closed
    decision_ts = 10000.0
    samples = [_sample(9700.0, 110.0), _sample(9880.0, 111.0)]
    r = V.compute_micro_vwap(decision_ts, samples, atr_1m=2.0,
                             max_sample_age_secs=60.0)
    assert r.samples_missing is True
    assert r.deviation_status == V.VwapDeviation.UNKNOWN


def test_vwap_fresh_newest_ok():
    decision_ts = 10000.0
    samples = [_sample(9700.0, 110.0), _sample(9980.0, 111.0)]
    r = V.compute_micro_vwap(decision_ts, samples, atr_1m=2.0,
                             max_sample_age_secs=60.0)
    assert r.samples_missing is False
    assert r.deviation_status in (V.VwapDeviation.ABOVE,
                                  V.VwapDeviation.BELOW,
                                  V.VwapDeviation.NEUTRAL)


def test_vwap_zero_atr_fail_closed():
    decision_ts = 10000.0
    samples = [_sample(9900.0, 110.0)]
    r = V.compute_micro_vwap(decision_ts, samples, atr_1m=0.0)
    assert r.samples_missing is True