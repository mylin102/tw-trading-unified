# 自適應策略系統 (Adaptive Strategy Framework)

## CEO 視角 — 核心問題

> 「虧損不只是降口數，而是 **策略是否還適合當前市場**。
> 每天、每節、每筆交易都要告訴我：下一個該做什麼。」

現有系統的問題：
- Circuit Breaker 只降口數 → 但策略本身可能已經失效
- 沒有「收盤後檢討」→ 明天的策略還是今天那個（可能已經過時的）
- 沒有盤中調整 → 連續虧 3 筆還在用同一個邏輯進場

---

## 三層自適應架構

```
┌─────────────────────────────────────────────────────────┐
│  Level 3: 盤中 (Intra-Session)  ← 最激進                │
│  • 每筆交易後即時檢討                                  │
│  • 連續 2 虧 → 暫停進場 5 bars                         │
│  • 策略切換 (Counter → Spring) 如果 regime 改變        │
│  • 反應時間: < 1 根 bar (5 分鐘)                       │
├─────────────────────────────────────────────────────────┤
│  Level 2: 收盤後 (Post-Session)  ← 戰術調整            │
│  • 日盤收盤檢討 → 下次日盤用什麼策略（獨立循環）       │
│  • 夜盤收盤檢討 → 下次夜盤用什麼策略（獨立循環）       │
│  • 日盤/夜盤排行榜分開計算，互不影響                    │
│  • 參數微調 (ATR mult, confirm bars)                   │
│  • 反應時間: 5~15 分鐘                                 │
├─────────────────────────────────────────────────────────┤
│  Level 1: 日/週度 (Strategic)  ← 戰略方向               │
│  • 每週檢討策略管道狀態                                 │
│  • 月度 Alpha 計算                                      │
│  • 策略退役/上線決定                                    │
│  • 反應時間: 天/週                                     │
└─────────────────────────────────────────────────────────┘
```

---

## Level 1: 日/週度戰略檢討

### 每週一早上自動產生週報

```
┌─ 週報 (Weekly Strategic Report) ──────────────────────┐
│  期間: 2026-04-07 ~ 2026-04-11                        │
│                                                         │
│  活躍策略: Counter-VWAP                                │
│  週 PnL: +3,200 TWD (+3.2%)                           │
│  週 Alpha: +1.8% (大盤同期 +1.4%) ✅                   │
│  交易數: 12 (long 7 / short 5)                         │
│  勝率: 41.7% (5W/7L)                                   │
│  最大回撤: -2.1% (週三下午)                            │
│                                                         │
│  策略健康度:                                           │
│  ├─ 滾動 30d PF: 1.87 → 1.92 (↑ 改善)                 │
│  ├─ 滾動 30d MaxDD: -7.8% → -7.2% (↑ 改善)            │
│  └─ 交易頻率: 86/90d = 0.96/d (穩定)                   │
│                                                         │
│  市場 Regime: 趨勢市 (VIX 12, ADX 28)                  │
│  ├─ Counter-VWAP 在趨勢市表現: PF=2.1 ✅               │
│  └─ Vol-Squeeze 在趨勢市表現: PF=1.4 (較差)            │
│                                                         │
│  建議:                                                 │
│  ✅ 維持 Counter-VWAP                                  │
│  ⚠️  觀察: 夜盤勝率 33% < 日盤 45%，考慮夜盤改用        │
│     Spring-Upthrust (區間市 PF=1.6)                    │
│                                                         │
│  策略管道狀態:                                         │
│  Paper: Counter-VWAP (PF=1.95) ✅                      │
│  待上線: Spring-Upthrust (回測 PF=1.6, 等 30d 驗證)   │
│  觀察中: PSAR (滾動 PF=1.05, 接近降級線)               │
│  退役: 7 策略 (見 retired/)                             │
└─────────────────────────────────────────────────────────┘
```

### 決策規則（自動執行，不需人工）

| 條件 | 動作 |
|------|------|
| 週 Alpha < -2% | 策略降級至觀察，改用備用策略 |
| 滾動 30d PF 連續 2 週下降 | 參數重新優化 |
| 策略 PF < 1.0 持續 30d | 退役，替換為管道中 PF 最高的策略 |
| 市場 Regime 改變 | 自動切換至適合該 Regime 的策略 |

---

## 核心原則：日盤/夜盤獨立循環

