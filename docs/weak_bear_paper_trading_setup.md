# weak_bear_trend Paper Trading 上線指南
## 2026-05-07 夜盤測試

## 🚀 快速啟動

### 1. 啟動 Monitor (Paper Trading)

```bash
cd ~/Documents/mylin102/tw-trading-unified

# 使用 weak_bear_trend 配置啟動夜盤 monitor
python3 main.py --config config/futures_night_weak_bear.yaml
```

### 2. 監控表現

**終端 1**: Monitor 運行 (上述命令)

**終端 2**: 實時監控腳本

```bash
# 實時追蹤 (類似 tail -f)
python3 scripts/monitor_weak_bear_paper.py --live

# 或定期查看摘要
python3 scripts/monitor_weak_bear_paper.py --summary
```

## 📋 配置說明

### 配置文件：`config/futures_night_weak_bear.yaml`

**關鍵設置**:
```yaml
active_strategy: weak_bear_trend
live_trading: false  # Paper Trading Mode

strategy:
  params:
    shadow_mode: false  # 用真實信號 (但不下單)
    stop_atr_mult: 1.5
    take_profit_atr_mult: 2.0
    time_stop_minutes: 20

trade_mgmt:
  allow_long: false  # 只做空
  allow_short: true
  max_positions: 1  # 單一持倉
```

### 策略進場條件回顧

```
✅ regime in {WEAK, CHOP}
✅ bias == SHORT
✅ ADX < 22
✅ 過去 5 bars 曾有反彈接近 VWAP
✅ 價格在 VWAP 之下或附近 (< 0.8 ATR)
✅ mom_velo < -5 (動能向下加速)
→ SELL
```

## 📊 監控指標

### 實時檢查清單

- [ ] **Regime 正確**: 確認在 WEAK/CHOP 時才進場
- [ ] **Bias 正確**: 確認 bias=SHORT
- [ ] **進場信號**: 記錄每次 SELL 信號的時間和價格
- [ ] **止損/止盈**: 確認 1.5 ATR 止損、2.0 ATR 止盈執行
- [ ] **時間止損**: 20 分鐘無獲利是否出場
- [ ] **PnL 追蹤**: 記錄每筆交易的盈虧

### 預期進場頻率

- **WEAK + SHORT**: 1-3 次/夜盤
- **如果不進場**: 可能原因
  - Regime 不是 WEAK/CHOP
  - Bias 不是 SHORT
  - 沒有反彈確認
  - 動能不夠向下

## 🔍 日誌位置

```bash
# 主日誌
~/Documents/mylin102/tw-trading-unified/logs/shioaji.log

# Paper Trading 專用日誌 (如果配置)
~/Documents/mylin102/tw-trading-unified/logs/paper_trading_weak_bear.jsonl

# 策略日誌
grep "WEAK_BEAR" ~/Documents/mylin102/tw-trading-unified/logs/shioaji.log
```

## 📈 績效評估標準

### 今晚成功標準

| 指標 | 成功 | 警告 | 失敗 |
|------|------|------|------|
| **進場次數** | 1-5 次 | 0 次或 >5 次 | - |
| **勝率** | > 50% | 40-50% | < 40% |
| **平均 PnL** | > 0 | 接近 0 | < 0 |
| **最大回撤** | < 3% | 3-5% | > 5% |
| **策略遵循** | 100% | 有小偏差 | 重大偏差 |

### 檢討要點

**如果獲利**:
- ✅ 進場邏輯是否正確？
- ✅ 止損/止盈是否恰當？
- ✅ 是否可以放大倉位？

**如果虧損**:
- ❌ 虧損原因：止損太緊？進場太早？
- ❌ Regime 判斷是否準確？
- ❌ 是否需要調整參數？

**如果不進場**:
- ⚠️ Regime 分類是否正確？
- ⚠️ Bias 計算是否準確？
- ⚠️ 進場條件是否太嚴格？

## 🛑 緊急處理

### 如果發現異常

1. **頻繁進場 (> 5 次/夜盤)**
   ```bash
   # 檢查是否為 bug
   grep "WEAK_BEAR_SIGNAL" logs/shioaji.log | wc -l
   
   # 如果太多，暫停策略
   # 修改 config: active_strategy: counter_vwap
   ```

2. **止損過大 (> 100 點)**
   ```bash
   # 檢查 ATR 計算
   grep "atr" logs/shioaji.log | tail -20
   
   # 可能需要調整 stop_atr_mult
   ```

3. **策略不進場但其他策略進場**
   ```bash
   # 檢查 regime 和 bias
   grep "regime\|bias" logs/shioaji.log | tail -30
   
   # 確認是否為 WEAK + SHORT
   ```

## 📝 夜盤後檢討模板

```markdown
# weak_bear_trend 夜盤檢討 - 2026-05-07

## 基本數據
- 開盤時間：18:00
- 收盤時間：05:00
- 進場次數：X 次
- 獲利次數：X 次
- 虧損次數：X 次
- 總 PnL: XXXX 點

## 成功之處
1. ...
2. ...

## 需要改進
1. ...
2. ...

## 參數調整建議
- stop_atr_mult: 1.5 → ?
- time_stop_minutes: 20 → ?
- min_mom_velo_bearish: -5 → ?

## 明日行動
- [ ] ...
- [ ] ...
```

## ⏰ 時間表

| 時間 | 動作 |
|------|------|
| **17:30** | 檢查系統狀態，準備啟動 |
| **17:50** | 啟動 monitor (paper trading) |
| **18:00** | 夜盤開盤，開始監控 |
| **18:00-05:00** | 實時監控進場信號 |
| **05:00** | 夜盤收盤，強制平倉 |
| **05:30** | 生成績效報告 |
| **次日** | 檢討會議，決定是否調整 |

## 🎯 最終目標

**今晚不是為了獲利，而是為了驗證**:

1. ✅ 策略邏輯是否正確執行
2. ✅ 進場條件是否合理
3. ✅ 止損/止盈機制是否正常
4. ✅ 與現有系統的整合是否順暢

**真實績效需要至少 1-2 週的數據累積**

---

## 📞 需要幫助？

如果遇到問題：

1. 檢查日誌：`logs/shioaji.log`
2. 查看監控腳本輸出
3. 檢查配置文件是否正確
4. 確認數據 feed 是否正常

**祝今晚測試順利！** 🚀
