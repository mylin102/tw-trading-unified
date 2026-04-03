#!/usr/bin/env python3
"""
暴力搜索能賺錢的選擇權策略組合：
1. 買方 + 嚴格過濾（bull_align guard, 連續確認）
2. 賣方 credit（score 低迷時賣，收 theta）
3. 買方 + 反轉過濾（只在 squeeze fire 時進場）
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "strategies" / "options"))

import pandas as pd
import numpy as np
from strategies.options.options_engine.engine.indicators import calculate_futures_squeeze, calculate_mtf_alignment

DATA = pathlib.Path.home() / "Documents/mylin102/tw-option-squeeze-trading/exports/tmf_replay_5min_q1_2026.csv"
df_raw = pd.read_csv(DATA, parse_dates=["datetime"], index_col="datetime")
df_5m = calculate_futures_squeeze(df_raw)
df_15m = calculate_futures_squeeze(
    df_raw.resample("15min", label="right", closed="left").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna(subset=["Open"])
)
df_1h = calculate_futures_squeeze(
    df_raw.resample("1h", label="right", closed="left").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna(subset=["Open"])
)

df_15m_a = df_15m[["momentum", "mom_state", "ema_fast", "ema_slow"]].reindex(df_5m.index, method="ffill")
df_1h_a = df_1h[["momentum", "mom_state"]].reindex(df_5m.index, method="ffill")

def calc_scores(w):
    out = []
    for i in range(len(df_5m)):
        d = {"5m": pd.DataFrame([{"momentum": df_5m.iloc[i]["momentum"], "mom_state": df_5m.iloc[i]["mom_state"]}])}
        if not pd.isna(df_15m_a.iloc[i]["momentum"]):
            d["15m"] = pd.DataFrame([{"momentum": df_15m_a.iloc[i]["momentum"], "mom_state": df_15m_a.iloc[i]["mom_state"]}])
        if not pd.isna(df_1h_a.iloc[i]["momentum"]):
            d["1h"] = pd.DataFrame([{"momentum": df_1h_a.iloc[i]["momentum"], "mom_state": df_1h_a.iloc[i]["mom_state"]}])
        out.append(calculate_mtf_alignment(d, weights=w)["score"])
    return np.array(out)

scores = calc_scores({"5m": 0.2, "15m": 0.4, "1h": 0.4})
close = df_5m["Close"].values
vwap = df_5m["vwap"].values
sqz_on = df_5m["sqz_on"].values
fired = df_5m["fired"].values
bull_align = df_5m["bullish_align"].values
bear_align = df_5m["bearish_align"].values
mom_state = df_5m["mom_state"].values
n = len(df_5m)

PV = 50
DELTA = 0.5
ENTRY_P = 100
THETA = 0.3

# ═══════════════════════════════════════
# Strategy 1: 買方 + bull_align guard + squeeze fire 確認
# ═══════════════════════════════════════
def strat_buy_filtered(entry_score, sl_pct, tp_pct, cooldown, trail_pct, require_fire):
    pos = 0
    entry_p = 0
    entry_mtx = 0
    has_tp1 = False
    side = None
    peak = 0
    cd = 0
    pnl = 0
    trades = 0
    wins = 0

    for i in range(1, n):
        c, s = close[i], scores[i]
        if pos > 0:
            diff = (c - entry_mtx) * (1 if side == "C" else -1)
            cur = entry_p + diff * DELTA - THETA
            cur = max(cur, 0.1)
            if cur > peak:
                peak = cur
            # TP1
            if not has_tp1 and pos == 2 and (cur - entry_p) / entry_p >= tp_pct:
                pnl += (cur - entry_p) * PV
                trades += 1
                wins += 1
                pos = 1
                has_tp1 = True
            # Trailing
            if trail_pct > 0 and has_tp1 and cur <= peak * (1 - trail_pct):
                pnl += (cur - entry_p) * PV * pos
                trades += 1
                if cur > entry_p:
                    wins += 1
                pos = 0
                cd = cooldown
                continue
            # Stop
            threshold = entry_p if has_tp1 else entry_p * (1 - sl_pct)
            if cur <= threshold:
                pnl += (cur - entry_p) * PV * pos
                trades += 1
                pos = 0
                cd = cooldown
                continue

        if pos == 0:
            if cd > 0:
                cd -= 1
                continue
            # bull_align guard
            if s >= entry_score and c > vwap[i] and bull_align[i]:
                if require_fire and not fired[i]:
                    continue
                side = "C"
            elif s <= -entry_score and c < vwap[i] and bear_align[i]:
                if require_fire and not fired[i]:
                    continue
                side = "P"
            else:
                continue
            pos = 2
            entry_p = ENTRY_P
            entry_mtx = c
            has_tp1 = False
            peak = ENTRY_P

    wr = wins / trades * 100 if trades > 0 else 0
    return {"trades": trades, "win_rate": wr, "net_pnl": pnl, "avg": pnl / trades if trades else 0}

# ═══════════════════════════════════════
# Strategy 2: 賣方 — score 在低區間時賣選擇權收 theta
# 當 abs(score) < sell_threshold 且沒有 squeeze → 賣 straddle/strangle
# 簡化：每根 bar 收 theta，如果價格大幅移動就虧
# ═══════════════════════════════════════
def strat_sell_theta(sell_threshold, max_move_pts, hold_bars):
    pos = 0
    bars_held = 0
    entry_mtx = 0
    pnl = 0
    trades = 0
    wins = 0

    for i in range(1, n):
        c, s = close[i], scores[i]
        if pos > 0:
            bars_held += 1
            move = abs(c - entry_mtx)
            # 每根收 theta
            if move >= max_move_pts:
                # 被穿價，虧 delta * move - theta collected
                loss = move * DELTA * PV - bars_held * THETA * PV
                pnl -= abs(loss)
                trades += 1
                pos = 0
                continue
            if bars_held >= hold_bars:
                # 時間到，收 theta 利潤
                gain = bars_held * THETA * PV - move * DELTA * PV * 0.3  # partial delta loss
                pnl += gain
                trades += 1
                if gain > 0:
                    wins += 1
                pos = 0
                continue

        if pos == 0:
            # 進場條件：低波動、score 平淡、squeeze on（壓縮中）
            if abs(s) < sell_threshold and sqz_on[i]:
                pos = 1
                entry_mtx = c
                bars_held = 0

    wr = wins / trades * 100 if trades > 0 else 0
    return {"trades": trades, "win_rate": wr, "net_pnl": pnl, "avg": pnl / trades if trades else 0}

# ═══════════════════════════════════════
# Strategy 3: 只在 squeeze fire 進場（最嚴格）
# ═══════════════════════════════════════
def strat_fire_only(sl_pct, tp_pct, cooldown, trail_pct):
    pos = 0
    entry_p = 0
    entry_mtx = 0
    has_tp1 = False
    side = None
    peak = 0
    cd = 0
    pnl = 0
    trades = 0
    wins = 0

    for i in range(1, n):
        c, _s = close[i], scores[i]
        if pos > 0:
            diff = (c - entry_mtx) * (1 if side == "C" else -1)
            cur = entry_p + diff * DELTA - THETA
            cur = max(cur, 0.1)
            if cur > peak:
                peak = cur
            if not has_tp1 and pos == 2 and (cur - entry_p) / entry_p >= tp_pct:
                pnl += (cur - entry_p) * PV
                trades += 1
                wins += 1
                pos = 1
                has_tp1 = True
            if trail_pct > 0 and has_tp1 and cur <= peak * (1 - trail_pct):
                pnl += (cur - entry_p) * PV * pos
                trades += 1
                if cur > entry_p:
                    wins += 1
                pos = 0
                cd = cooldown
                continue
            threshold = entry_p if has_tp1 else entry_p * (1 - sl_pct)
            if cur <= threshold:
                pnl += (cur - entry_p) * PV * pos
                trades += 1
                pos = 0
                cd = cooldown
                continue

        if pos == 0:
            if cd > 0:
                cd -= 1
                continue
            if not fired[i]:
                continue
            # fire 時看 momentum 方向
            if mom_state[i] >= 3 and c > vwap[i] and bull_align[i]:
                side = "C"
            elif mom_state[i] <= 0 and c < vwap[i] and bear_align[i]:
                side = "P"
            else:
                continue
            pos = 2
            entry_p = ENTRY_P
            entry_mtx = c
            has_tp1 = False
            peak = ENTRY_P

    wr = wins / trades * 100 if trades > 0 else 0
    return {"trades": trades, "win_rate": wr, "net_pnl": pnl, "avg": pnl / trades if trades else 0}


print(f"數據: {df_5m.index[0].date()} ~ {df_5m.index[-1].date()} ({n} bars)\n")

# ── Strategy 1: 買方 + 過濾 ──
print("="*65)
print("📊 Strategy 1: 買方 + bull_align guard")
print("="*65)
best1 = None
for es in [60, 70, 80, 90]:
    for sl in [0.20, 0.30, 0.40]:
        for tp in [0.3, 0.5, 0.8]:
            for cd in [3, 6, 12]:
                for fire in [False, True]:
                    r = strat_buy_filtered(es, sl, tp, cd, 0.15, fire)
                    if r["trades"] >= 5 and (best1 is None or r["net_pnl"] > best1["net_pnl"]):
                        best1 = {**r, "es": es, "sl": sl, "tp": tp, "cd": cd, "fire": fire}

if best1:
    print(f"  最佳: entry={best1['es']} sl={best1['sl']*100:.0f}% tp={best1['tp']*100:.0f}% cd={best1['cd']} fire={best1['fire']}")
    print(f"  交易: {best1['trades']}  勝率: {best1['win_rate']:.1f}%  淨損益: {best1['net_pnl']:+,.0f}  平均: {best1['avg']:+,.0f}")

# ── Strategy 2: 賣方 theta ──
print(f"\n{'='*65}")
print("📊 Strategy 2: 賣方 theta (squeeze 壓縮時賣)")
print("="*65)
best2 = None
for thresh in [10, 20, 30, 40, 50]:
    for max_mv in [50, 100, 150, 200]:
        for hb in [6, 12, 24, 36]:
            r = strat_sell_theta(thresh, max_mv, hb)
            if r["trades"] >= 5 and (best2 is None or r["net_pnl"] > best2["net_pnl"]):
                best2 = {**r, "thresh": thresh, "max_mv": max_mv, "hb": hb}

if best2:
    print(f"  最佳: threshold={best2['thresh']} max_move={best2['max_mv']}pts hold={best2['hb']}bars")
    print(f"  交易: {best2['trades']}  勝率: {best2['win_rate']:.1f}%  淨損益: {best2['net_pnl']:+,.0f}  平均: {best2['avg']:+,.0f}")

# ── Strategy 3: Squeeze Fire Only ──
print(f"\n{'='*65}")
print("📊 Strategy 3: Squeeze Fire Only (最嚴格買方)")
print("="*65)
best3 = None
for sl in [0.20, 0.30, 0.40, 0.50]:
    for tp in [0.3, 0.5, 0.8, 1.0]:
        for cd in [3, 6, 12]:
            r = strat_fire_only(sl, tp, cd, 0.15)
            if r["trades"] >= 3 and (best3 is None or r["net_pnl"] > best3["net_pnl"]):
                best3 = {**r, "sl": sl, "tp": tp, "cd": cd}

if best3:
    print(f"  最佳: sl={best3['sl']*100:.0f}% tp={best3['tp']*100:.0f}% cd={best3['cd']}")
    print(f"  交易: {best3['trades']}  勝率: {best3['win_rate']:.1f}%  淨損益: {best3['net_pnl']:+,.0f}  平均: {best3['avg']:+,.0f}")

# ── 總結 ──
print(f"\n{'='*65}")
print("🏆 總結")
print("="*65)
for name, b in [("買方+過濾", best1), ("賣方theta", best2), ("Fire Only", best3)]:
    if b:
        status = "✅ 賺錢" if b["net_pnl"] > 0 else "❌ 虧損"
        print(f"  {name}: {status}  {b['net_pnl']:+,.0f} TWD ({b['trades']} trades, {b['win_rate']:.0f}% WR)")
