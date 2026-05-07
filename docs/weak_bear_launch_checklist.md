# weak_bear_trend 夜盤上線檢查清單
## 2026-05-07

## ✅ 準備工作 (17:30 前完成)

### 文件就緒

- [x] 策略文件：`strategies/plugins/futures/weak_bear_trend.py`
- [x] 配置文件：`config/futures_night_weak_bear.yaml`
- [x] 配置文件已複製到：`config/strategies/weak_bear_trend.yaml`
- [x] 監控腳本：`scripts/monitor_weak_bear_paper.py`
- [x] 文檔：`docs/weak_bear_paper_trading_setup.md`

### 系統檢查

```bash
# 1. 檢查 Python 環境
cd ~/Documents/mylin102/tw-trading-unified
python3 --version  # 應該顯示 Python 3.11+

# 2. 檢查配置文件
cat config/futures_night_weak_bear.yaml | grep "active_strategy"
# 應該顯示：active_strategy: weak_bear_trend

# 3. 檢查 live_trading 設定 (必須為 false)
cat config/futures_night_weak_bear.yaml | grep "live_trading"
# 應該顯示：live_trading: false

# 4. 檢查日誌目錄
ls -la logs/
# 確認有寫入權限
```

### 策略參數確認

```yaml
# config/futures_night_weak_bear.yaml 關鍵參數

strategy:
  active_strategy: weak_bear_trend  # ✓
  
  params:
    shadow_mode: false  # ✓ Paper trading 用真實信號
    stop_atr_mult: 1.5  # ✓ 止損 1.5 ATR
    take_profit_atr_mult: 2.0  # ✓ 止盈 2.0 ATR
    time_stop_minutes: 20  # ✓ 20 分鐘時間止損
    max_adx: 22.0  # ✓ WEAK regime 上限
    min_mom_velo_bearish: -5.0  # ✓ 動能向下加速

trade_mgmt:
  allow_long: false  # ✓ 只做空
  allow_short: true  # ✓
  max_positions: 1  # ✓ 單一持倉
```

## 🚀 啟動流程 (17:50)

### 終端 1: 啟動 Monitor

```bash
cd ~/Documents/mylin102/tw-trading-unified

# 使用 weak_bear_trend 配置
python3 main.py --config config/futures_night_weak_bear.yaml
```

**預期輸出**:
```
[INFO] StrategyRegistry: discovered X plugin(s), 0 error(s)
[INFO] weak_bear_trend 策略已載入
[INFO] Paper Trading Mode: ENABLED
[INFO] 等待夜盤開盤...
```

**如果看到錯誤**:
```
❌ Strategy 'weak_bear_trend' NOT in registry
→ 檢查策略文件是否存在
→ 檢查是否有語法錯誤：python3 -m py_compile strategies/plugins/futures/weak_bear_trend.py

❌ Config file not found
→ 確認配置文件路徑正確
```

### 終端 2: 啟動監控

```bash
# 實時追蹤
python3 scripts/monitor_weak_bear_paper.py --live
```

**預期輸出**:
```
============================================================
weak_bear_trend 實時日誌追蹤
============================================================
監控：/Users/mylin/logs/paper_trading_weak_bear.jsonl

📊 [18:00:00] Regime: WEAK, Bias: SHORT
```

## 📊 夜盤監控 (18:00 - 05:00)

### 進場條件檢查清單

每次進場前自動檢查：

```
[ ] Regime = WEAK 或 CHOP?
[ ] Bias = SHORT?
[ ] ADX < 22?
[ ] 過去 5 bars 有反彈接近 VWAP?
[ ] 價格在 VWAP 之下或附近 (< 0.8 ATR)?
[ ] mom_velo < -5 (向下加速)?
[ ] volume_spike >= 1.0?
→ 全部滿足 → SELL 信號
```

### 預期情境