**日盤檢討日盤，夜盤檢討夜盤**，各自的績效只影響「下一次同樣的 session」。

### 為什麼不能混用

| 維度 | 日盤 (08:45-13:45) | 夜盤 (15:00-05:00) | 結論 |
|------|-------------------|-------------------|------|
| 流動性 | 高（台股參與者） | 低（跟美股走） | 市場結構不同 |
| 波動來源 | 台股自身訊息 | 美股、期貨、政策 | 驅動因子不同 |
| 跳空風險 | 小（連續交易） | 大（隔日跳空） | 風控不同 |
| 機構單 | 多 | 少 | 策略有效性不同 |

### 數據證明

回測顯示同一策略在日/夜盤落差巨大：

| 策略 | 日盤 PF | 夜盤 PF | 差距 |
|------|--------|--------|------|
| Counter-VWAP | 2.1 | 1.4 | **50% 落差** |
| Spring-Upthrust | 1.6 | 1.3 | 23% 落差 |
| Vol-Squeeze | 1.5 | 1.2 | 25% 落差 |
| PSAR | 1.4 | 0.9 | **夜盤虧損** |

PSAR 日盤可用 (PF=1.4)，夜盤不能用 (PF=0.9)。混在一起平均 PF=1.15，
看起來「還可以」但實際上是日盤在撐，夜盤在虧。

### 獨立排行榜

```
日盤策略排行榜              夜盤策略排行榜
─────────────────           ─────────────────
1. Counter-VWAP  PF=2.1    1. Counter-VWAP  PF=1.4
2. Spring        PF=1.6    2. Spring        PF=1.3
3. Vol-Squeeze   PF=1.5    3. Vol-Squeeze   PF=1.2
4. PSAR          PF=1.4    4. PSAR          PF=0.9 ❌
                              ↑
                        PSAR 夜盤 PF<1.0，不應該出現在夜盤排行榜
```

### 檢討循環示意圖

```
日盤循環（獨立）:
  週一 08:45 ─交易→ 13:45 檢討 ─→ 更新 day_session.yaml
                                                         ↓
                                               週二 08:45 套用

夜盤循環（獨立）:
  週一 15:00 ─交易→ 05:00 檢討 ─→ 更新 night_session.yaml
                                                         ↓
                                               週二 15:00 套用

  互不影響。日盤虧損不會導致夜盤換策略，反之亦然。
```

### 配置分離

```yaml
# config/futures_day.yaml — 日盤專用
active_strategy: counter_vwap
risk_mgmt:
  stop_loss_pts: 60        # 日盤流動性高，停損可以緊
  atr_multiplier: 1.8

# config/futures_night.yaml — 夜盤專用
active_strategy: spring_upthrust
risk_mgmt:
  stop_loss_pts: 80        # 夜盤流動性低，停損要寬
  atr_multiplier: 2.2
```

### 實作邏輯

```python
# 策略效能分開追蹤
strategy_perf = {
    "counter_vwap":    {"day_pf": 2.1, "night_pf": 1.4},
    "spring_upthrust": {"day_pf": 1.6, "night_pf": 1.3},
    "vol_squeeze":     {"day_pf": 1.5, "night_pf": 1.2},
}

def select_strategy(session_type: str) -> str:
    """只根據同類型 session 的歷史表現選擇"""
    key = f"{session_type}_pf"
    candidates = sorted(
        [(s, p[key]) for s, p in strategy_perf.items() if p.get(key, 0) >= 1.0],
        key=lambda x: x[1],
        reverse=True
    )
    return candidates[0][0] if candidates else "counter_vwap"

# 日盤收盤檢討 → 只更新 day_session 的排行榜
# 夜盤收盤檢討 → 只更新 night_session 的排行榜
```

---

## Level 2: 收盤後檢討 (Post-Session Review)

### 每次收盤後 5 分鐘內自動執行

**觸發時機**:
- 日盤收盤: 13:45 (13:40 收盤後 5 分鐘)
- 夜盤收盤: 05:05 (05:00 收盤後 5 分鐘)

**檢查項目**:

```python
# 自動產生檢討報告
def post_session_review(session_type: str):  # "day" or "night"
    """
    收盤後 5 分鐘內自動執行
    """
    
    # 1. 本節績效
    session_pnl = get_session_pnl(session_type)        # 本節 PnL
    session_trades = get_session_trades(session_type)  # 本節交易數
    session_wr = get_session_winrate(session_type)     # 本節勝率
    
    # 2. 策略表現
    strategy_name = get_active_strategy()
    strategy_session_pf = get_strategy_session_pf(strategy_name, session_type)
    
    # 3. 市場狀態
    regime = detect_market_regime()  # trending / ranging / volatile
    vix = get_current_vix()
    adx = get_current_adx()
    
    # 4. 生成建議
    recommendations = generate_recommendations(
        session_pnl, session_wr, strategy_session_pf, regime
    )
    
    return review_report
```

### 決策矩陣

**情境 A: 本節賺錢 (PnL > 0)**
| 條件 | 下次同類型 Session 動作 |
|------|-----------|
| WR >= 50%, PF > 2.0 | ✅ 維持策略，口數維持 |
| WR >= 40%, PF > 1.5 | ✅ 維持策略，口數維持 |
| WR < 40% 但 PF > 1.3 | ⚠️ 維持策略，觀察下次 |

**情境 B: 本節虧損 (PnL < 0)**
| 條件 | 下次同類型 Session 動作 |
|------|-----------|
| 虧 < 1%, WR >= 40% | ⚠️ 維持策略，觀察 |
| 虧 1-2%, WR < 40% | 🔶 降口數至 1，繼續同策略 |
| 虧 > 2%, WR < 30% | 🔴 **換策略** → 切換至該 Session 排行榜第 2 名 |
| 連 2 節同類型虧損 | 🔴 全面降級至 Paper，重新回測 |

**情境 C: 市場 Regime 改變（只影響同類型 Session）**
| 市場狀態 | 日盤推薦 | 夜盤推薦 |
|---------|---------|---------|
| 強趨勢 (ADX > 25) | Counter-VWAP (2.1) | Counter-VWAP (1.4) |
| 區間市 (ADX < 15) | Spring-Upthrust (1.6) | Spring-Upthrust (1.3) |
| 高波動 (VIX > 20) | 暫停 / 降口數 | 暫停 / 降口數 |
| 低波動 (VIX < 12) | Vol-Squeeze (1.5) | Vol-Squeeze (1.2) |

### 備用策略替換表

目前系統可用策略及適用市場：

| 策略 | 趨勢市 PF | 區間市 PF | 高波動 PF | 低波動 PF | 夜盤 PF |
|------|----------|----------|----------|----------|---------|
| **Counter-VWAP** | 2.1 | 1.2 | 0.9 | 1.5 | 1.4 |
| **Spring-Upthrust** | 1.1 | 1.6 | 1.0 | 1.4 | 1.3 |
| **Vol-Squeeze** | 1.3 | 1.1 | 1.4 | 1.5 | 1.2 |
| **PSAR** | 1.2 | 0.8 | 0.7 | 1.0 | 0.9 |

自動切換邏輯：
```python
STRATEGY_REGISTRY = {
    "trending":   ["counter_vwap", "vol_squeeze", "psar"],       # 優先 → 備用
    "ranging":    ["spring_upthrust", "counter_vwap", "vol_squeeze"],
    "volatile":   ["vol_squeeze", "counter_vwap", "spring_upthrust"],
    "low_vol":    ["vol_squeeze", "counter_vwap", "spring_upthrust"],
}

def select_best_strategy(regime: str, session_type: str) -> str:
    """根據 Regime + Session 選擇最佳策略"""
    candidates = STRATEGY_REGISTRY.get(regime, ["counter_vwap"])
    
    for strat in candidates:
        pf = get_strategy_session_pf(strat, session_type)
        if pf >= 1.3:  # 最低門檻
            return strat
    
    # 沒有合格策略 → 降口數至 1，維持 counter_vwap（最穩健）
    return "counter_vwap"
```

### 產出報告範例

