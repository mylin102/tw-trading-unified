"""
strategy_reorder_simulator.py

Simulate alternative router candidate orders using attribution CSV logs.

Goal:
- Estimate whether reordering strategies could improve selected outcomes
- Compare actual winner vs shadow alternatives
- Produce per-order summary reports

Expected input CSVs:
1. router_evaluation_log.csv
2. strategy_signal_log.csv   (optional)
3. trade_attribution_log.csv (required for realized PnL lookup)

Recommended future extension:
- integrate with shadow replay outputs for better counterfactual estimates
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd


@dataclass(slots=True)
class SimulationConfig:
    input_dir: Path
    output_dir: Path
    candidate_orders: list[list[str]]
    symbol: Optional[str] = None
    regime: Optional[str] = None
    min_trades_per_strategy: int = 5


def load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def normalize_trade_expectancy(trade_df: pd.DataFrame, min_trades_per_strategy: int = 5) -> pd.DataFrame:
    """
    Build a simple expected value table from realized trades.

    Output columns:
    - strategy_name
    - regime_at_entry
    - trade_count
    - avg_pnl
    - win_rate
    """
    if trade_df.empty:
        return pd.DataFrame(columns=["strategy_name", "regime_at_entry", "trade_count", "avg_pnl", "win_rate"])

    rows = []
    for (strategy_name, regime), g in trade_df.groupby(["strategy_name", "regime_at_entry"], dropna=False):
        pnl = g["pnl"].fillna(0.0)
        trade_count = len(g)
        win_rate = float((pnl > 0).sum()) / trade_count if trade_count else 0.0
        avg_pnl = float(pnl.mean()) if trade_count else 0.0

        rows.append(
            {
                "strategy_name": strategy_name,
                "regime_at_entry": regime,
                "trade_count": trade_count,
                "avg_pnl": avg_pnl,
                "win_rate": win_rate,
                "eligible_for_sim": trade_count >= min_trades_per_strategy,
            }
        )

    return pd.DataFrame(rows)


def build_bar_groups(router_df: pd.DataFrame) -> list[pd.DataFrame]:
    """
    Group router rows by timestamp + symbol (+ regime implicitly retained in rows).

    Assumes one bar is uniquely identified by:
    - timestamp
    - symbol
    """
    if router_df.empty:
        return []

    required = {"timestamp", "symbol", "strategy_name", "candidate_order", "status", "regime"}
    missing = required - set(router_df.columns)
    if missing:
        raise ValueError(f"router_evaluation_log missing columns: {sorted(missing)}")

    groups = []
    for _, g in router_df.groupby(["timestamp", "symbol"], dropna=False, sort=True):
        groups.append(g.sort_values("candidate_order").reset_index(drop=True))
    return groups


def find_actual_winner(bar_df: pd.DataFrame) -> Optional[str]:
    winners = bar_df.loc[bar_df["winner"] == True, "strategy_name"].tolist()  # noqa: E712
    if not winners:
        return None
    return winners[0]


def available_candidates(bar_df: pd.DataFrame) -> list[str]:
    return bar_df["strategy_name"].dropna().tolist()


def pick_simulated_winner(bar_df: pd.DataFrame, new_order: list[str]) -> Optional[str]:
    """
    Simple simulation rule:
    - Among strategies that appeared in this bar's candidate list,
      choose the first one present in new_order.
    - This is a structural reorder approximation, not shadow replay truth.

    Because we do not know whether shadowed strategies would have fired unless
    shadow replay was recorded, this simulator is best-effort only.
    """
    present = set(available_candidates(bar_df))
    for name in new_order:
        if name in present:
            return name
    return None


def attach_expected_pnl(
    winner_name: Optional[str],
    regime: Optional[str],
    expectancy_df: pd.DataFrame,
) -> float:
    if winner_name is None or expectancy_df.empty:
        return 0.0

    subset = expectancy_df[
        (expectancy_df["strategy_name"] == winner_name)
        & (expectancy_df["regime_at_entry"] == regime)
        & (expectancy_df["eligible_for_sim"] == True)  # noqa: E712
    ]
    if subset.empty:
        subset = expectancy_df[
            (expectancy_df["strategy_name"] == winner_name)
            & (expectancy_df["eligible_for_sim"] == True)  # noqa: E712
        ]
    if subset.empty:
        return 0.0
    return float(subset.iloc[0]["avg_pnl"])


def simulate_order(
    router_df: pd.DataFrame,
    trade_df: pd.DataFrame,
    new_order: list[str],
    symbol: Optional[str] = None,
    regime: Optional[str] = None,
    min_trades_per_strategy: int = 5,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns:
    1. per-bar simulation result
    2. one-row summary
    """
    if router_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    df = router_df.copy()

    if symbol is not None:
        df = df[df["symbol"] == symbol]
    if regime is not None:
        df = df[df["regime"] == regime]

    expectancy_df = normalize_trade_expectancy(trade_df, min_trades_per_strategy=min_trades_per_strategy)

    rows = []
    for bar_df in build_bar_groups(df):
        ts = str(bar_df.iloc[0]["timestamp"])
        sym = str(bar_df.iloc[0]["symbol"])
        reg = str(bar_df.iloc[0]["regime"])

        actual_winner = find_actual_winner(bar_df)
        simulated_winner = pick_simulated_winner(bar_df, new_order)

        actual_expected_pnl = attach_expected_pnl(actual_winner, reg, expectancy_df)
        simulated_expected_pnl = attach_expected_pnl(simulated_winner, reg, expectancy_df)

        rows.append(
            {
                "timestamp": ts,
                "symbol": sym,
                "regime": reg,
                "actual_winner": actual_winner,
                "simulated_winner": simulated_winner,
                "actual_expected_pnl": actual_expected_pnl,
                "simulated_expected_pnl": simulated_expected_pnl,
                "expected_pnl_delta": simulated_expected_pnl - actual_expected_pnl,
                "candidate_list": ",".join(available_candidates(bar_df)),
                "simulated_order": ",".join(new_order),
            }
        )

    detail_df = pd.DataFrame(rows)

    if detail_df.empty:
        return detail_df, pd.DataFrame()

    changed_count = int((detail_df["actual_winner"] != detail_df["simulated_winner"]).sum())
    summary_df = pd.DataFrame(
        [
            {
                "simulated_order": ",".join(new_order),
                "bars": len(detail_df),
                "changed_count": changed_count,
                "change_rate": changed_count / len(detail_df) if len(detail_df) else 0.0,
                "actual_expected_pnl_sum": float(detail_df["actual_expected_pnl"].sum()),
                "simulated_expected_pnl_sum": float(detail_df["simulated_expected_pnl"].sum()),
                "expected_pnl_delta_sum": float(detail_df["expected_pnl_delta"].sum()),
            }
        ]
    )

    return detail_df, summary_df


