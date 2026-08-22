#!/usr/bin/env python3
"""mts_trend_replay_v1.py — TSB 2.0 counterfactual bar-lifecycle replay for the
trend-confirmed release hypothesis (MTS 2.0 marginal priority).

Replays historical MTS calendar-spread entries from the runtime fills log and
walks the 1-min near/far bar series under three deterministic arms:

  * BASELINE_SINGLE_LEG_RELEASE  — release the same leg the historical run
                                   released (at the historical release bar),
                                   then exit the remaining leg at the exit bar.
  * TREND_CONFIRMED_RELEASE      — release the counter-trend leg at the first
                                   bar where the MTS 2.0 trend-signal pipeline
                                   (compute_adl_snr + compute_renko +
                                   compute_micro_vwap -> arbitrate_trend)
                                   returns pass_release, then exit the remaining
                                   leg at the exit bar. If trend never confirms,
                                   hold both legs (combined exit).
  * NO_REVT                      — hold both legs the whole episode and exit
                                   combined at the historical exit bar.

All arms run on the SAME 1-min bar series and the SAME cost model (point value
10, broker fee, turnover tax), so they are directly comparable.

READ-ONLY: no broker, no PM2, no Shioaji. The only filesystem write is the
JSON manifest under scripts/research/output/.

VERDICT: if fewer than MIN_ELIGIBLE_FOR_APPROVAL (30) entries are eligible, the
summary is tagged RESEARCH_INSUFFICIENT_SAMPLE and the verdict is HOLD.
"""
from __future__ import annotations

import bisect
import csv
import hashlib
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# MTS 2.0 trend pipeline (real modules). If import fails the harness still
# runs but every trend-confirm step returns a fail-closed block.
HAS_MODULES = True
try:
    from strategies.plugins.futures.active.mts_trend_signal_adapter import (
        TrendDirection,
        compute_adl_snr,
        adl_signal_state,
        arbitrate_trend,
    )
    from strategies.plugins.futures.active.mts_renko_signal import (
        compute_renko,
        renko_signal_state,
    )
    from strategies.plugins.futures.active.mts_micro_vwap import (
        compute_micro_vwap,
        vwap_signal_state,
    )
except Exception:  # pragma: no cover
    HAS_MODULES = False

# ---------------------------------------------------------------------------
POINT_VALUE = 10.0
BROKER_FEE = 20.0
TAX_RATE = 2e-5
MIN_ELIGIBLE_FOR_APPROVAL = 30
ARMS = (
    "BASELINE_SINGLE_LEG_RELEASE",
    "TREND_CONFIRMED_RELEASE",
    "NO_REVT",
)
SELF_PATH = Path(__file__)


def cost(entry_px: float, exit_px: float) -> float:
    """Transaction cost (TWD) for one entered-and-exited leg."""
    return 2.0 * BROKER_FEE + (entry_px + exit_px) * POINT_VALUE * TAX_RATE


def leg_pnl(entry_px: float, exit_px: float, side: str) -> float:
    """Pts-scale PnL for one leg under the shared cost model."""
    sign = 1.0 if str(side).upper() == "LONG" else -1.0
    return (exit_px - entry_px) * sign * POINT_VALUE - cost(entry_px, exit_px)


# ---------------------------------------------------------------------------
# Input loading (discovery, read-only)
def discover_fills_log(root: str) -> Path | None:
    root_p = Path(root)
    cands = [root_p / "logs" / "mts_trade_fills.jsonl"]
    if (root_p / "data").exists():
        cands += sorted((root_p / "data").glob("**/mts_trade_fills.jsonl"))
    present = [c for c in cands if c.exists() and c.stat().st_size > 0]
    if not present:
        return None
    present.sort(key=lambda p: p.stat().st_size)
    return present[-1]  # largest = most complete


def discover_bar_files(root: str, leg: str) -> list[Path]:
    return sorted(Path(root).glob(f"data/tmf_{leg}_*.csv"))


