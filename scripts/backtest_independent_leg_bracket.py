#!/usr/bin/env python3
"""Compare TMF spread results with the independent leg bracket disabled/enabled."""

import glob
import os
import sys
from pathlib import Path

import pandas as pd
import yaml

sys.path.append(".")

import core.channel_safety
from backtest_spread_v2 import SpreadBacktester


DATA_PATTERN = "data/tmf_calendar_spread_*.csv"
CONFIG_PATH = Path("config/futures.yaml")
BRACKET_PARAMS = {
    "enabled": True,
    "take_profit_pts": 100.0,
    "stop_loss_pts": 30.0,
}


class _BacktestSafetyState:
    """Allow historical entries without depending on live broker reconciliation."""

    @staticmethod
    def entry_allowed(_ticker=""):
        return True


def _load_base_params():
    with CONFIG_PATH.open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)
    return dict(config.get("mts", {}).get("params", {}))


def _run_scenario(label, files, base_params, bracket_params):
    # 2026-11-22 Hermes Agent: isolate offline replay from the live account gate.
    core.channel_safety.get_safety_state = lambda: _BacktestSafetyState()
    params = dict(base_params)
    params.pop("independent_leg_bracket", None)
    if bracket_params is not None:
        params["independent_leg_bracket"] = dict(bracket_params)

    tester = SpreadBacktester("tmf_spread", config={"params": params})
    for filename in files:
        frame = pd.read_csv(filename)
        if frame.empty:
            continue
        timestamp_col = next(
            (column for column in ("ts", "timestamp", "datetime") if column in frame.columns),
            None,
        )
        if timestamp_col is None:
            raise ValueError(f"No timestamp column found in {filename}")
        frame[timestamp_col] = pd.to_datetime(frame[timestamp_col])
        tester.run_on_df(frame.set_index(timestamp_col))

    exits = [trade for trade in tester.trades if trade["action"] == "EXIT"]
    realized = [trade["pnl"] for trade in tester.trades if "pnl" in trade]
    winners = sum(pnl for pnl in realized if pnl > 0)
    losers = -sum(pnl for pnl in realized if pnl < 0)
    return {
        "scenario": label,
        "net_pnl": tester.cash - 100_000,
        "trade_count": len(exits),
        "win_rate": sum(pnl > 0 for pnl in realized) / len(realized) if realized else 0.0,
        "profit_factor": winners / losers if losers else (float("inf") if winners else 0.0),
    }


def _print_table(title, result):
    print(title)
    print("Scenario          | Net PnL (TWD) | Trades | Win Rate | Profit Factor")
    print("------------------+---------------+--------+----------+--------------")
    pf = "inf" if result["profit_factor"] == float("inf") else f"{result['profit_factor']:.2f}"
    print(
        f"{result['scenario']:<18} | {result['net_pnl']:>13,.2f} | "
        f"{result['trade_count']:>6} | {result['win_rate']:>7.2%} | {pf:>12}"
    )


def main():
    files = sorted(glob.glob(DATA_PATTERN))
    if len(files) != 4:
        raise RuntimeError(f"Expected 4 files matching {DATA_PATTERN}; found {len(files)}")

    base_params = _load_base_params()
    results = [
        _run_scenario("BASELINE", files, base_params, None),
        _run_scenario("BRACKET_ENABLED", files, base_params, BRACKET_PARAMS),
    ]

    print(f"Combined results across {len(files)} files: {', '.join(Path(name).name for name in files)}")
    _print_table("BASELINE", results[0])
    print()
    _print_table("BRACKET_ENABLED", results[1])


if __name__ == "__main__":
    # 2026-11-22 Hermes Agent: keep this offline backtest on Apple Silicon E-Cores.
    if sys.platform == "darwin":
        os.system(f"taskpolicy -b -p {os.getpid()}")
    main()
