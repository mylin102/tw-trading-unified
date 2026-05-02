# adaptive_orb v1.5 — 開發路徑

**日期**: 2026-05-02
**Base**: `adaptive_orb_v1` (現行版本, PF=1.10, MFE/MAE=1.74, CAGR=62%)

## 原則

**不要為了提高勝率犧牲 MFE/MAE ratio。**

v1 勝率低 (27%) 但 MFE/MAE=1.74，這才是趨勢策略的核心 edge。
v2 把持倉從 236 bars 壓到 15 bars，MFE/MAE 掉到 1.02，edge 被砍掉。

## v1 → v1.5 增量變更

### 加入 (from v2)
- ATR-normalized breakout_strength 欄位 (作為 regime classifier 的輸入)
- Strategy Router v2: `STRATEGY_POLICY` dict (已實作)
  - `KILL_SWITCH` by CAGR
  - `REGIME_BLOCKED` 日誌
- `CONFIRMED_BREAKOUT` filter (bs >= 0.25 + volume_spike >= 1.5 + close > vwap)

### 保留 (from v1)
- `_range_high` / `_range_low` ORB 範圍
- `scout` entry (不要改)
- 長持倉 exit 邏輯 (v1 的 stop_loss / trailing stop / trend hold)
- MFE/MAE ≥ 1.5 的現狀

### 不加入 (from v2)
- ❌ `EARLY_BREAKOUT` scout (0.3 size) — v2 回測淨虧 −$35K
- ❌ 1.5x ATR tight exit — MFE/MAE 毀滅器
- ❌ 300 bars→15 bars 的持倉縮短

## 實作順序建議

1. 先確認 v1 現行 exit 參數 (stop_loss_pts=60, trailing_stop_trigger=150)
2. 在 `futures_bar_regime.py` 加入 volume_spike 閘門 (Phase 3)
3. 把 v1 的 regime 分類和 Strategy Router v2 對接
4. 回測驗證 MFE/MAE 不退化

## 關鍵對比數字

| 指標 | v1 (現行) | v1.5 (目標) | v2 (參考) |
|------|-----------|-------------|-----------|
| PF | 1.10 | ≥ 1.10 | 1.04 |
| MFE/MAE | 1.74 | ≥ 1.70 | 1.02 |
| Win Rate | 27.4% | ≥ 30% | 36.1% |
| Avg Hold | 236 bars | ≥ 150 bars | 15 bars |
| Max DD | $1.1M | ≤ $1.1M | $1.2M |
