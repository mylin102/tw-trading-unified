# Quant Lab v1.1 修改計劃

## 背景

Quant Lab Operations Manual 經 CEO 審查後，識別出 5 個缺失：
1. 沒有策略生命週期管理（上線/衰退/退役）
2. 沒有資本回報 KPI 追蹤
3. 沒有降級協議 (Circuit Breaker)
4. 沒有決策日誌（所有變更不可追溯）
5. 沒有 ML 模型退化警報

本次修改補齊以上缺口，從 v1.0 升級至 v1.1。

---

## 執行順序

### Step 1: Strategy Health Monitor

**檔案**: `scripts/tools/strategy_health.py` + `tests/test_strategy_health.py`

**為什麼要做**:
手冊定義了「滾動 30 日 PF < 1.0 → 降級」和「滾動 90 日 PF < 0.8 → 退役」，
但沒有腳本執行這個檢查。目前策略上線後就沒有人管它，除非手動跑回測。

**解決的痛點**:
- VWAP Bounce 夜盤虧損 98%，如果自動偵測就能提前退役，不會等到手動審查才發現
- Paper 模式的策略可能在「慢性死亡」而沒有人知道
- CEO Review 只看當前狀態，不看趨勢

**功能**:
```
輸入: 交易記錄 (logs/trades/ 或 exports/trades/)
輸出: 策略健康報告 (PASS / WARN / FAIL / RETIRE)

檢查項目:
  - 滾動 30 日 PF
  - 滾動 30 日 MaxDD
  - 滾動 90 日 PF
  - 交易頻率退化（交易變少 = 策略找不到機會）
  - 參數敏感度（最佳參數是否孤立）

CLI:
  python3 scripts/tools/strategy_health.py                  # 檢查所有策略
  python3 scripts/tools/strategy_health.py --strategy counter_vwap --window 30d
  python3 scripts/tools/strategy_health.py --dry-run          # 只報告，不修改狀態
```

**依賴**: 無（只讀交易記錄，不依賴任何運行中服務）

**風險**: 低（純讀取 + 報告，不修改任何狀態）

---

### Step 2: Decision Logger

**檔案**: `core/decision_logger.py` + `tests/test_decision_logger.py`

**為什麼要做**:
目前策略切換（vol_squeeze → counter_vwap）只存在於 git log 裡，
沒有人能回答「為什麼在 4/12 改策略？」這個問題。
當策略出问题时，無法追溯「當時誰決定的？理由是什麼？」

**解決的痛點**:
- 策略改了、參數調了，三個月後忘了為什麼
- 出現問題時無法判定是「策略本身的問題」還是「參數改壞了」
- 無審計軌跡，不符合交易系統最佳實踐

**功能**:
```
API:
  log_decision(action="switch", strategy="vol_squeeze→counter_vwap",
               reason="PF 1.95 > 1.3", author="user", risk_level="medium")

輸出格式 (logs/decisions.csv):
  timestamp,action,strategy,reason,author,risk_level,status
  2026-04-12T14:00:00,switch,vol_squeeze→counter_vwap,PF 1.95 > 1.3,user,medium,active
  2026-04-12T14:05:00,param_edit,counter_vwap:atr_sl_mult=2.0→2.5,reduce DD,user,low,active

整合點:
  - config 修改時自動呼叫（hook）
  - Dashboard 參數套用時自動記錄
  - CLI 工具可手動記錄
```

**依賴**: 無（純寫 CSV，append-only，不讀取現有資料）

**風險**: 極低（只寫不讀，不會破壞現有狀態）

---

### Step 3: Circuit Breaker

**檔案**: `core/circuit_breaker.py` + `tests/test_circuit_breaker.py`

**為什麼要做**:
目前沒有自動停損機制。策略可以一直虧下去，
直到人工發現並手動關機。真實交易系統需要「保險絲」。

**解決的痛點**:
- Paper 模式可以無限虧損（雖然不賠錢，但浪費時間）
- 未來進 Live 時沒有 circuit breaker 就是賭命
- CEO Review 說「REJECTED」但策略還在跑