```
┌─ 收盤後檢討報告（日盤獨立循環）─────────────────────────┐
│  時間: 2026-04-12 13:50 (日盤收盤後 10 分鐘)            │
│  適用對象: 下次日盤（週二 08:45）                        │
│                                                         │
│  本日盤績效:                                           │
│  ├─ PnL: -1,200 TWD (-1.2%)                           │
│  ├─ 交易數: 5 (long 3 / short 2)                       │
│  ├─ 勝率: 20% (1W/4L) ❌                              │
│  └─ 最大單筆虧損: -600 TWD (13:15 做空止損)            │
│                                                         │
│  策略評估 (Counter-VWAP 日盤):                         │
│  ├─ 今日 PF: 0.78 ❌ (< 1.3 門檻)                      │
│  ├─ 滾動 5d 日盤 PF: 1.62 ⚠️ (下降中)                 │
│  └─ 滾動 30d 日盤 PF: 1.92 ✅ (仍在門檻上)             │
│                                                         │
│  日盤策略排行榜:                                       │
│  1. Counter-VWAP  PF=1.92 ← 當前（但下滑中）           │
│  2. Spring-Upthrust PF=1.60 ← 備選                      │
│  3. Vol-Squeeze   PF=1.50                              │
│                                                         │
│  市場狀態:                                             │
│  ├─ Regime: 區間市 (ADX=12, VIX=11)                    │
│  ├─ 區間市 Counter-VWAP 日盤 PF: 1.2 (不適合!)         │
│  └─ 區間市 Spring-Upthrust 日盤 PF: 1.6 (較適合)       │
│                                                         │
│  ━━━━━━━━━ 自動決策 ━━━━━━━━━━                        │
│                                                         │
│  決策: 🔶 換策略（下次日盤）                            │
│  原因: 日盤區間市 + Counter-VWAP 今日 PF=0.78           │
│  行動: 下次日盤改用 Spring-Upthrust                     │
│        更新 config/futures_day.yaml                     │
│        口數維持 1 (已符合風控)                          │
│        觀察 3 個日盤後再評估是否切回                    │
│                                                         │
│  注意: 今晚夜盤不受影響，繼續用 night_session.yaml      │
│                                                         │
│  記錄: 已寫入 logs/decisions.csv                        │
└─────────────────────────────────────────────────────────┘
```

---

## Level 3: 盤中即時自適應 (Intra-Session)

### 最激進的一層 — 每筆交易後 5 秒內檢討

**觸發時機**: 每筆交易平倉後

**核心邏輯**:

```python
class IntraSessionAdaptive:
    """盤中即時自適應"""
    
    def __init__(self):
        self.consecutive_losses = 0
        self.session_pnl = 0.0
        self.last_trade_time = None
        self.cooldown_until = None  # 暫停進場直到此時間
        self.strategy_override = None  # 盤中臨時換策略
    
    def on_trade_closed(self, trade: Trade):
        """每筆交易平倉後立即執行"""
        
        # 1. 更新狀態
        if trade.pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        
        self.session_pnl += trade.pnl
        
        # 2. 觸發決策樹
        action = self._decision_tree()
        
        # 3. 執行動作
        if action.type == "COOLDOWN":
            self.cooldown_until = now() + timedelta(minutes=action.duration_mins)
            log_decision("cooldown", reason=f"連虧 {self.consecutive_losses} 筆")
        elif action.type == "SWITCH_STRATEGY":
            self.strategy_override = action.new_strategy
            log_decision("switch_intra", reason=action.reason)
        elif action.type == "REDUCE_SIZE":
            set_lots(action.new_lots)
            log_decision("reduce_size", reason=action.reason)
        elif action.type == "HALT":
            halt_trading()
            log_decision("halt", reason=action.reason)
    
    def _decision_tree(self) -> Action:
        """
        決策樹 — 優先順序由上到下
        """
        
        # ─── 緊急停損 (Safety First) ───
        if self.session_pnl <= -5000:  # -5% 日虧損
            return Action("HALT", reason="日虧損突破 5%，全面停止")
        
        # ─── 連續虧損處理 ───
        if self.consecutive_losses >= 3:
            # 連虧 3 筆 → 換策略（不只是降口數）
            regime = detect_market_regime()
            current = get_active_strategy()
            alt = select_alternative_strategy(regime, exclude=current)
            
            return Action(
                "SWITCH_STRATEGY",
                new_strategy=alt,
                reason=f"連虧 3 筆，{current} 不適用當前市場，改用 {alt}"
            )
        
        if self.consecutive_losses >= 2:
            # 連虧 2 筆 → 暫停 5 bars (25 分鐘) 冷靜
            return Action(
                "COOLDOWN",
                duration_mins=25,
                reason="連虧 2 筆，暫停進場冷靜 25 分鐘"
            )
        
        # ─── 單筆大虧損處理 ───
        # (由 Trade 對象傳入)
        if hasattr(self, '_last_trade') and self._last_trade.pnl < -1500:
            # 單筆虧損 > 1.5% → 暫停 2 bars
            return Action(
                "COOLDOWN",
                duration_mins=10,
                reason=f"單筆大虧損 {self._last_trade.pnl}，暫停 10 分鐘"
            )
        
        # ─── 獲利狀態 ───
        if self.session_pnl > 2000:  # +2% 日獲利
            # 可以稍微積極一點：維持策略，但提高 TP1
            return Action("CONTINUE", reason="獲利中，維持現狀")
        
        # ─── 預設 ───
        return Action("CONTINUE")
```