def run_simulations(config: SimulationConfig) -> None:
    config.output_dir.mkdir(parents=True, exist_ok=True)

    router_df = load_csv(config.input_dir / "router_evaluation_log.csv")
    trade_df = load_csv(config.input_dir / "trade_attribution_log.csv")

    all_summaries = []
    for idx, order in enumerate(config.candidate_orders, start=1):
        detail_df, summary_df = simulate_order(
            router_df=router_df,
            trade_df=trade_df,
            new_order=order,
            symbol=config.symbol,
            regime=config.regime,
            min_trades_per_strategy=config.min_trades_per_strategy,
        )

        tag = f"order_{idx}"
        detail_df.to_csv(config.output_dir / f"{tag}_detail.csv", index=False)
        summary_df.to_csv(config.output_dir / f"{tag}_summary.csv", index=False)

        if not summary_df.empty:
            all_summaries.append(summary_df)

    if all_summaries:
        final_summary = pd.concat(all_summaries, ignore_index=True).sort_values(
            "expected_pnl_delta_sum", ascending=False
        )
    else:
        final_summary = pd.DataFrame()

    final_summary.to_csv(config.output_dir / "simulation_summary.csv", index=False)

    metadata = {
        "symbol": config.symbol,
        "regime": config.regime,
        "min_trades_per_strategy": config.min_trades_per_strategy,
        "candidate_orders": config.candidate_orders,
    }
    (config.output_dir / "simulation_config.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("Simulation complete.")
    print(f"Input dir : {config.input_dir}")
    print(f"Output dir: {config.output_dir}")
    print(f"Orders    : {len(config.candidate_orders)}")
    if not final_summary.empty:
        print("\nTop results:")
        print(final_summary.head(10).to_string(index=False))
    else:
        print("\nNo simulation results generated.")


def parse_orders(raw_orders: Iterable[str]) -> list[list[str]]:
    """
    Example:
        --order counter_vwap,spring_upthrust,kbar_feature
        --order kbar_feature,counter_vwap,spring_upthrust
    """
    parsed = []
    for raw in raw_orders:
        items = [x.strip() for x in raw.split(",") if x.strip()]
        if not items:
            continue
        parsed.append(items)
    return parsed


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Simulate alternative strategy candidate orders.")
    parser.add_argument("--input-dir", required=True, help="Directory containing attribution CSV files")
    parser.add_argument("--output-dir", required=True, help="Directory for simulation outputs")
    parser.add_argument(
        "--order",
        action="append",
        required=True,
        help="Comma-separated candidate order. Can be repeated multiple times.",
    )
    parser.add_argument("--symbol", default=None, help="Optional symbol filter, e.g. TX")
    parser.add_argument("--regime", default=None, help="Optional regime filter, e.g. WEAK")
    parser.add_argument("--min-trades-per-strategy", type=int, default=5, help="Minimum trades required to use avg_pnl")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    candidate_orders = parse_orders(args.order)
    if not candidate_orders:
        raise SystemExit("No valid --order provided")

    config = SimulationConfig(
        input_dir=Path(args.input_dir),
        output_dir=Path(args.output_dir),
        candidate_orders=candidate_orders,
        symbol=args.symbol,
        regime=args.regime,
        min_trades_per_strategy=args.min_trades_per_strategy,
    )
    run_simulations(config)


if __name__ == "__main__":
    main()