**功能**:
```
三級降級:
  Level 1 (日虧損 >= 2%): 口數降至 1, 記錄決策日誌
  Level 2 (日虧損 >= 5%): 暫停進場, 僅平倉現有部位, 通知用戶
  Level 3 (週虧損 >= 8%): 全面切換至 paper mode

API:
  breaker = CircuitBreaker(capital=100_000, daily_limit_pct=0.02, weekly_limit_pct=0.08)
  breaker.check_and_act(current_pnl, period="daily")  # 返回 action: CONTINUE / REDUCE / HALT / PAPER

整合點:
  - monitor.py 每次 PnL 更新時呼叫
  - Dashboard 顯示當前 breaker 狀態
  - CLI: python3 -c "from core.circuit_breaker import CircuitBreaker; ..."
```

**依賴**: Step 2（降級事件要記錄決策日誌）

**風險**: 中（會影響交易行為，需要測試邊界條件）
**測試要求**: 必須有單元測試覆蓋所有閾值邊界

---

### Step 4: Retired Strategies Directory

**檔案**: `strategies/retired/` + `strategies/retired/README.md`

**為什麼要做**:
退役策略不能直接刪除。理由：
1. 需要保留回測記錄（「這個策略曾經在什麼條件下賺過錢？」）
2. 需要保留參數（「當時的閾值是多少？」）
3. 需要保留退役理由（「為什麼淘汰？什麼時候可能復活？」）

**解決的痛點**:
- 直接刪除 = 失去歷史智慧
- 留在原處 = 可能被誤用（import 了退役策略）
- 沒有退役記錄 = 可能重複犯同樣的錯誤

**功能**:
```
目錄結構:
  strategies/retired/
  ├── README.md                     # 退役策略清單 + 理由
  ├── vwap_bounce.py                # 原始策略代碼（不執行）
  ├── momentum_burst.py
  ├── night_short_only.py
  └── ...

README.md 格式:
  | 策略 | 退役日期 | 原因 | 最後 PF | 最後 MaxDD | 可能復活條件 |
  | vwap_bounce | 2026-04-01 | 夜盤虧損 98%, 全日 PF=0.23 | 0.23 | -55% | 區間市 + 高流動性 |

不執行任何程式碼，只是文件歸檔。
```

**依賴**: Step 1（需要先知道哪些策略該退役）

**風險**: 無（純文件 + 程式碼搬移）

---

### Step 5: Dashboard Pipeline View

**檔案**: `ui/dashboard.py`（修改現有）

**為什麼要做**:
CEO/用戶需要一眼看到「整個研發管道」的狀態。
目前 Dashboard 只有「當前策略切換」和「參數調整」，
看不到「有哪些策略在回測中？有哪些在考慮中？有哪些已退役？」

**解決的痛點**:
- 策略選項只顯示在 dropdown 裡，看不到生命週期狀態
- 新策略加入時不知道它在哪個階段
- 無法快速評估「目前有多少策略可以上場？」

**功能**:
```
在 Dashboard 新增 Tab 或面板:

┌─ 策略管道 ─────────────────────────────────────────┐
│ 構想 (3)        │ 回測中 (2)   │ Paper (1) │ Live (0)│
│ ───────────────  │ ──────────── │ ──────── │ ─────── │
│ • PSAR v2       │ • orb_ml     │ Counter- │         │
│ • Mean Revert   │ • KalmanMom  │   VWAP   │         │
│ • Order Flow    │              │          │         │
│                                                         │
│ 退役 (7): vwap_bounce, momentum_burst, night_short...   │
│                                                         │
│ 總計: 13 策略 | 活躍: 1 | 待上線: 5                      │
└─────────────────────────────────────────────────────────┘
```

**依賴**: Step 4（退役策略目錄）

**風險**: 低（純 UI 改動，不影響交易邏輯）

---

### Step 6: ML Model Accuracy Drift Tracking

**檔案**: `scripts/optimization/train_rf.py`（修改現有）

**為什麼要做**:
Random Forest 模型訓練後沒有記錄準確度趨勢。
如果市場結構改變，模型會慢慢失效，但沒有人知道。

**解決的痛點**:
- orb_ml 策略依賴模型預測，模型退化 = 策略退化
- 目前只有 `models/orb_rf_v2.pkl`，沒有 accuracy 記錄
- 無法回答「v2 比 v1 好多少？」