**情境 1: 正常進場**
```
時間：22:30
Regime: WEAK
Bias: SHORT
價格：22000, VWAP: 22050
事件：22:00-22:25 反彈至 22040，然後下跌
→ SELL @ 22000, SL: 22075, TP: 21900
```

**情境 2: 不進場 (條件不滿足)**
```
可能原因:
- Regime = TREND (不是 WEAK)
- Bias = LONG (不是 SHORT)
- ADX > 22 (太強)
- 沒有反彈確認
- mom_velo > -5 (動能不夠)
```

### 監控頻率

| 時間 | 動作 |
|------|------|
| **每 30 分鐘** | 檢查 monitor 是否正常運行 |
| **每次進場** | 記錄信號和價格 |
| **每次出場** | 記錄 PnL 和原因 |
| **05:00** | 確認強制平倉 |

## 🛑 異常處理

### 問題 1: 頻繁進場 (> 5 次/夜盤)

```bash
# 檢查日誌
grep "WEAK_BEAR_SIGNAL" logs/shioaji.log | wc -l

# 可能原因
- Regime 分類錯誤
- 進場條件太寬鬆

# 解決方案
# 暫停策略，修改 config:
# active_strategy: counter_vwap
```

### 問題 2: 完全不進場

```bash
# 檢查 regime 和 bias
grep "regime\|bias" logs/shioaji.log | tail -30

# 可能原因
- 今晚不是 WEAK regime
- Bias 不是 SHORT
- 進場條件太嚴格

# 解決方案
# 接受現實，不是每晚都適合交易
```

### 問題 3: 止損過大 (> 100 點)

```bash
# 檢查 ATR
grep "atr" logs/shioaji.log | tail -20

# 可能原因
- ATR 計算錯誤
- 市場波動過大

# 解決方案
# 調整 stop_atr_mult: 1.5 → 1.2
```

## 📈 收盤後檢討 (05:30)

### 生成報告

```bash
# 查看摘要
python3 scripts/monitor_weak_bear_paper.py --summary

# 或查看完整日誌
cat logs/paper_trading_weak_bear.jsonl | python3 -m json.tool
```

### 填寫檢討模板

```markdown
## 2026-05-07 夜盤檢討

### 基本數據
- 進場次數：__ 次
- 獲利次數：__ 次
- 虧損次數：__ 次
- 勝率：__%
- 總 PnL: ____ 點
- 最大回撤：__%

### 關鍵觀察
1. 進場時機：(符合預期？)
2. 止損執行：(嚴格？)
3. 止盈執行：(嚴格？)
4. Regime 判斷：(準確？)

### 問題與改進
1. ...
2. ...

### 參數調整建議
- stop_atr_mult: 1.5 → __
- time_stop_minutes: 20 → __
- min_mom_velo_bearish: -5 → __

### 明日行動
- [ ] ...
- [ ] ...
```

## ✅ 成功標準

### 今晚成功 (不是獲利，是驗證)

- [x] Monitor 正常運行 8 小時
- [x] 策略正確載入
- [x] 進場邏輯符合設計
- [x] 止損/止盈正確執行
- [x] 日誌記錄完整

### 績效標準 (參考)

| 指標 | 優秀 | 良好 | 需改進 |
|------|------|------|--------|
| 進場次數 | 1-5 | 0 或 6-8 | > 8 |
| 勝率 | > 60% | 40-60% | < 40% |
| 總 PnL | > 0 | -500 ~ 0 | < -500 |
| 策略遵循 | 100% | > 90% | < 90% |

## 🎯 最終提醒

**今晚目標**: **驗證策略邏輯，不是獲利**

- ✅ 如果獲利：很好，但不代表策略完美
- ✅ 如果虧損：正常，收集數據改進
- ✅ 如果不進場：也正常，不是每晚都適合

**關鍵**: 如實記錄，客觀分析，持續改進

---

**祝今晚測試順利！** 🚀

**聯絡方式**: 如有問題，查看日誌 `logs/shioaji.log`
