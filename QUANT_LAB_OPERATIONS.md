# 🔬 Taiwan Trading Unified - Quant Lab Operations Manual

本手冊記載了 Wave 1-17 重構後，系統的核心研發流程。

## 1. 數據層 (Data Foundation)
系統使用 Parquet 作為單一真理來源 (SSOT)。
- **歷史庫位置**: `data/historical/TXFR1_5m.parquet`
- **擴充數據**: 
  ```bash
  python3 scripts/sync/expand_history.py --ticker TXFR1 --years 3
  ```
- **數據檢查**: 使用 Dashboard 的「Data Mgmt」頁面進行裂縫分析。

## 2. 物理指標層 (Indicators)
指標計算已全面 NumPy 向量化，支援 80 萬筆級別的高速運算。
- **核心指標**: 
  - `Kalman Filter`: 降噪趨勢估算。
  - `LRL Curvature`: 線性回歸加速度（彎曲方向）。
- **註冊新指標**: 在 `core/data_enricher.py` 中使用 `enricher.register()`。

## 3. AI 機器學習流水線 (ML Pipeline)
這是提升勝率的核心路徑。
- **Step A: 特徵提取**
  ```bash
  python3 scripts/optimization/extract_features.py
  ```
  *會自動執行逐月流式提取，產出 `data/optimization/orb_ml_dataset.csv`。*
- **Step B: 模型訓練**
  ```bash
  python3 scripts/optimization/train_rf.py
  ```
  *訓練隨機森林模型，產出權重分析與 `models/orb_rf_v2.pkl`。*
- **Step C: 策略應用**
  策略 `orb_ml` 會自動載入最新模型並執行部位縮放。

## 4. 自動化優化 (Optimization)
- **並行網格搜索**: 使用 Dashboard 的「🔬 參數掃描優化」頁面。
- **年度魯棒性分析**:
  ```bash
  python3 scripts/optimization/annual_sweep.py
  ```

## 5. 風險管理 (Risk Audit)
- **蒙地卡羅測試**: 在回測報告頁面查看「95% VaR」與「破產機率」。
- **部位縮放**: 基於 AI 信心值的 1/2/3 口動態下單邏輯已整合在 `orb_ml`。

## 6. 策略生命週管理 (Strategy Lifecycle)

### 上線門檻 (Entry Gate)
策略必須同時滿足以下條件才能進入 Paper 階段：
| 條件 | 閾值 | 驗證方式 |
|------|------|---------|
| Profit Factor | >= 1.3 | 回測（樣本外） |
| Max Drawdown | <= -15% | 蒙地卡羅 95% CI |
| 交易筆數 | >= 30 | 回測期間 >= 1 個月 |
| Win Rate | >= 30% | 統計檢定 |

### 衰退偵測 (Decay Detection)
- **滾動 30 日 PF < 1.0** → 策略自動降級至「觀察」狀態
- **滾動 30 日 MaxDD 突破歷史 1.5x** → 發出預警通知
- 偵測腳本: `python3 scripts/tools/strategy_health.py --window 30d`

### 退役條件 (Retirement)
策略出現以下任一狀況即退役：
- 滾動 90 日 PF < 0.8
- MaxDD 突破 -20%
- 參數敏感度測試顯示最佳參數孤立（過擬合）

退役策略移入 `strategies/retired/`，保留回測記錄但不再執行。

### 現役策略管道 (Active Pipeline)
```
構想: 3 → 回測中: 2 → Paper: 1 (Counter-VWAP) → Live: 0 → 退役: 7
```

## 7. 資本回報儀表板 (Capital Returns)

### 核心 KPI（CEO 只看這 4 個數字）
| KPI | 計算方式 | 目前值 |
|-----|---------|--------|
| 月 Alpha | 策略月回報 − 大盤同期回報 | 待計算 |
| 月研發投入 | 開發 + 回測 + 優化時間 | 待追蹤 |
| 資本效率 | 月淨利 / 使用保證金 | 待計算 |
| ROI | 月淨利 / 100,000 TWD | 待計算 |

### 檢視方式
```bash
python3 scripts/tools/ceo_review.py              # 完整審查
python3 scripts/tools/ceo_review.py --history     # 歷史記錄
```
報告自動存於 `logs/ceo_reviews/`，含 verdict（CLEARED / CONDITIONAL / REJECTED）。

## 8. 降級協議 (Circuit Breaker)

自動觸發，無須人工判斷：

| 觸發條件 | 動作 | 復原方式 |
|---------|------|---------|
| 日虧損 >= 2% | 口數降至 1 | 次日重置 |
| 日虧損 >= 5% | 暫停進場，僅監控 | 人工覆盤後手動恢復 |
| 週虧損 >= 8% | 全面切換至 paper mode | 策略健康檢查通過後恢復 |
| API 斷線 > 60s | 暫停進場 | 自動恢復 |

### 日盤/夜盤獨立循環

**核心原則**: 日盤檢討日盤，夜盤檢討夜盤。各自排行榜分開計算。

| | 日盤 (08:45-13:45) | 夜盤 (15:00-05:00) |
|--|-------------------|-------------------|
| 排行榜 | 日盤專用，只看日盤歷史 | 夜盤專用，只看夜盤歷史 |
| 收盤檢討 | 13:50 → 更新 `config/futures_day.yaml` | 05:05 → 更新 `config/futures_night.yaml` |
| 下次套用 | 隔日 08:45 | 當日 15:00 |
| 互不影響 | 日盤虧損不會導致夜盤換策略 | 夜盤虧損不會導致日盤換策略 |

### 策略切換速度

| 層級 | 切換對象 | 反應時間 | 審批 |
|------|---------|---------|------|
| 盤中 (L3) | 同 Session 內換策略 | < 5 秒 | 自動 |
| 收盤後 (L2) | 下次同類型 Session | 5-10 分鐘 | 自動 |
| 週度 (L1) | 策略上線/退役 | 下次開盤前 | 自動 + 通知 |

### 決策日誌 (Decision Log)
所有策略切換/參數修改/降級事件記錄於 `logs/decisions.csv`：
```
timestamp,action,strategy,reason,author,status
2026-04-12T14:00,switch,vol_squeeze→counter_vwap,PF 1.95 > 1.3,config,active
```

---
**GStack Engineering - Standard Operating Procedure v1.1**

### CEO 審查摘要 (v1.1 Review)
| 維度 | 狀態 | 說明 |
|------|------|------|
| 策略生命週 | ✅ 新增 | 上線/衰退/退役三階段 |
| 資本回報 | ✅ 新增 | 4 個核心 KPI + CEO Review CLI |
| 降級協議 | ✅ 新增 | 自動觸發，3 級降級 |
| 決策日誌 | ✅ 新增 | 所有變更可追溯 |
| 日/夜盤獨立 | ✅ 新增 | 排行榜分開、收盤檢討獨立循環 |
| 收盤後檢討 | ✅ 新增 | 日盤檢討→下次日盤、夜盤檢討→下次夜盤 |
| 退化警報 | ⏳ 待做 | 模型準確度 drift 追蹤 |
| 研發管道可視化 | ⏳ 待做 | Dashboard 新增 pipeline view |
