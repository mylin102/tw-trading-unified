import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path("strategies/options").resolve()))
sys.path.insert(0, str(Path("strategies/options/src").resolve()))

from strategies.options.live_options_squeeze_monitor import ShioajiOptionsSmartMonitor
from strategies.options.theta_gang import SpreadLeg, SpreadPosition, ThetaGangManager

SOURCE = Path("strategies/options/live_options_squeeze_monitor.py").read_text(encoding="utf-8")


def make_monitor_stub():
    monitor = ShioajiOptionsSmartMonitor.__new__(ShioajiOptionsSmartMonitor)
    monitor._theta_cfg = {
        "squeeze_release_confirm_bars": 2,
        "max_bar_price_deviation_pts": 250,
    }
    monitor._theta_release_confirm_count = 0
    monitor._theta_release_last_bar_ts = None
    return monitor


def test_signal_bar_quality_blocks_large_reference_drift():
    monitor = make_monitor_stub()

    quality = monitor._evaluate_signal_bar_quality(
        {
            "Open": 35602.0,
            "High": 35612.0,
            "Low": 35552.0,
            "Close": 35565.0,
            "price_mtx": 37440.5,
        },
        reference_price=37440.5,
    )

    assert quality["quality"] == "BLOCK"
    assert "price_drift>250" in quality["issues"]


def test_theta_release_confirmation_requires_two_bars():
    monitor = make_monitor_stub()
    monitor._resolve_futures_squeeze_state = lambda ts: (False, "mock-futures")

    first = monitor._update_theta_release_confirmation(
        {
            "timestamp": datetime.datetime(2026, 4, 20, 21, 50, 0),
            "completed_bar_timestamp": datetime.datetime(2026, 4, 20, 21, 50, 0),
            "squeeze_on": False,
            "Open": 37530.0,
            "High": 37536.0,
            "Low": 37513.0,
            "Close": 37530.0,
            "price_mtx": 37524.5,
        },
        37524.5,
    )
    second = monitor._update_theta_release_confirmation(
        {
            "timestamp": datetime.datetime(2026, 4, 20, 21, 55, 0),
            "completed_bar_timestamp": datetime.datetime(2026, 4, 20, 21, 55, 0),
            "squeeze_on": False,
            "Open": 37541.0,
            "High": 37549.0,
            "Low": 37541.0,
            "Close": 37549.0,
            "price_mtx": 37541.0,
        },
        37541.0,
    )

    assert first["confirmed"] is False
    assert first["confirm_count"] == 1
    assert second["confirmed"] is True
    assert second["confirm_count"] == 2


def test_theta_release_confirmation_blocks_on_futures_conflict():
    monitor = make_monitor_stub()
    monitor._resolve_futures_squeeze_state = lambda ts: (True, "mock-futures")

    state = monitor._update_theta_release_confirmation(
        {
            "timestamp": datetime.datetime(2026, 4, 20, 21, 50, 0),
            "completed_bar_timestamp": datetime.datetime(2026, 4, 20, 21, 50, 0),
            "squeeze_on": False,
            "Open": 37530.0,
            "High": 37536.0,
            "Low": 37513.0,
            "Close": 37530.0,
            "price_mtx": 37524.5,
        },
        37524.5,
    )

    assert state["confirmed"] is False
    assert state["reason"] == "futures_sqz_conflict"
    assert state["confirm_count"] == 0


def test_theta_gang_release_exit_respects_confirmation_gate():
    def bs_stub(_spot, strike, *_args, **_kwargs):
        return {"price": 193.0 if strike == 37200 else 10.0}

    manager = ThetaGangManager(
        {"theta_gang": {"exit_on_squeeze_release": True}},
        bs_stub,
        100,
    )
    manager.position = SpreadPosition(
        strategy="iron_condor",
        legs=[SpreadLeg("P", 37200, "SELL"), SpreadLeg("P", 37000, "BUY")],
        entry_time=datetime.datetime(2026, 4, 20, 21, 3, 58),
        net_credit=183.0,
        max_loss=17.0,
        quantity=1,
    )

    blocked = manager.evaluate_exit(37440.0, 0.26, 7 / 365, False, allow_squeeze_release=False)
    allowed = manager.evaluate_exit(37440.0, 0.26, 7 / 365, False, allow_squeeze_release=True)

    assert blocked is None
    assert allowed is not None
    assert allowed["reason"].startswith("SQUEEZE_RELEASE")


def test_directional_entry_path_uses_release_confirmation_guard():
    assert 'Directional entry gated' in SOURCE
    assert 'directional_release_state = self._update_theta_release_confirmation(signal, spot)' in SOURCE


def test_directional_score_exit_path_uses_release_confirmation_guard():
    assert 'SCORE_DECAY gated' in SOURCE
    assert 'if exit_reason == "score_decay" and not directional_release_state["confirmed"]:' in SOURCE
