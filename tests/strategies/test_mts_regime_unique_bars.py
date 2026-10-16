# 2026-10-12 Codex: merged prewarm/live bars cannot fabricate complete hours.
from strategies.plugins.futures.active.mts_hvwap_candidate import aggregate_completed_bars


def bars(n):
    base = 1791766800.0  # an epoch-hour boundary; each timestamp is bar CLOSE
    return [{"ts": base + (i + 1) * 300, "close": 49000 + i, "volume": 10}
            for i in range(n)]


def test_duplicate_half_hour_is_not_complete_hour():
    history = bars(6)
    assert aggregate_completed_bars(history + history, history[0]["ts"] + 3600, 3600) == []


def test_complete_hour_volume_counts_each_real_bar_once():
    history = bars(12)
    result = aggregate_completed_bars(history + history, history[-1]["ts"], 3600)
    assert len(result) == 1
    assert result[0]["volume"] == 120
