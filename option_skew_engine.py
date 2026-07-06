#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
option_skew_engine.py

Purpose
-------
Build intraday option skew signals from option quotes and futures bars.

This module is designed for tw-trading-unified style workflows:

    option quotes + futures canonical bars
        -> skew_signal.csv
        -> consumed by backtest / strategy layer

Core Principle
--------------
This file only builds signals from data available at or before timestamp t.
Do NOT use future option prices when generating the signal for t.

Expected option quote columns
-----------------------------
timestamp          datetime string
option_type        CALL / PUT / C / P
strike             numeric
last_price         numeric, optional if bid/ask exists
bid                numeric, optional
ask                numeric, optional
expiry             optional
volume             optional
open_interest      optional

Expected futures bar columns
----------------------------
timestamp          datetime string
close              numeric
trading_day        optional
session            optional

Output columns
--------------
timestamp
trading_day
session
underlying_price
atm_strike
put_strike
call_strike
put_price
call_price
put_change
call_change
put_call_divergence
skew_level
skew_change
direction
confidence
vol_regime

Example
-------
python option_skew_engine.py \
  --options data/raw/options_TMF_20260424.csv \
  --futures data/processed/TMF_5m_20260424.csv \
  --output data/processed/skew_signal_TMF_20260424.csv \
  --otm-points 300
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import pandas as pd


@dataclass
class SkewConfig:
    otm_points: int = 300
    resample_rule: Optional[str] = None
    confidence_scale: float = 80.0
    min_price: float = 0.1
    neutral_threshold: float = 0.15
    vol_expand_threshold: float = 0.25


def _normalize_option_type(value: object) -> str:
    s = str(value).strip().upper()
    if s in {"C", "CALL", "買權"}:
        return "CALL"
    if s in {"P", "PUT", "賣權"}:
        return "PUT"
    raise ValueError(f"Unknown option_type: {value}")