def load_fills(path: str) -> list[dict]:
    out: list[dict] = []
    if not Path(path).exists():
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def load_bars(files: list[Path], leg: str) -> dict[str, dict]:
    """Concatenate rolling bar windows; index each timestamp -> its bar dict."""
    by_ts: dict[str, dict] = {}
    for fp in sorted(files, key=lambda p: p.name):
        if not fp.exists():
            continue
        with open(fp, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    ts = row["ts"].strip()
                    by_ts[ts] = {
                        "ts": ts,
                        "open": float(row["Open"]),
                        "high": float(row["High"]),
                        "low": float(row["Low"]),
                        "close": float(row["Close"]),
                        "volume": float(row["Volume"]) if row.get("Volume") else 0.0,
                        "leg": leg,
                    }
                except (KeyError, ValueError, TypeError):
                    continue
    return by_ts


def build_episodes(fills: list[dict]) -> list[dict]:
    by: dict[str, list[dict]] = {}
    for f in fills:
        by.setdefault(str(f.get("trade_id") or "unknown"), []).append(f)
    episodes = []
    for tid in sorted(by):
        ep = {"trade_id": tid, "entry_near": None, "entry_far": None,
              "release": None, "exit": None}
        for e in by[tid]:
            ft = e.get("fill_type")
            if ft == "ENTRY" and e.get("leg") == "NEAR":
                ep["entry_near"] = e
            elif ft == "ENTRY" and e.get("leg") == "FAR":
                ep["entry_far"] = e
            elif ft == "RELEASE":
                ep["release"] = e
            elif ft == "EXIT":
                ep["exit"] = e
        episodes.append(ep)
    return episodes


def normalize_ts(ts: str) -> str:
    return str(ts).strip().replace("T", " ")


def bar_at_or_before(keys: list[str], bars: dict[str, dict], ts: str) -> dict | None:
    idx = bisect.bisect_right(keys, normalize_ts(ts)) - 1
    if idx < 0:
        return None
    return bars[keys[idx]]


# ---------------------------------------------------------------------------
# deterministic trend signal using the real modules
def _atr(series: list[dict]) -> float:
    if not series:
        return 1.0
    prev_c = series[0]["close"]
    trs = []
    for b in series:
        hi, lo = b["high"], b["low"]
        trs.append(max(hi - lo, abs(hi - prev_c), abs(lo - prev_c)))
        prev_c = b["close"]
    return (sum(trs) / len(trs)) or 1.0


def _vwap_samples(series: list[dict]) -> list[dict]:
    """Deterministic 5-sec-style volume-split resample of the 1-min bars."""
    out = []
    for b in series:
        per = max(float(b.get("volume") or 0.0), 1.0) / 12.0
        for _ in range(12):
            out.append({"ts": b["ts"], "price": b["close"], "volume": per})
    return out[-900:]


def expected_direction(series: list[dict]) -> TrendDirection:
    if len(series) < 2:
        return TrendDirection.UNKNOWN
    if series[-1]["close"] > series[0]["close"]:
        return TrendDirection.BULLISH
    if series[-1]["close"] < series[0]["close"]:
        return TrendDirection.BEARISH
    return TrendDirection.UNKNOWN


def _trend_decision(decision_ts: str, near_series: list[dict]) -> dict:
    """Real ADL + Renko + Micro-VWAP -> arbitrate_trend. Returns to_dict()."""
    if not HAS_MODULES:
        return {"decision_ts": decision_ts, "direction": "CHOP",
                "confidence": 0.0, "pass_release": False,
                "block_reason": "MODULES_UNAVAILABLE"}
    expected = expected_direction(near_series)
    bars = [{"high": b["high"], "low": b["low"], "close": b["close"],
             "volume": b["volume"]} for b in near_series]
    adl = compute_adl_snr(decision_ts, bars, window_n=12)
    adl_ss = adl_signal_state(adl, expected)

    closes = [b["close"] for b in near_series]
    brick = max(_atr(near_series) * 0.5, 0.1)
    ren = compute_renko(decision_ts, closes, brick,
                        seed_price=closes[0] if closes else 0.0)
    ren_ss = renko_signal_state(ren, expected)

    vw = compute_micro_vwap(decision_ts, _vwap_samples(near_series),
                            atr_1m=max(_atr(near_series), 0.01))
    vw_ss = vwap_signal_state(vw, expected)

    decision = arbitrate_trend(decision_ts, ren_ss, adl_ss, vw_ss,
                               decision_max_quote_age_ms=0.0,
                               window_max_quote_age_ms=0.0)
    return decision.to_dict()


def walk_trend_confirmation(keys: list[str], bars: dict[str, dict],
                            ts_from: str, ts_to: str) -> dict | None:
    """Walk bars entry->horizon; return the first pass_release decision dict
    (augmented with bar_ts), or None if never confirmed."""
    i = bisect.bisect_left(keys, normalize_ts(ts_from))
    j = bisect.bisect_right(keys, normalize_ts(ts_to))
    window = keys[i:j]
    for end in range(12, len(window) + 1):
        near_series = [bars[k] for k in window[:end]]
        decision_ts = window[end - 1]
        d = _trend_decision(decision_ts, near_series)
        if isinstance(d, dict) and d.get("pass_release"):
            d["bar_ts"] = decision_ts
            return d
    return None


# ---------------------------------------------------------------------------
# per-leg accessors
def _entry_price(ep: dict, leg: str) -> float:
    e = ep["entry_near"] if leg == "NEAR" else ep["entry_far"]
    return float(e.get("price") or 0.0)


def _leg_side(ep: dict, leg: str) -> str:
    e = ep["entry_near"] if leg == "NEAR" else ep["entry_far"]
    return str(e.get("side") or "LONG")


def _counter_trend_leg(decision_dir: str | None) -> str:
    """Released leg is the counter-trend leg (deterministic mapping)."""
    if decision_dir == "BULLISH":
        return "NEAR"
    if decision_dir == "BEARISH":
        return "FAR"
    return "NEAR"


def _lifecycle_horizon(ep: dict) -> str:
    """Historical endpoint timestamp shared by all arms (exit>release>entry)."""
    if ep["exit"] is not None:
        return normalize_ts(ep["exit"].get("timestamp", ""))
    if ep["release"] is not None:
        return normalize_ts(ep["release"].get("timestamp", ""))
    return normalize_ts(ep["entry_near"].get("timestamp", ""))


# ---------------------------------------------------------------------------
# per-arm statistics
def _empty_stats() -> dict:
    return {"eligible": 0, "skipped": 0, "pnl": 0.0, "pnls": [],
            "avg_pnl": 0.0, "max_drawdown": 0.0, "release": 0,
            "combined": 0, "exit_count": 0, "coverage": 0.0}


def _peak_trough_drawdown(pnls: list[float]) -> float:
    if not pnls:
        return 0.0
    cum, peak, mdd = 0.0, -1e18, 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return mdd


def _simulate_arm(arm: str, episodes: list[dict], keys: list[str],
                  near: dict[str, dict], far: dict[str, dict]) -> dict:
    st = _empty_stats()
    for ep in episodes:
        an, af = ep["entry_near"], ep["entry_far"]
        if an is None or af is None or (ep["exit"] is None and ep["release"] is None):
            st["skipped"] += 1
            continue
        entry_ts = normalize_ts(an.get("timestamp", ""))
        horizon_ts = _lifecycle_horizon(ep)
        if bar_at_or_before(keys, near, entry_ts) is None or \
                bar_at_or_before(keys, near, horizon_ts) is None:
            st["skipped"] += 1
            continue
        st["eligible"] += 1

        exit_px = bar_at_or_before(keys, near, horizon_ts)["close"]
        pnl = 0.0
        released = False
        combined = False
        n_exits = 0

        if arm == "BASELINE_SINGLE_LEG_RELEASE":
            rel = ep["release"]
            if rel is not None:
                released = True
                rel_leg = str(rel.get("leg") or "NEAR")
                rel_px = float(rel.get("price") or exit_px)
                pnl += leg_pnl(_entry_price(ep, rel_leg), rel_px, _leg_side(ep, rel_leg))
                other = "FAR" if rel_leg == "NEAR" else "NEAR"
                pnl += leg_pnl(_entry_price(ep, other), exit_px, _leg_side(ep, other))
                n_exits = 2
            else:
                pnl += leg_pnl(_entry_price(ep, "NEAR"), exit_px, _leg_side(ep, "NEAR"))
                pnl += leg_pnl(_entry_price(ep, "FAR"), exit_px, _leg_side(ep, "FAR"))
                combined = True
                n_exits = 2

        elif arm == "NO_REVT":
            pnl += leg_pnl(_entry_price(ep, "NEAR"), exit_px, _leg_side(ep, "NEAR"))
            pnl += leg_pnl(_entry_price(ep, "FAR"), exit_px, _leg_side(ep, "FAR"))
            combined = True
            n_exits = 2

        else:  # TREND_CONFIRMED_RELEASE
            confirmed = walk_trend_confirmation(keys, near, entry_ts, horizon_ts)
            if confirmed is not None:
                released = True
                rel_bar = bar_at_or_before(keys, near, confirmed.get("bar_ts", horizon_ts)) or \
                    bar_at_or_before(keys, near, horizon_ts)
                rel_px = rel_bar["close"]
                rel_leg = _counter_trend_leg(confirmed.get("direction"))
                pnl += leg_pnl(_entry_price(ep, rel_leg), rel_px, _leg_side(ep, rel_leg))
                other = "FAR" if rel_leg == "NEAR" else "NEAR"
                pnl += leg_pnl(_entry_price(ep, other), exit_px, _leg_side(ep, other))
                n_exits = 2
            else:
                pnl += leg_pnl(_entry_price(ep, "NEAR"), exit_px, _leg_side(ep, "NEAR"))
                pnl += leg_pnl(_entry_price(ep, "FAR"), exit_px, _leg_side(ep, "FAR"))
                combined = True
                n_exits = 2

        st["pnl"] += pnl
        st["pnls"].append(pnl)
        st["exit_count"] += n_exits
        st["release"] += 1 if released else 0
        st["combined"] += 1 if combined else 0

    n = len(st["pnls"])
    st["avg_pnl"] = round(st["pnl"] / n, 2) if n else 0.0
    st["max_drawdown"] = round(_peak_trough_drawdown(st["pnls"]), 2)
    denom = st["eligible"] + st["skipped"]
    st["coverage"] = round(st["eligible"] / denom, 4) if denom else 0.0
    return st


# ---------------------------------------------------------------------------
# entry points
def run_replay(fills_path: str = "", bars_root: str = "") -> dict:
    """Programmatic entry used by tests. Returns a summary dict."""
    root = bars_root or "."
    fills_path = fills_path or discover_fills_log(root)
    if not fills_path:
        return {"error": "no fills log found", "verdict": "HOLD"}
    fills = load_fills(str(fills_path))
    episodes = build_episodes(fills)
    near = load_bars(discover_bar_files(root, "near"), "near")
    far = load_bars(discover_bar_files(root, "far"), "far")
    keys = sorted(near.keys())
    arm_stats = {arm: _simulate_arm(arm, episodes, keys, near, far) for arm in ARMS}
    eligible_total = arm_stats[ARMS[0]]["eligible"]
    coverage = max((s["coverage"] for s in arm_stats.values()), default=0.0)
    verdict = "READY_FOR_APPROVAL" if eligible_total >= MIN_ELIGIBLE_FOR_APPROVAL else "HOLD"
    status = "OBSERVED" if eligible_total >= MIN_ELIGIBLE_FOR_APPROVAL else "RESEARCH_INSUFFICIENT_SAMPLE"
    return {
        "fills_path": str(fills_path),
        "near_files": len(discover_bar_files(root, "near")),
        "far_files": len(discover_bar_files(root, "far")),
        "episodes": len(episodes),
        "eligible": eligible_total,
        "status": status,
        "verdict": verdict,
        "coverage": coverage,
        "arms": arm_stats,
    }


def build_manifest(arms_stats: dict, eligible: int, coverage: float) -> dict:
    src = SELF_PATH.read_text(encoding="utf-8") if SELF_PATH.exists() else ""
    return {
        "harness": "mts_trend_replay_v1",
        "version": "1.0",
        "content_sha256": hashlib.sha256(src.encode("utf-8")).hexdigest(),
        "eligible": eligible,
        "coverage": round(coverage, 4),
        "coverage_per_arm": {k: v["coverage"] for k, v in arms_stats.items()},
        "arms": arms_stats,
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    root = argv[0] if argv else "."
    fills_path = argv[1] if len(argv) > 1 else ""
    fills_path = fills_path or discover_fills_log(root)
    if not fills_path:
        print("mts_trend_replay_v1: no fills log found under", root)
        return 2

    fills = load_fills(str(fills_path))
    episodes = build_episodes(fills)
    near = load_bars(discover_bar_files(root, "near"), "near")
    far = load_bars(discover_bar_files(root, "far"), "far")
    keys = sorted(near.keys())

    arm_stats = {arm: _simulate_arm(arm, episodes, keys, near, far) for arm in ARMS}
    eligible_total = arm_stats[ARMS[0]]["eligible"]
    verdict = "READY_FOR_APPROVAL" if eligible_total >= MIN_ELIGIBLE_FOR_APPROVAL else "HOLD"
    coverage_any = max((s["coverage"] for s in arm_stats.values()), default=0.0)
    manifest = build_manifest(arm_stats, eligible_total, coverage_any)

    print("=" * 76)
    print("MTS 2.0 TREND-RELEASE COUNTERFACTUAL REPLAY (mts_trend_replay_v1)")
    print("=" * 76)
    print(f"fills_log   : {fills_path}")
    print(f"episodes    : {len(episodes)}")
    print(f"near_files  : {len(discover_bar_files(root, 'near'))}")
    print(f"far_files   : {len(discover_bar_files(root, 'far'))}")
    print(f"eligibility : {eligible_total} (min required {MIN_ELIGIBLE_FOR_APPROVAL})")
    print(f"status      : {manifest['status'] if False else ('OBSERVED' if eligible_total>=MIN_ELIGIBLE_FOR_APPROVAL else 'RESEARCH_INSUFFICIENT_SAMPLE')}")
    print(f"verdict     : {verdict}")
    print("-" * 76)
    for arm, s in arm_stats.items():
        print(f"[{arm}]")
        print(f"  eligible={s['eligible']} skipped={s['skipped']} coverage={s['coverage']}")
        print(f"  pnl={round(s['pnl'],2)}  avg_pnl={s['avg_pnl']}  max_drawdown={s['max_drawdown']}")
        print(f"  release={s['release']} combined={s['combined']} exit_count={s['exit_count']}")
    print("=" * 76)

    out_dir = Path("scripts/research/output")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_meta = out_dir / "mts_trend_replay_v1_manifest.json"
    out_meta.write_text(json.dumps(manifest, indent=2))
    print(f"manifest -> {out_meta}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())