**功能**:
```
修改 train_rf.py，每次訓練後記錄:
  - 訓練 accuracy
  - OOB (out-of-bag) score
  - 特徵重要度排名變化
  - 與上一個模型的 accuracy 差異

輸出: data/optimization/model_history.csv
  version,timestamp,train_acc,oob_score,f1_score,top3_features,prev_acc_diff
  v2,2026-04-10T10:00:00,0.72,0.68,0.65,kalman_trend;lrl_curv;atr, -0.03

整合:
  - strategy_health.py 讀取此檔案，檢查模型是否退化
  - 如果最新模型 accuracy < 上一版本 -0.05 → 發出預警
```

**依賴**: 無（獨立修改）

**風險**: 低（只追加記錄，不修改現有訓練邏輯）

---

## 測試計劃

每個 Step 對應一個測試檔：

| Step | 測試檔 | 關鍵測試 |
|------|--------|---------|
| 1 | `tests/test_strategy_health.py` | 模擬不同 PF/MaxDD 情境的判定結果 |
| 2 | `tests/test_decision_logger.py` | CSV 寫入正確性、append-only 性質 |
| 3 | `tests/test_circuit_breaker.py` | 三級降級閾值邊界測試 |
| 4 | 無測試（文件歸檔） | — |
| 5 | 無單元測試（手動 UI 測試） | — |
| 6 | `tests/test_model_history.py` | 準確度記錄/讀取/drift 偵測 |

所有測試加入 CI：`python3 -m pytest tests/ -v`

---

## 風險評估

| 風險 | 影響 | 機率 | 緩解 |
|------|------|------|------|
| Circuit Breaker 誤觸發 | 中 | 低 | 有 dry-run 模式 + 單元測試 |
| 決策日誌寫入衝突 | 低 | 低 | append-only CSV，不支援併發寫入 |
| Dashboard 改動影響現有 UI | 低 | 低 | 新增 Tab，不修改現有元件 |
| 退役策略搬移導致 import 錯誤 | 低 | 低 | 保留原始位置（moved 註解），只搬程式碼 |

---

## 不做項目

| 項目 | 理由 |
|------|------|
| 月 Alpha 對大盤計算 | 需要外部大盤數據源，目前只有 TMF/TXO |
| 研發投入時間追蹤 | 主觀且無自動化采集方式 |
| 即時監控告警 (Slack/Line) | 過度複雜，先用 CLI + Dashboard 即可 |
| 多策略同時執行的資金分配優化 | 目前只有一個活躍策略，不需要 |

---

## 成功標準

完成後，手冊應該回答以下問題：
1. ✅ 「Counter-VWAP 現在健康嗎？」→ `python3 scripts/tools/strategy_health.py`
2. ✅ 「為什麼 4/12 改了策略？」→ `cat logs/decisions.csv`
3. ✅ 「今天虧損超過 2% 了嗎？」→ Circuit Breaker 自動降口數
4. ✅ 「有哪些策略退役了？為什麼？」→ `cat strategies/retired/README.md`
5. ✅ 「ML 模型 v2 比 v1 好嗎？」→ `cat data/optimization/model_history.csv`
6. ✅ 「目前有多少策略可以上場？」→ Dashboard 管道 view
7. ✅ 「日盤/夜盤排行榜分別是什麼？」→ Dashboard 分開顯示，互不混淆
8. ✅ 「日盤檢討會影響夜盤嗎？」→ 不會。各自獨立循環

## 日盤/夜盤獨立原則

**核心設計**: 日盤檢討 → 下次日盤套用。夜盤檢討 → 下次夜盤套用。

- 日盤排行榜只看日盤歷史 (Counter PF=2.1)
- 夜盤排行榜只看夜盤歷史 (Counter PF=1.4)
- 配置分離: `config/futures_day.yaml` vs `config/futures_night.yaml`
- 停損分開: 日盤 60pts, 夜盤 80pts（流動性不同）

---

**預估工作量**: 3-4 個開發 session
**預估測試**: +30 測試
**預估程式碼**: ~600 行新增 + ~100 行修改
