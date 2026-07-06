from __future__ import annotations

import copy
import datetime
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from strategies.options.options_engine.engine.greeks import black_scholes
from strategies.options.options_engine.engine.indicators import calculate_futures_squeeze, calculate_mtf_alignment
from strategies.options.theta_gang import ThetaGangManager

try:
    from strategies.options.options_engine.engine.greeks_ql import black_scholes as quantlib_black_scholes
except ImportError:
    quantlib_black_scholes = None

POINT_VALUE = 50.0


@dataclass
class ThetaBacktestTrade:
    entry_time: str
    exit_time: str
    bars_held: int
    entry_spot: float
    exit_spot: float
    entry_credit: float
    exit_value: float
    gross_pnl_points: float
    net_pnl_twd: float
    pnl_points: float
    exit_reason: str
    score_at_entry: float
    score_at_exit: float


def load_replay_bars(path: str | Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    timestamp_col = "datetime" if "datetime" in frame.columns else "timestamp"
    if timestamp_col not in frame.columns:
        raise ValueError(f"Replay file {path} missing datetime/timestamp column")

    frame[timestamp_col] = pd.to_datetime(frame[timestamp_col])
    frame = frame.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )
    required = ["Open", "High", "Low", "Close", "Volume"]
    missing = [col for col in required if col not in frame.columns]
    if missing:
        raise ValueError(f"Replay file {path} missing columns: {missing}")

    frame = frame.set_index(timestamp_col).sort_index()
    frame.index.name = "datetime"
    return frame[required].apply(pd.to_numeric, errors="coerce").dropna(subset=["Open", "High", "Low", "Close"])


def build_theta_signal_frame(
    bars: pd.DataFrame,
    *,
    length: int = 20,
    weights: dict[str, float] | None = None,
) -> pd.DataFrame:
    if bars.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume", "sqz_on", "score"])

    frame_5m = calculate_futures_squeeze(bars, length)
    frame_15m = calculate_futures_squeeze(
        bars.resample("15min", label="right", closed="left").agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        ).dropna(subset=["Open"]),
        length,
    )
    frame_1h = calculate_futures_squeeze(
        bars.resample("1h", label="right", closed="left").agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        ).dropna(subset=["Open"]),
        length,
    )

    score_series: list[float] = []
    for timestamp, row in frame_5m.iterrows():
        data_dict: dict[str, pd.DataFrame] = {"5m": pd.DataFrame([row[["momentum", "mom_state"]]])}

        aligned_15m = frame_15m.loc[:timestamp]
        if not aligned_15m.empty:
            data_dict["15m"] = aligned_15m[["momentum", "mom_state"]].tail(1)

        aligned_1h = frame_1h.loc[:timestamp]
        if not aligned_1h.empty:
            data_dict["1h"] = aligned_1h[["momentum", "mom_state"]].tail(1)
        elif "15m" in data_dict:
            data_dict["1h"] = data_dict["15m"]

        score_series.append(float(calculate_mtf_alignment(data_dict, weights=weights)["score"]))

    result = frame_5m.copy()
    result["score"] = score_series
    return result


def _select_bs_fn(cfg: dict[str, Any]) -> Callable[..., dict[str, float]]:
    pricing_model = str(cfg.get("pricing", {}).get("pricing_model", "black_scholes")).lower()
    if pricing_model == "quantlib" and quantlib_black_scholes is not None:
        return quantlib_black_scholes
    return black_scholes


def _resolve_backtest_iv(cfg: dict[str, Any], explicit_iv: float | None) -> float:
    pricing_cfg = cfg.get("pricing", {})
    theta_cfg = cfg.get("theta_gang", {})
    raw_iv = float(explicit_iv if explicit_iv is not None else pricing_cfg.get("default_iv", 0.25))
    min_iv = float(max(theta_cfg.get("min_iv", 0.18), pricing_cfg.get("min_iv", 0.18)))
    max_iv = float(pricing_cfg.get("max_iv", raw_iv))
    return min(max(raw_iv, min_iv), max_iv)


def _resolve_start_dte_days(cfg: dict[str, Any], explicit_start_dte_days: float | None) -> float:
    if explicit_start_dte_days is not None:
        return float(explicit_start_dte_days)

    theta_cfg = cfg.get("theta_gang", {})
    pricing_cfg = cfg.get("pricing", {})
    strategy_cfg = cfg.get("strategy", {})
    return float(
        max(
            theta_cfg.get("min_dte_entry", 7) + 1,
            strategy_cfg.get("monthly_delivery_min_days", 14),
            pricing_cfg.get("near_dte_days", 3.0),
        )
    )