def _load_options(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"timestamp", "option_type", "strike"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Option file missing columns: {sorted(missing)}")

    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["option_type"] = df["option_type"].map(_normalize_option_type)
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")

    if "mid_price" not in df.columns:
        if {"bid", "ask"}.issubset(df.columns):
            bid = pd.to_numeric(df["bid"], errors="coerce")
            ask = pd.to_numeric(df["ask"], errors="coerce")
            last = pd.to_numeric(df.get("last_price", np.nan), errors="coerce")
            df["mid_price"] = np.where(
                (bid > 0) & (ask > 0) & (ask >= bid),
                (bid + ask) / 2.0,
                last,
            )
        elif "last_price" in df.columns:
            df["mid_price"] = pd.to_numeric(df["last_price"], errors="coerce")
        else:
            raise ValueError("Option file must contain either bid/ask or last_price.")

    df["mid_price"] = pd.to_numeric(df["mid_price"], errors="coerce")
    df = df.dropna(subset=["timestamp", "strike", "mid_price"])
    df = df[df["mid_price"] > 0]
    return df.sort_values("timestamp")


def _load_futures(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"timestamp", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Futures file missing columns: {sorted(missing)}")

    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["timestamp", "close"])
    return df.sort_values("timestamp")


def _nearest_strike(quotes: pd.DataFrame, target: float, option_type: str) -> Tuple[Optional[float], Optional[float]]:
    q = quotes[quotes["option_type"] == option_type]
    if q.empty:
        return None, None
    idx = (q["strike"] - target).abs().idxmin()
    row = q.loc[idx]
    return float(row["strike"]), float(row["mid_price"])


def _latest_quotes_until(options: pd.DataFrame, timestamp: pd.Timestamp) -> pd.DataFrame:
    # Last observed quote for each option_type + strike at or before timestamp.
    q = options[options["timestamp"] <= timestamp]
    if q.empty:
        return q
    return (
        q.sort_values("timestamp")
        .groupby(["option_type", "strike"], as_index=False)
        .tail(1)
    )


def build_skew_signal(options: pd.DataFrame, futures: pd.DataFrame, config: SkewConfig) -> pd.DataFrame:
    rows = []

    for _, bar in futures.iterrows():
        ts = bar["timestamp"]
        underlying = float(bar["close"])
        quotes = _latest_quotes_until(options, ts)

        if quotes.empty:
            continue

        atm_strike, _ = _nearest_strike(quotes, underlying, "CALL")
        if atm_strike is None:
            atm_strike, _ = _nearest_strike(quotes, underlying, "PUT")
        if atm_strike is None:
            continue

        put_target = underlying - config.otm_points
        call_target = underlying + config.otm_points

        put_strike, put_price = _nearest_strike(quotes, put_target, "PUT")
        call_strike, call_price = _nearest_strike(quotes, call_target, "CALL")

        if put_price is None or call_price is None:
            continue

        # Simple level: downside premium minus upside premium.
        skew_level = float(put_price - call_price)

        rows.append(
            {
                "timestamp": ts,
                "trading_day": bar.get("trading_day", pd.NaT),
                "session": bar.get("session", ""),
                "underlying_price": underlying,
                "atm_strike": atm_strike,
                "put_strike": put_strike,
                "call_strike": call_strike,
                "put_price": put_price,
                "call_price": call_price,
                "skew_level": skew_level,
            }
        )

    out = pd.DataFrame(rows)
    if out.empty:
        return out

    out = out.sort_values("timestamp").reset_index(drop=True)

    if config.resample_rule:
        out = (
            out.set_index("timestamp")
            .resample(config.resample_rule)
            .last()
            .dropna(subset=["underlying_price"])
            .reset_index()
        )

    out["put_change"] = out["put_price"].diff()
    out["call_change"] = out["call_price"].diff()
    out["put_call_divergence"] = out["put_change"] - out["call_change"]
    out["skew_change"] = out["skew_level"].diff()

    # Normalize confidence. Large divergence means stronger skew signal.
    raw = out["put_call_divergence"].fillna(0.0) / max(config.confidence_scale, 1e-9)
    out["confidence"] = raw.abs().clip(0, 1)

    def classify_direction(x: float, conf: float) -> str:
        if conf < config.neutral_threshold:
            return "NEUTRAL"
        return "DOWN" if x > 0 else "UP"

    out["direction"] = [
        classify_direction(x, c)
        for x, c in zip(out["put_call_divergence"].fillna(0.0), out["confidence"].fillna(0.0))
    ]

    # Vol regime: option structure moving fast = expanding risk perception.
    skew_abs = out["skew_change"].abs().fillna(0.0)
    denom = out[["put_price", "call_price"]].mean(axis=1).replace(0, np.nan)
    vol_impulse = (skew_abs / denom).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    out["vol_impulse"] = vol_impulse
    out["vol_regime"] = np.where(
        out["vol_impulse"] >= config.vol_expand_threshold,
        "EXPANDING",
        "COMPRESSING",
    )
    out.loc[out["confidence"] < config.neutral_threshold, "vol_regime"] = "NEUTRAL"

    # Fill first-row changes with zero for easier downstream consumption.
    for col in ["put_change", "call_change", "put_call_divergence", "skew_change"]:
        out[col] = out[col].fillna(0.0)

    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--options", required=True, help="Path to option quotes CSV")
    parser.add_argument("--futures", required=True, help="Path to futures canonical bars CSV")
    parser.add_argument("--output", required=True, help="Output skew signal CSV")
    parser.add_argument("--otm-points", type=int, default=300)
    parser.add_argument("--resample-rule", default=None, help="Optional pandas resample rule, e.g. 1min, 5min")
    parser.add_argument("--confidence-scale", type=float, default=80.0)
    parser.add_argument("--neutral-threshold", type=float, default=0.15)
    parser.add_argument("--vol-expand-threshold", type=float, default=0.25)
    args = parser.parse_args()

    config = SkewConfig(
        otm_points=args.otm_points,
        resample_rule=args.resample_rule,
        confidence_scale=args.confidence_scale,
        neutral_threshold=args.neutral_threshold,
        vol_expand_threshold=args.vol_expand_threshold,
    )

    options = _load_options(args.options)
    futures = _load_futures(args.futures)
    signal = build_skew_signal(options, futures, config)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    signal.to_csv(output, index=False)

    print(f"[OK] wrote {len(signal):,} rows -> {output}")


if __name__ == "__main__":
    main()