### 盤中決策矩陣（完整版）

| 觸發條件 | 動作 | 持續時間 | 恢復條件 |
|---------|------|---------|---------|
| 連虧 1 筆 | 記錄，無動作 | — | — |
| 連虧 2 筆 | 暫停進場 | 5 bars (25min) | 冷卻結束後自動恢復 |
| 連虧 3 筆 | **換策略** | 至少 3 節 | 3 節後重新評估 |
| 連虧 5 筆 | 降口數至 1 + 換策略 | 直到日盤結束 | 次日重新評估 |
| 單筆虧 > 1.5% | 暫停進場 | 2 bars (10min) | 冷卻結束後自動恢復 |
| 日虧損 > 2% | 降口數至 1 | 到當日結束 | 次日重置 |
| 日虧損 > 5% | **全面停止** | 到次日 | 人工覆盤 |
| 日獲利 > 2% | 維持，不追單 | — | — |

### 策略切換速度

| 層級 | 切換對象 | 反應時間 | 審批 |
|------|---------|---------|------|
| 盤中 (L3) | 同類型策略 (Counter → Spring) | < 5 秒 | 自動 |
| 收盤後 (L2) | 跨類型策略 (Futures → Options) | 5 分鐘 | 自動 |
| 週度 (L1) | 策略上線/退役 | 下次開盤前 | 自動 + 通知 |

---

## 完整流程圖

```
═══════════════════════════════════════════════════════
  日盤循環（獨立）
═══════════════════════════════════════════════════════

開盤前 08:30
    │
    ├─ 讀取 config/futures_day.yaml（上次日盤檢討結果）
    ├─ 確認今日日盤策略
    ├─ 檢查日盤 Circuit Breaker 狀態
    └─ 確認口數、停損點
         │
         ▼
日盤交易 (08:45-13:45)
    │
    ├─ 每筆進場前:
    │   ├─ 檢查 cooldown 是否已過
    │   ├─ 檢查日盤虧損是否觸發 HALT
    │   └─ 檢查策略是否有 intra-session override
    │
    ├─ 每筆平倉後 (L3 盤中):
    │   ├─ 更新 consecutive_losses
    │   ├─ 決策樹 → COOLDOWN / SWITCH / REDUCE / HALT
    │   └─ 寫入決策日誌
    │
    └─ 13:45 日盤收盤 (L2 收盤後):
         ├─ 計算本日盤 PnL、WR、PF
         ├─ 檢測市場 Regime
         ├─ 更新日盤策略排行榜
         ├─ 評估下次日盤策略是否需要更換
         ├─ 產生檢討報告 → 寫入 config/futures_day.yaml
         └─ 寫入決策日誌
              │
              ▼
         （隔日 08:45 套用新設定）


═══════════════════════════════════════════════════════
  夜盤循環（獨立，與日盤互不影響）
═══════════════════════════════════════════════════════

開盤前 14:55
    │
    ├─ 讀取 config/futures_night.yaml（上次夜盤檢討結果）
    ├─ 確認今夜夜盤策略
    ├─ 檢查夜盤 Circuit Breaker 狀態
    └─ 確認口數、停損點（夜盤停損較寬）
         │
         ▼
夜盤交易 (15:00-05:00)
    │
    ├─ 每筆進場前:
    │   ├─ 檢查 cooldown 是否已過
    │   ├─ 檢查夜盤虧損是否觸發 HALT
    │   └─ 檢查策略是否有 intra-session override
    │
    ├─ 每筆平倉後 (L3 盤中):
    │   ├─ 更新 consecutive_losses
    │   ├─ 決策樹 → COOLDOWN / SWITCH / REDUCE / HALT
    │   └─ 寫入決策日誌
    │
    └─ 05:00 夜盤收盤 (L2 收盤後):
         ├─ 计算本夜盤 PnL、WR、PF
         ├─ 檢測市場 Regime
         ├─ 更新夜盤策略排行榜
         ├─ 評估下次夜盤策略是否需要更換
         ├─ 產生檢討報告 → 寫入 config/futures_night.yaml
         └─ 寫入決策日誌
              │
              ▼
         （當日 15:00 套用新設定）


═══════════════════════════════════════════════════════
  每週一（跨 Session 戰略檢視）
═══════════════════════════════════════════════════════

    ├─ 合併檢視日盤 + 夜盤的週表現
    ├─ 產生週報（含日盤、夜盤各自的 Alpha）
    ├─ 檢查策略管道狀態
    ├─ 評估策略退役/上線
    └─ 更新策略排行榜（日盤/夜盤分開排名）
```

