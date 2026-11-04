"""2026-11-04 Hermes Agent: a combined exit that never fills must not freeze
position management.

Incident mts-auto-151203-918 (Mini PAPER): COMBINED_EXIT_SUBMITTED at
2026-11-03T15:12:12, zero exit fills, and _manage_position's
`if self._lifecycle == "EXITING": return None` guard skipped every lifecycle
evaluation for 17h — the position was never managed again.

Contract: PAPER-only watchdog. When EXITING has lasted >=
params.exit_fill_timeout_secs (default 30) with no fill, release the latch
(_lifecycle -> OPEN, in-flight False), emit COMBINED_EXIT_STALLED_REARMED and
let the same tick continue evaluating. LIVE is untouched (a timer is not
order-state aware).
"""
from pathlib import Path

import pytest

import strategies.plugins.futures.active.tmf_spread as tmf

SRC = Path(tmf.__file__).read_text()
EVENTS = []


@pytest.fixture(autouse=True)
def _cap(monkeypatch):
    EVENTS.clear()
    monkeypatch.setattr(tmf, "_append_event", lambda ev, **kw: EVENTS.append((ev, kw)))


def _s(timeout=30.0, live=False, lifecycle="EXITING", started=1000.0):
    s = object.__new__(tmf.TMFSpread)
    s._params = {"exit_fill_timeout_secs": timeout}
    s._lifecycle = lifecycle
    s._combined_exit_in_flight = True
    s._exit_submitted_mono = started
    s._trade_id = "mts-test"
    s.live_trading = live
    return s


def test_stalled_exit_is_rearmed_after_timeout():
    s = _s()
    assert s._maybe_rearm_stalled_exit(now_mono=1031.0) is True
    assert s._lifecycle == "OPEN"
    assert s._combined_exit_in_flight is False
    assert s._exit_submitted_mono is None
    ev = [kw for name, kw in EVENTS if name == "COMBINED_EXIT_STALLED_REARMED"]
    assert ev and ev[0]["elapsed_s"] == 31.0 and ev[0]["timeout_s"] == 30.0


def test_before_timeout_stays_latched():
    s = _s()
    assert s._maybe_rearm_stalled_exit(now_mono=1029.0) is False
    assert s._lifecycle == "EXITING"
    assert EVENTS == []


def test_live_is_never_rearmed_by_timer():
    s = _s(live=True)
    assert s._maybe_rearm_stalled_exit(now_mono=9999.0) is False
    assert s._lifecycle == "EXITING"


def test_no_clock_means_no_rearm():
    s = _s(started=None)
    assert s._maybe_rearm_stalled_exit(now_mono=9999.0) is False


def test_not_exiting_is_a_noop():
    s = _s(lifecycle="OPEN")
    assert s._maybe_rearm_stalled_exit(now_mono=9999.0) is False


def test_guard_is_wired_into_manage_position():
    i = SRC.index('if self._lifecycle == "EXITING":')
    window = SRC[i:i + 260]
    assert "_maybe_rearm_stalled_exit()" in window
    assert "EXIT_ALREADY_SUBMITTED" in window


def test_clock_is_started_where_the_combined_exit_is_submitted():
    i = SRC.index('logger.info("[POLICY_J_LIVE_TRIGGERED] Executing COMBINED_EXIT')
    assert "_exit_submitted_mono = time.monotonic()" in SRC[i:i + 900]


def test_successful_exit_still_clears_to_flat():
    i = SRC.index("self._has_position = False\n        self._lifecycle")
    window = SRC[i:i + 220]
    assert '_lifecycle = "FLAT"' in window
    assert "_exit_submitted_mono = None" in window
