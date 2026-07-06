"""
Backtest TMF Spread Strategy (Phase 0 MTSE)

Reads historical near (indicator CSV) / far (far CSV) data, runs tmf_spread
strategy logic, tracks release events and trailing exit PnL.
"""

import os
import sys
import pandas as pd
import glob
from datetime import datetime
from pathlib import Path

# ── Config ──
DATA_DIR = Path("logs/market_data")
ENTRY_PTS = 20
TRAIL_PTS = 20
MIN_BARS = 3  # minimum bars before entry allowed


class SpreadState:
    def __init__(self):
        self.reset_state()
        self.reset_telemetry()

    def reset_state(self):
        self.in_position = False
        self.entry_ts = None
        self.near_entry = 0.0
        self.far_entry = 0.0
        self.released_leg = None
        self.side = None
        self.peak = 0.0
        self.nadir = 0.0
        self.release_ts = None
        self._released_this_entry = False

    def reset_telemetry(self):
        self.total_entries = 0
        self.total_releases = 0
        self.near_releases = 0
        self.far_releases = 0
        self.continuation_wins = 0
        self.continuation_losses = 0
        self.releases = []
        self.exits = []  # prevent double-release

        # Telemetry
        self.releases = []
        self.exits = []
        self.total_entries = 0
        self.total_releases = 0
        self.near_releases = 0
        self.far_releases = 0
        self.continuation_wins = 0
        self.continuation_losses = 0

    def pnl_near(self, close):
        return close - self.near_entry

    def pnl_far(self, close):
        return self.far_entry - close

    def on_bar(self, ts, near_close, far_close, squeeze_on, bars_since_open):
        if near_close <= 0 or far_close <= 0 or pd.isna(near_close) or pd.isna(far_close):
            return None

        # ── In position ──
        if self.in_position:
            return self._manage(ts, near_close, far_close)

        # ── Entry gate ──
        if not squeeze_on:
            return None
        if bars_since_open < MIN_BARS:
            return None

        # Entry: Long Near / Short Far
        self.in_position = True
        self.entry_ts = ts
        self.near_entry = near_close
        self.far_entry = far_close
        self.released_leg = None
        self.side = None
        self.peak = near_close
        self.nadir = far_close
        self.release_ts = None
        self._released_this_entry = False
        self.total_entries += 1
        return "ENTRY"

    def _manage(self, ts, near_close, far_close):
        npnl = self.pnl_near(near_close)
        fpnl = self.pnl_far(far_close)

        # ── Full spread held, check release ──
        if self.released_leg is None and not self._released_this_entry:
            if npnl <= -ENTRY_PTS:
                self.released_leg = "near"
                self.side = "SHORT"
                self.nadir = far_close
                self.release_ts = ts
                self._released_this_entry = True
                self.total_releases += 1
                self.near_releases += 1
                self.releases.append({
                    "ts": ts, "leg": "near",
                    "near_entry": self.near_entry, "far_entry": self.far_entry,
                    "near_close": near_close, "far_close": far_close,
                    "near_pnl": npnl, "far_pnl": fpnl,
                    "spread_entry": self.near_entry - self.far_entry,
                    "spread_now": near_close - far_close,
                })
                return "RELEASE_NEAR"

            if fpnl <= -ENTRY_PTS:
                self.released_leg = "far"
                self.side = "LONG"
                self.peak = near_close
                self.release_ts = ts
                self._released_this_entry = True
                self.total_releases += 1
                self.far_releases += 1
                self.releases.append({
                    "ts": ts, "leg": "far",
                    "near_entry": self.near_entry, "far_entry": self.far_entry,
                    "near_close": near_close, "far_close": far_close,
                    "near_pnl": npnl, "far_pnl": fpnl,
                    "spread_entry": self.near_entry - self.far_entry,
                    "spread_now": near_close - far_close,
                })
                return "RELEASE_FAR"

            return "HOLD"

        # ── Single leg remaining — trailing ──
        if self.released_leg is not None:
            if self.side == "LONG":
                self.peak = max(self.peak, near_close)
                if self.peak - near_close >= TRAIL_PTS and self._released_this_entry:
                    pnl = near_close - self.near_entry
                    self.exits.append({
                        "ts": ts, "side": "LONG",
                        "entry": self.near_entry, "exit": near_close,
                        "pnl": pnl, "peak": self.peak,
                    })
                    if pnl > 0:
                        self.continuation_wins += 1
                    else:
                        self.continuation_losses += 1
                    self.reset_state()
                    return "EXIT_LONG_TRAIL"
                return "TRAIL_LONG"

            else:  # SHORT
                self.nadir = min(self.nadir, far_close)
                if far_close - self.nadir >= TRAIL_PTS and self._released_this_entry:
                    pnl = self.far_entry - far_close
                    self.exits.append({
                        "ts": ts, "side": "SHORT",
                        "entry": self.far_entry, "exit": far_close,
                        "pnl": pnl, "nadir": self.nadir,
                    })
                    if pnl > 0:
                        self.continuation_wins += 1
                    else:
                        self.continuation_losses += 1
                    self.reset_state()
                    return "EXIT_SHORT_TRAIL"
                return "TRAIL_SHORT"

        # Release already happened — staying flat (shouldn't reach here)
        return None


