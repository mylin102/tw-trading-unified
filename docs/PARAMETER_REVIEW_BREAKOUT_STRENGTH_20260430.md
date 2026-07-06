# 參數與公式檢討報告：突破強度 (Breakout Strength)

**日期**：2026-05-02 (檢討 2026-04-30 交易異常)
**狀態**：✅ 已實作 (Implemented)
**編號**：PR-2026-001

## 1. 現象描述 (Symptoms)
在 2026-04-30 16:40 (夜盤)，系統顯示：
- **Score**: 80.0 (極強多頭)
- **Trend**: BULL
- **Sqz**: Unlock (釋放)
- **結果**: **未產生任何期貨交易**。原因為 `bull_breakout` (0.1817) 未達舊門檻 (0.20%)。

## 2. 核心升級：V-Model 突破引擎 (V-Model Breakout Engine)
系統已從「絕對百分比」模型升級為「波動率正規化 + 三段式確認」模型。

### 2.1 新公式 (The Formula)
$$Breakout Strength = \frac{Close - High_{20}.shift(1)}{ATR}$$
*   **優勢**：使用 ATR 進行正規化，使門檻具備市場適應性。
*   **精準度**：使用 `.shift(1)` 排除當前 Bar 自身的高點影響。
*   **安全性**：加入 ATR Floor (預設 50 pts) 避免低波環境下數值爆炸。

### 2.2 三段式確認邏輯 (Three-Stage Confirmation)
僅當滿足以下三個條件時，系統才會確認 `TREND` 或 `BEAR` regime：
1.  **結構突破 (Structure)**：`Close > High20` (即 `BS > 0`)。
2.  **強度門檻 (Strength)**：`BS >= Threshold` (依市場模式動態調整)。
3.  **行為確認 (Confirmation)**：`Volume Spike >= 1.5` 且 `Close > VWAP` (多頭) 或 `Close < VWAP` (空頭)。

## 3. 動能感知門檻 (Regime-Aware Thresholds)
系統會根據當前大盤模式自動調整突破敏感度：

| 大盤模式 (Session Regime) | 修正倍數 (Multiplier) | 實際門檻 (Threshold) | 說明 |
| :--- | :--- | :--- | :--- |
| **SQUEEZE** (盤整) | 1.0x | **0.25 ATR** | 突破需較強，避免假突破。 |
| **TRENDING** (趨勢) | 0.6x | **0.15 ATR** | 趨勢已成，門檻降低以利追價。 |
| **NORMAL/SHOCK** | 1.0x | **0.25 ATR** | 基準門檻。 |

## 4. 回測結果：門檻敏感度分析 (Backtest Results)

| 門檻 (Threshold) | 多頭趨勢 | 空頭趨勢 | 交易數 | 獲利因子(PF) | 勝率(WP) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **0.15** | 27,820 | 27,205 | 2,208 | **1.09** | **26.7%** |
| **0.20** | 27,072 | 26,413 | 2,196 | 1.08 | 26.5% |
| **0.25 (現行)** | 26,205 | 25,529 | 2,196 | 1.08 | 26.4% |
| **0.30** | 25,323 | 24,609 | 2,171 | 1.09 | 26.6% |

### 4.1 解讀
- **0.15** 最佳數據，PF=1.09, WR=26.7%，比現行 0.25 多 6.2% TREND 訊號。
- **0.20** 折衷選擇，PF=1.08, WR=26.5%，比 0.25 多 3.3% TREND。
- **0.25** (現行) 略保守但差距微小。
- **0.30** 過緊，排除最多可交易訊號，但 PF 未改善。

### 4.2 建議
**暫維持 0.25 (現行門檻)**。PF 和 WR 在所有門檻間差異不足 2%，
降低門檻會增加訊號但無顯著 PF 改善，反而引入更多假突破風險。

## 5. 代碼變更記錄 (Change Log)
- ✅ `futures_bar_regime.py`: 實作三段式確認與動能感知門檻。
- ✅ `date_utils.py`: 修正休市判斷邏輯（包含五一勞動節）。
- ✅ `indicators.py`: 實作 ATR 正規化與 Shift(1) 邏輯。
- ✅ `scripts/backtest_adaptive_orb_breakout_threshold_sweep.py`: 新增門檻回測腳本 (2026-05-02)。
- ✅ `docs/PARAMETER_REVIEW_BREAKOUT_STRENGTH_20260430.md`: 新增第4節回測結果 (2026-05-02)。

### 5.1 本次變更 (2026-05-02)
- ✅ 完成 0.15/0.20/0.25/0.30 四組門檻回測，PF 與 WR 差異 < 2%
- ✅ 建議維持 0.25 現行門檻
- 參見 `scripts/backtest_adaptive_orb_breakout_threshold_sweep.py`

## 6. Shadow Live 驗證準則 (Next Week)
觀察期：2026-05-04 ~ 2026-05-06

| 檢查項 (Audit Item) | 通過條件 (Success Criteria) | 數據源 |
| :--- | :--- | :--- |
| **ATR Floor** | `atr_used >= atr_floor` 且低波動時 `BS_ATR` 不爆衝 | `router_trace` |
| **Session Buffer** | 15:00–15:25 不因單一 `Volume Spike` 觸發進場 | `router_trace` |
| **Direction Lock** | 結構突破方向 (🚀/💀) 與 Regime Policy 嚴格一致 | Dashboard/Log |

## 7. 自動化審計工具 (v15_daily_audit)
已開發 `scripts/v15_daily_audit.py`，每日收盤後運行：
```bash
python3 scripts/v15_daily_audit.py [YYYYMMDD]
```
輸出包含：`ATR_GATE_PASS/FAIL` 次數、`SESSION_BUFFER_SKIP` 次數、`REGIME_BLOCKED` 次數及實際交易 PnL 摘要。

---
**簽署**：Gemini CLI Agent
