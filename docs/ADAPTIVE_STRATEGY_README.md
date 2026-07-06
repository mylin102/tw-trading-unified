# 自適應策略系統 (Adaptive Strategy System) — 完整實作紀錄

## 概述

從 Phase 0 到 Phase 4，將交易系統從「單一策略用到底」升級為「三層自適應診斷系統」。

**核心改變**:
- 連虧 3 筆不再是「機械式換策略」，而是「診斷根因 → 對症下藥」
- 日盤/夜盤完全獨立：排行榜分開、Config 分開、檢討分開
- 所有決策可追溯（決策日誌）

## 架構

```
┌─────────────────────────────────────────────────────────┐
│  Level 3: 盤中 (Intra-Session)                          │
│  • 每筆平倉後 → 連續虧損計數器                           │
│  • 3 連虧 → 診斷引擎 → TIGHTEN / COOLDOWN / HALT / SWITCH│
│  • Circuit Breaker → 日虧損 2%/5% 自動降級               │
│  • 每小時審計 → DATA_FAILURE / NO_SIGNALS / COOLDOWN     │
├─────────────────────────────────────────────────────────┤
│  Level 2: 收盤後 (Post-Session)                         │
│  • 日盤 13:50 → 檢討 → 更新 futures_day.yaml            │
│  • 夜盤 05:05 → 檢討 → 更新 futures_night.yaml          │
│  • 診斷引擎 → 自動調整 confirm_bars / min_momentum       │
├─────────────────────────────────────────────────────────┤
│  Level 1: 週度 (Strategic)                              │
│  • 每週一 → 週報 → 日/夜盤各自 PF、勝率、策略管道         │
└─────────────────────────────────────────────────────────┘
```

## 診斷引擎決策樹

```
連虧 3 筆 → diagnose_losing_streak()
  │
  ├─ 全部 STOP_LOSS
  │   ├─ avg vwap_distance > 2x ATR → TIGHTEN(confirm_bars +3)
  │   │   理由: 進場離 VWAP 太遠 = 追價
  │   └─ avg momentum < 30 → TIGHTEN(min_momentum +20)
  │       理由: 動能不夠 = 弱訊號
  │
  ├─ 全部 VWAP_EXIT
  │   └─ TIGHTEN(min_momentum +20)
  │       理由: 趨勢強度不夠，被 VWAP 掃出場
  │
  ├─ 有交易在 SHOCK regime
  │   └─ HALT
  │       理由: SHOCK 不適合交易
  │
  ├─ 混合退出 + < 5 筆
  │   └─ COOLDOWN 15min
  │       理由: PF=2.1 本來就有 40% 會輸，正常變異
  │
  └─ 混合退出 + 5+ 筆
      └─ 算 rolling PF → < 1.0 才換策略
```

## 日盤/夜盤獨立

| 項目 | 日盤 | 夜盤 |
|------|------|------|
| 停損 | 60 pts | 80 pts |
| ATR mult | 1.8x | 2.2x |
| 滑價 | 1 pt | 2 pts |
| confirm_bars | 7 | 10 |
| min_momentum | 30 | 50 |
| 排行榜 | 日盤專用 | 夜盤專用 |
| Config | futures_day.yaml | futures_night.yaml |

## 檔案清單

### Core (新/改)
| 檔案 | 功能 |
|------|------|
| `core/signal.py` | Signal.validate() + to_dict() |
| `core/circuit_breaker.py` | 三級降級：DIAGNOSE / HALT / REDUCE_SIZE |
| `core/decision_logger.py` | Append-only 決策日誌 (logs/decisions.csv) |
| `core/diagnostic_engine.py` | 根因診斷引擎 |
| `core/session_config.py` | 日/夜盤 Config 管理器 (atomic write-back) |
| `core/strategy_registry.py` | 策略績效排行榜 (day_pf / night_pf) |
| `core/market_regime.py` | 防禦性：缺少欄位時回傳 NEUTRAL |
| `strategies/futures/monitor.py` | 整合所有組件 + Spring validate 修補 |

### Config
| 檔案 | 功能 |
|------|------|
| `config/futures_day.yaml` | 日盤專用參數 |
| `config/futures_night.yaml` | 夜盤專用參數 |

### Scripts
| 檔案 | 功能 |
|------|------|
| `scripts/daily_review.py` | 收盤後檢討 + 自動寫入 Config |
| `scripts/weekly_report.py` | 每週戰略報告 |
| `scripts/tools/ceo_review.py` | CEO 審查工具 |

### UI
| 檔案 | 功能 |
|------|------|
| `ui/dashboard.py` | 新增「策略管道」Tab |
| `ui/backtest_pages/single_test.py` | Q1 數據自動計算指標 |

### Tests
| 檔案 | 測試數 |
|------|--------|
| `tests/test_ceo_review.py` | 19 |
| `tests/test_phase1.py` | 27 |
| `tests/test_phase2.py` | 21 |
| `tests/test_phase3.py` | 10 |
| `tests/test_phase4.py` | 12 |

## 測試結果

```
287 passed, 1 skipped (by design), 0 failures
```

Skipped: `test_real_csv_integration` — 需要 monitor 產生當日 CSV，找不到檔案時正確跳過。

## 使用方法

```bash
# 完整審查
python3 scripts/tools/ceo_review.py
python3 scripts/tools/ceo_review.py --history

# 收盤後檢討
python3 scripts/daily_review.py --session day
python3 scripts/daily_review.py --session night --dry-run

# 每週報告
python3 scripts/weekly_report.py

# 測試
python3 -m pytest tests/ -v
```

---
**版本**: v2.0
**日期**: 2026-04-12
**GStack Engineering**