def load_date_csvs(date_str):
    near_path = DATA_DIR / f"MXF_{date_str}_PAPER_indicators.csv"
    far_path = DATA_DIR / f"MXF_far_{date_str}_PAPER.csv"

    if not near_path.exists() or not far_path.exists():
        return None

    near = pd.read_csv(near_path, parse_dates=["timestamp"])
    far = pd.read_csv(far_path, parse_dates=["timestamp"])

    if near.empty or far.empty:
        return None

    near = near.set_index("timestamp").sort_index()
    far = far.set_index("timestamp").sort_index()

    merged = pd.merge(
        near[["close", "sqz_on", "atr"]],
        far[["close"]],
        left_index=True, right_index=True, how="inner",
    )
    merged.columns = ["near_close", "sqz_on", "atr", "far_close"]
    merged.index.name = "timestamp"
    merged["sqz_on"] = merged["sqz_on"].fillna(False).astype(bool)
    merged["bars_since_open"] = range(len(merged))
    merged["atr"] = merged["atr"].fillna(50)
    return merged


def main():
    ind_files = sorted(DATA_DIR.glob("MXF_*_PAPER_indicators.csv"))
    dates = set()
    for f in ind_files:
        parts = f.stem.split("_")
        if len(parts) >= 4:
            d = parts[1]
            if (DATA_DIR / f"MXF_far_{d}_PAPER.csv").exists():
                dates.add(d)

    state = SpreadState()
    all_actions = []

    print(f"\n{'='*60}")
    print(f"TMF Spread Backtest (Phase 0)")
    print(f"Entry: squeeze_on, {ENTRY_PTS}pt release stop, {TRAIL_PTS}pt trailing")
    print(f"{'='*60}\n")

    for date_str in sorted(dates):
        df = load_date_csvs(date_str)
        if df is None:
            continue

        session_entries = 0
        session_releases = 0
        session_exits = 0
        for ts, row in df.iterrows():
            action = state.on_bar(
                ts, row["near_close"], row["far_close"],
                bool(row["sqz_on"]), int(row["bars_since_open"]),
            )
            if action:
                all_actions.append({"ts": ts, "action": action,
                                    "near": row["near_close"],
                                    "far": row["far_close"]})
                if action == "ENTRY":
                    session_entries += 1
                elif "RELEASE" in action:
                    session_releases += 1
                elif "EXIT" in action:
                    session_exits += 1

        print(f"  {date_str}: entries={session_entries} releases={session_releases} exits={session_exits}")

    # ── Summary ──
    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"  Dates with data: {len(dates)}")
    print(f"  Total entries: {state.total_entries}")
    print(f"  Total releases: {state.total_releases}")
    rel_rate = state.total_releases / max(state.total_entries, 1) * 100
    print(f"  Release rate: {rel_rate:.1f}%")
    print(f"    Near leg releases: {state.near_releases} ({state.near_releases/max(state.total_releases,1)*100:.0f}%)")
    print(f"    Far leg releases:  {state.far_releases} ({state.far_releases/max(state.total_releases,1)*100:.0f}%)")
    print(f"  Trailing exits: {len(state.exits)}")
    print(f"    Wins:  {state.continuation_wins}")
    print(f"    Losses: {state.continuation_losses}")
    if state.exits:
        total_pnl = sum(e["pnl"] for e in state.exits)
        avg_pnl = total_pnl / len(state.exits)
        wr = state.continuation_wins / len(state.exits) * 100
        print(f"    Total trailing PnL: {total_pnl:.1f} pts")
        print(f"    Avg trailing PnL:   {avg_pnl:.1f} pts")
        print(f"    Win rate: {wr:.1f}%")
        print(f"\n  Recent exits:")
        for e in state.exits[-10:]:
            print(f"    {e['ts']} {e['side']:5s} entry={e['entry']:.0f} exit={e['exit']:.0f} pnl={e['pnl']:+.1f}")

    print(f"\n  Recent releases:")
    for r in state.releases[-10:]:
        print(f"    {r['ts']} leg={r['leg']:4s} near_pnl={r['near_pnl']:+.1f} far_pnl={r['far_pnl']:+.1f} "
              f"spread={r['spread_entry']:.1f}→{r['spread_now']:.1f}")

    print(f"\n{'='*60}")
    total_trades = state.total_releases + len(state.exits)
    print(f"Phase 0 verdict: {total_trades} total trade events across {len(dates)} days")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