def _remaining_dte_years(
    start_dte_days: float,
    current_ts: datetime.datetime,
    entry_ts: datetime.datetime | None,
    cfg: dict[str, Any],
) -> float:
    pricing_cfg = cfg.get("pricing", {})
    floor_days = float(pricing_cfg.get("expiry_dte_floor_days", 0.35))
    if entry_ts is None:
        remaining_days = start_dte_days
    else:
        elapsed_days = (current_ts - entry_ts).total_seconds() / (24 * 3600)
        remaining_days = max(floor_days, start_dte_days - elapsed_days)
    return remaining_days / 365.0


def _compute_max_drawdown(equity_curve: list[float]) -> float:
    peak = float("-inf")
    max_drawdown = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
    return max_drawdown


def _mark_open_position(
    manager: ThetaGangManager,
    *,
    spot: float,
    iv: float,
    dte_years: float,
    score: float,
) -> dict[str, Any] | None:
    position = manager.position
    if not position or not position.is_open:
        return None

    current_value = 0.0
    for leg in position.legs:
        current_leg_value = manager.bs_fn(spot, leg.strike, dte_years, manager.r, iv, leg.side)["price"]
        if leg.action == "SELL":
            current_value += current_leg_value
        else:
            current_value -= current_leg_value

    broker_fee = 20.0 * 2 * position.quantity
    exchange_fee = 5.0 * 2 * position.quantity
    tax = (position.net_credit + current_value) * POINT_VALUE * 0.001 * position.quantity
    unrealized_twd = (position.net_credit - current_value) * POINT_VALUE - broker_fee - exchange_fee - tax
    return {
        "strategy": position.strategy,
        "entry_time": position.entry_time.isoformat() if position.entry_time else None,
        "bars_held": None,
        "entry_credit": float(position.net_credit),
        "mark_value": float(current_value),
        "unrealized_twd": float(unrealized_twd),
        "spot": float(spot),
        "score": float(score),
    }