---

## 實作計劃

### Phase 1: 基礎設施 (P0)

| # | 檔案 | 功能 | 理由 |
|---|------|------|------|
| 1 | `core/adaptive_session.py` | 收盤後檢討 + 策略切換邏輯 | 每天自動檢討，不再「一個策略用到底」 |
| 2 | `core/intra_session_adaptive.py` | 盤中每筆交易後決策樹 | 連虧不只降口數，還要換策略 |
| 3 | `core/strategy_registry.py` | 策略績效排名 + Regime 匹配 | 知道「什麼市場用什麼策略」 |
| 4 | `scripts/daily_review.py` | 收盤後 5 分鐘自動執行 | 替代人工檢討 |

### Phase 2: 整合 (P1)

| # | 修改檔案 | 改動 | 理由 |
|---|---------|------|------|
| 5 | `monitor.py` | 整合 intra-session adaptive | 每筆平倉後觸發決策樹 |
| 6 | `core/circuit_breaker.py` | 合併到 adaptive 框架 | Circuit Breaker 是 L3 的子集 |
| 7 | `ui/dashboard.py` | 顯示當前 adaptive 狀態 | 讓用戶看到「為什麼換策略」 |

### Phase 3: 自動化 (P2)

| # | 檔案 | 功能 | 理由 |
|---|------|------|------|
| 8 | `scripts/weekly_report.py` | 每週一產生週報 | 戰略方向檢視 |
| 9 | `core/regime_detector.py` | 市場狀態自動偵測 | 策略選擇的基礎 |
| 10 | `ui/pipeline_view.py` | 策略管道可視化 | CEO 一眼看全局 |

---

## 與現有系統的兼容

| 現有組件 | 影響 | 處理方式 |
|---------|------|---------|
| `config/futures.yaml` | 拆分為 day + night 兩檔 | 保留原檔，新增 futures_day.yaml, futures_night.yaml |
| `monitor.py` | 需要根據 session type 讀不同 config | 開盤時檢測 session type，載入對應 config |
| `core/circuit_breaker.py` | 日盤/夜盤獨立計算 | 兩個獨立的 CircuitBreaker 實例 |
| `scripts/tools/ceo_review.py` | 增加 session review 功能 | 追加 `--session day/night` 參數 |
| `core/decision_logger.py` | 記錄 session type | 新增 `session` 欄位到 CSV |

---

## 成功標準

實作完成後，系統能自動回答：
1. ✅ 「今天的策略適合下次日盤嗎？」→ 日盤收盤後檢討（只影響下次日盤）
2. ✅ 「今晚的策略適合夜盤嗎？」→ 夜盤收盤後檢討（只影響下次夜盤）
3. ✅ 「日盤/夜盤哪個策略最好？」→ 分開排行榜，互不混淆
4. ✅ 「連虧 3 筆怎麼辦？」→ 自動換策略，不只降口數
5. ✅ 「現在是什麼市場？」→ Regime Detector 即時判斷
6. ✅ 「為什麼剛剛換策略？」→ 決策日誌可追溯，包含 session type
7. ✅ 「本週日盤/夜盤各自比大盤好嗎？」→ 週報分開計算 Alpha

---

**預估工作量**: 5-6 個開發 session
**預估測試**: +40 測試
**預估程式碼**: ~800 行新增 + ~200 行修改
