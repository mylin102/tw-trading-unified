import datetime

import pandas as pd

from strategies.options.theta_backtest import build_theta_signal_frame, run_theta_backtest_on_signals


def test_build_theta_signal_frame_adds_score_column():
    index = pd.date_range("2026-02-04 15:00:00", periods=120, freq="5min")
    bars = pd.DataFrame(
        {
            "Open": [33000 + i for i in range(120)],
            "High": [33010 + i for i in range(120)],
            "Low": [32990 + i for i in range(120)],
            "Close": [33005 + i for i in range(120)],
            "Volume": [1000 + (i % 5) * 10 for i in range(120)],
        },
        index=index,
    )

    signals = build_theta_signal_frame(bars, length=20)

    assert "score" in signals.columns
    assert "sqz_on" in signals.columns
    assert len(signals) == len(bars)
    assert signals.index.equals(bars.index)


def test_run_theta_backtest_records_release_exit_trade():
    index = pd.date_range("2026-02-04 15:00:00", periods=4, freq="5min")
    signals = pd.DataFrame(
        {
            "Open": [1000.0, 1005.0, 1100.0, 1100.0],
            "High": [1005.0, 1010.0, 1105.0, 1105.0],
            "Low": [995.0, 1000.0, 1095.0, 1095.0],
            "Close": [1000.0, 1005.0, 1100.0, 1100.0],
            "Volume": [1000, 1000, 1000, 1000],
            "sqz_on": [True, True, False, False],
            "score": [35.0, 40.0, 25.0, 20.0],
        },
        index=index,
    )

    def bs_stub(spot, strike, *_args, **_kwargs):
        if strike == 800:
            return {"price": 10.0 if spot < 1100 else 2.0}
        if strike == 900:
            return {"price": 40.0 if spot < 1100 else 10.0}
        return {"price": 1.0}

    cfg = {
        "pricing": {"strike_rounding": 100, "default_iv": 0.25, "max_iv": 0.5, "min_iv": 0.18},
        "strategy": {"monthly_delivery_min_days": 14},
        "theta_gang": {
            "strategy": "bull_put_spread",
            "wing_width": 100,
            "otm_offset": 100,
            "min_credit": 20,
            "min_iv": 0.18,
            "min_dte_entry": 7,
            "min_dte_exit": 3,
            "take_profit_pct": 0.95,
            "max_loss_pct": 1.0,
            "exit_on_squeeze_release": True,
            "squeeze_release_confirm_bars": 2,
            "min_holding_bars": 0,
        },
    }

    result = run_theta_backtest_on_signals(
        signals,
        cfg,
        strategy="bull_put_spread",
        iv=0.25,
        start_dte_days=14,
        bs_fn=bs_stub,
    )

    assert result["trade_count"] == 1
    assert result["win_rate"] == 100.0
    assert result["open_position"] is None
    assert result["trades"][0]["exit_reason"].startswith("SQUEEZE_RELEASE")
    assert result["trades"][0]["entry_time"] == datetime.datetime(2026, 2, 4, 15, 0).isoformat()


def test_run_theta_backtest_marks_open_position_risk_in_drawdown():
    index = pd.date_range("2026-02-04 15:00:00", periods=2, freq="5min")
    signals = pd.DataFrame(
        {
            "Open": [1000.0, 950.0],
            "High": [1005.0, 955.0],
            "Low": [995.0, 945.0],
            "Close": [1000.0, 950.0],
            "Volume": [1000, 1000],
            "sqz_on": [True, True],
            "score": [35.0, 35.0],
        },
        index=index,
    )

    def bs_stub(spot, strike, *_args, **_kwargs):
        if strike == 800:
            return {"price": 10.0 if spot >= 1000 else 50.0}
        if strike == 900:
            return {"price": 40.0 if spot >= 1000 else 80.0}
        return {"price": 1.0}

    cfg = {
        "pricing": {"strike_rounding": 100, "default_iv": 0.25, "max_iv": 0.5, "min_iv": 0.18},
        "strategy": {"monthly_delivery_min_days": 14},
        "theta_gang": {
            "strategy": "bull_put_spread",
            "wing_width": 100,
            "otm_offset": 100,
            "min_credit": 20,
            "min_iv": 0.18,
            "min_dte_entry": 7,
            "min_dte_exit": 3,
            "take_profit_pct": 0.95,
            "max_loss_pct": 1.0,
            "exit_on_squeeze_release": False,
            "squeeze_release_confirm_bars": 2,
            "min_holding_bars": 0,
        },
    }

    result = run_theta_backtest_on_signals(
        signals,
        cfg,
        strategy="bull_put_spread",
        iv=0.25,
        start_dte_days=14,
        bs_fn=bs_stub,
    )

    assert result["trade_count"] == 0
    assert result["open_position"] is not None
    assert result["marked_net_pnl_twd"] < 0
    assert result["max_drawdown_twd"] < 0