def run_theta_backtest_on_signals(
    signal_frame: pd.DataFrame,
    cfg: dict[str, Any],
    *,
    strategy: str = "bull_put_spread",
    iv: float | None = None,
    start_dte_days: float | None = None,
    bs_fn: Callable[..., dict[str, float]] | None = None,
) -> dict[str, Any]:
    if signal_frame.empty:
        return {
            "strategy": strategy,
            "bars": 0,
            "trade_count": 0,
            "win_rate": 0.0,
            "net_pnl_twd": 0.0,
            "marked_net_pnl_twd": 0.0,
            "avg_pnl_twd": 0.0,
            "max_drawdown_twd": 0.0,
            "open_position": None,
            "trades": [],
        }

    working_cfg = copy.deepcopy(cfg)
    working_cfg.setdefault("theta_gang", {})
    working_cfg["theta_gang"]["strategy"] = strategy

    manager = ThetaGangManager(
        working_cfg,
        bs_fn or _select_bs_fn(working_cfg),
        int(working_cfg.get("pricing", {}).get("strike_rounding", 100)),
    )
    resolved_iv = _resolve_backtest_iv(working_cfg, iv)
    resolved_start_dte_days = _resolve_start_dte_days(working_cfg, start_dte_days)
    release_confirm_bars = int(working_cfg.get("theta_gang", {}).get("squeeze_release_confirm_bars", 1))
    min_holding_bars = int(working_cfg.get("theta_gang", {}).get("min_holding_bars", 0))

    entry_meta: dict[str, Any] = {}
    trades: list[ThetaBacktestTrade] = []
    realized_pnl_twd = 0.0
    equity_curve = [0.0]
    release_confirm_count = 0
    release_last_bar_ts: datetime.datetime | None = None

    for timestamp, row in signal_frame.iterrows():
        row_ts = timestamp.to_pydatetime() if isinstance(timestamp, pd.Timestamp) else timestamp
        spot = float(row["Close"])
        score = float(row.get("score", 0.0) or 0.0)
        squeeze_on = bool(row.get("sqz_on", False))
        dte_years = _remaining_dte_years(resolved_start_dte_days, row_ts, entry_meta.get("entry_ts"), working_cfg)

        if manager.position and manager.position.is_open:
            if not squeeze_on:
                if release_last_bar_ts != row_ts:
                    release_confirm_count += 1
                    release_last_bar_ts = row_ts
            else:
                release_confirm_count = 0
                release_last_bar_ts = None

            allow_release = release_confirm_count >= release_confirm_bars
            exit_info = manager.evaluate_exit(
                spot,
                resolved_iv,
                dte_years,
                squeeze_on,
                allow_squeeze_release=allow_release,
            )
            is_stop_loss = bool(exit_info and "SL" in str(exit_info.get("reason", "")))
            bars_held = int(entry_meta.get("bars_held", 0)) + 1
            entry_meta["bars_held"] = bars_held

            if exit_info and not is_stop_loss and bars_held < min_holding_bars:
                exit_info = None

            if exit_info:
                position = manager.close_position()
                net_pnl_twd = float(exit_info["gross_pnl"]) * POINT_VALUE - float(exit_info["cost"])
                trade = ThetaBacktestTrade(
                    entry_time=entry_meta["entry_ts"].isoformat(),
                    exit_time=row_ts.isoformat(),
                    bars_held=bars_held,
                    entry_spot=float(entry_meta["entry_spot"]),
                    exit_spot=spot,
                    entry_credit=float(position.net_credit),
                    exit_value=float(exit_info["current_value"]),
                    gross_pnl_points=float(exit_info["gross_pnl"]),
                    net_pnl_twd=float(net_pnl_twd),
                    pnl_points=float(exit_info["pnl"]),
                    exit_reason=str(exit_info["reason"]),
                    score_at_entry=float(entry_meta.get("entry_score", 0.0)),
                    score_at_exit=score,
                )
                trades.append(trade)
                realized_pnl_twd += net_pnl_twd
                entry_meta = {}
                release_confirm_count = 0
                release_last_bar_ts = None
        else:
            if squeeze_on:
                entry_info = manager.evaluate_entry(
                    spot,
                    resolved_iv,
                    dte_years,
                    squeeze_on,
                    score=score,
                )
                if entry_info:
                    manager.open_position(entry_info)
                    if manager.position is not None:
                        manager.position.entry_time = row_ts
                    entry_meta = {
                        "entry_ts": row_ts,
                        "entry_spot": spot,
                        "entry_score": score,
                        "bars_held": 0,
                    }
                    release_confirm_count = 0
                    release_last_bar_ts = None

        mark_snapshot = _mark_open_position(
            manager,
            spot=spot,
            iv=resolved_iv,
            dte_years=dte_years,
            score=score,
        )
        marked_equity = realized_pnl_twd + float((mark_snapshot or {}).get("unrealized_twd", 0.0))
        equity_curve.append(marked_equity)

    open_position = None
    if not signal_frame.empty:
        last_ts = signal_frame.index[-1]
        last_row = signal_frame.iloc[-1]
        last_dt = last_ts.to_pydatetime() if isinstance(last_ts, pd.Timestamp) else last_ts
        dte_years = _remaining_dte_years(resolved_start_dte_days, last_dt, entry_meta.get("entry_ts"), working_cfg)
        open_position = _mark_open_position(
            manager,
            spot=float(last_row["Close"]),
            iv=resolved_iv,
            dte_years=dte_years,
            score=float(last_row.get("score", 0.0) or 0.0),
        )
        if open_position is not None:
            open_position["bars_held"] = int(entry_meta.get("bars_held", 0))

    wins = sum(1 for trade in trades if trade.net_pnl_twd > 0)
    net_pnl_twd = sum(trade.net_pnl_twd for trade in trades)
    marked_net_pnl_twd = net_pnl_twd + float((open_position or {}).get("unrealized_twd", 0.0))
    trade_count = len(trades)
    return {
        "strategy": strategy,
        "bars": int(len(signal_frame)),
        "trade_count": trade_count,
        "win_rate": float((wins / trade_count) * 100.0) if trade_count else 0.0,
        "net_pnl_twd": float(net_pnl_twd),
        "marked_net_pnl_twd": float(marked_net_pnl_twd),
        "avg_pnl_twd": float(net_pnl_twd / trade_count) if trade_count else 0.0,
        "max_drawdown_twd": float(_compute_max_drawdown(equity_curve)),
        "open_position": open_position,
        "trades": [asdict(trade) for trade in trades],
    }
