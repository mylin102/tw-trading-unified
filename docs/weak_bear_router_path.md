# weak_bear_trend Router 路徑完整分析

## 🗺️ 完整路由流程

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. Monitor 啟動 (main.py)                                        │
│    配置文件：config/futures_night_weak_bear.yaml                │
│    active_strategy: weak_bear_trend                             │
└────────────────────┬────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│ 2. Monitor._strategy_tick()                                      │
│    - 讀取配置：self.STRATEGY.get("active_strategy")             │
│    - 獲取策略：active_name = "weak_bear_trend"                  │
│    - 確保初始化：_ensure_strategy_initialized()                 │
└────────────────────┬────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│ 3. Monitor._route_signal()                                       │
│    - 構建 context: _build_strategy_context(bar, session_regime) │
│    - 分類 regime: classify_futures_bar_regime(bar)              │
│    - 得到 bar_regime (例如：WEAK, bias=SHORT)                   │
└────────────────────┬────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│ 4. route_futures_signal() [core/futures_strategy_router.py]     │
│    輸入：                                                        │
│    - registry: StrategyRegistry (包含 weak_bear_trend)          │
│    - context: StrategyContext (包含 bar 數據)                    │
│    - regime_result: FuturesBarRegimeResult                      │
│    - active_strategy_name: "weak_bear_trend"                    │
└────────────────────┬────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│ 5. _strategy_order_for_regime()                                  │
│    根據 regime 選擇候选策略列表：                                 │
│                                                                  │
│    if regime == "WEAK":                                         │
│        base = list(config.weak_strategies)                      │
│        # weak_strategies 包含：                                 │
│        # ["adaptive_orb", "adaptive_orb_v15",                   │
│        #  "trend_continuation_v1", "counter_vwap",              │
│        #  "spring_upthrust", "kbar_feature",                    │
│        #  "calendar_condor_v2", "range_mean_reversion_v1",      │
│        #  "weak_bear_trend"] ← 這裡！                           │
│        if active_strategy_name:                                 │
│            base.insert(0, active_strategy_name)                 │
│        # 結果：["weak_bear_trend", "adaptive_orb", ...]         │
│        return _dedupe(base)                                     │
└────────────────────┬────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│ 6. _apply_strategy_policy()                                      │
│    檢查每個候选策略的 STRATEGY_POLICY：                          │
│                                                                  │
│    for name in candidates:                                      │
│        allowed, reason = _check_strategy_policy(                │
│            name, regime, metrics)                               │
│                                                                  │
│    # weak_bear_trend 檢查：                                     │
│    policy = {                                                    │
│        "enabled_regimes": ["WEAK", "CHOP"],                     │
│        "max_weight": 0.5,                                       │
│        "kill_if_cagr_below": -0.05,                             │
│        "required_bias": "SHORT",  ← 關鍵！                      │
│    }                                                            │
│                                                                  │
│    # 檢查項目：                                                  │
│    1. regime in enabled_regimes? ✓ (WEAK in ["WEAK", "CHOP"])   │
│    2. CAGR > kill_if_cagr_below? ✓ (假設 > -5%)                 │
│    3. required_bias 匹配？✓ (bias=SHORT)                        │
│                                                                  │
│    結果：weak_bear_trend 通過檢查，保留在 candidates 中          │
└────────────────────┬────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│ 7. 策略評估循環                                                  │
│    for name in candidates:                                      │
│        strategy = registry.get(name)                            │
│        prepare_strategy(name, strategy)  # 初始化               │
│        signal = strategy.on_bar(context)  # 調用策略            │
│                                                                  │
│    # weak_bear_trend.on_bar() 被調用！                          │
│    # 策略內部檢查：                                              │
│    # 1. regime in {WEAK, CHOP}? ✓                               │
│    # 2. bias == SHORT? ✓                                        │
│    # 3. ADX < 22? ✓                                             │
│    # 4. 反彈確認？✓                                              │
│    # 5. mom_velo < -5? ✓                                        │
│    # → 返回 SELL 信號                                            │
└────────────────────┬────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│ 8. 信號選擇                                                      │
│    從所有候選策略中選擇最佳信號：                                │
│                                                                  │
│    if signal and signal.action in {"BUY", "SELL"}:              │
│        # 檢查優先級和信心分數                                    │
│        if signal.confidence > best_confidence:                  │
│            best_signal = signal                                 │
│            best_strategy = name                                 │
│                                                                  │
│    # 如果 weak_bear_trend 的信心最高 (0.75)                      │
│    # 則被選為最終信號                                            │
└────────────────────┬────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│ 9. 返回決策                                                      │
│    return FuturesRouterDecision(                                │
│        action="TRADE",                                          │
│        reason="weak_bear_trend",                                │
│        regime="WEAK",                                           │
│        bias="SHORT",                                            │
│        selected_strategy="weak_bear_trend",                     │
│        signal=Signal("SELL", ...)                               │
│    )                                                            │
└────────────────────┬────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│ 10. Monitor._execute_trade()                                     │
│     - 接收決策：decision.signal                                  │
│     - 執行交易：trader.execute()                                 │
│     - Paper Trading: 記錄但不真實下單                            │
└─────────────────────────────────────────────────────────────────┘
```

## 🔑 關鍵檢查點

### 1. 配置文件 (config/futures_night_weak_bear.yaml)

```yaml
active_strategy: weak_bear_trend  # ← 這裡決定使用哪個策略
```

**影響**: Monitor 啟動時會讀取這個配置，設置 `self.active_strategy_name = "weak_bear_trend"`

### 2. Strategy Registry (自動發現)

```python
# core/strategy_registry.py
reg = StrategyRegistry()
reg.discover()  # 掃描 strategies/plugins/futures/*.py

# weak_bear_trend.py 會被自動發現並註冊
# 因為它繼承 StrategyBase 並有 name = "weak_bear_trend"
```

**影響**: weak_bear_trend 必須在 registry 中才能被調用

### 3. Router 配置 (core/futures_strategy_router.py)

```python
# 候選策略列表
weak_strategies = (
    "adaptive_orb", "adaptive_orb_v15", "trend_continuation_v1",
    "counter_vwap", "spring_upthrust", "kbar_feature",
    "calendar_condor_v2", "range_mean_reversion_v1",
    "weak_bear_trend"  # ← 必須在這裡！
)

# 策略政策
STRATEGY_POLICY = {
    "weak_bear_trend": {
        "enabled_regimes": ["WEAK", "CHOP"],  # ← 只在 WEAK/CHOP 啟用
        "max_weight": 0.5,
        "kill_if_cagr_below": -0.05,
        "required_bias": "SHORT",  # ← 只在 bias=SHORT 時進場
    }
}
```

**影響**: 
- weak_bear_trend 只在 WEAK/CHOP regime 被考慮
- 必須 bias=SHORT 才能進場

### 4. Regime 分類 (core/futures_bar_regime.py)

```python
bar_regime = classify_futures_bar_regime(bar, session_regime)

# 返回：
# regime="WEAK"  (因為 ADX < 22, 震盪格局)
# bias="SHORT"   (因為價格 < VWAP, EMA 空頭排列)
```

**影響**: 只有當 bar_regime 是 WEAK/CHOP 且 bias=SHORT 時，weak_bear_trend 才會被考慮

### 5. 策略進場邏輯 (strategies/plugins/futures/weak_bear_trend.py)

```python
def on_bar(self, context):
    # 內部檢查
    if regime not in {"WEAK", "CHOP"}:
        return None  # 阻止進場
    
    if bias != "SHORT":
        return None  # 阻止進場
    
    if adx >= 22:
        return None  # 阻止進場
    
    # ... 其他檢查
    
    # 全部通過 → 返回 SELL 信號
    return Signal("SELL", "WEAK_BEAR_TREND", ...)
```

## 🎯 如何切換到 weak_bear_trend

### 方法 1: 修改配置文件 (推薦)

```yaml
# config/futures_night.yaml
active_strategy: weak_bear_trend  # ← 改這裡
```

**效果**: 下次 Monitor 啟動時會使用 weak_bear_trend

### 方法 2: 動態切換 (需要重啟)

```bash
# 1. 停止 Monitor (Ctrl+C)
# 2. 修改配置文件
# 3. 重新啟動
python3 main.py --config config/futures_night.yaml
```

### 方法 3: 命令行覆蓋 (如果支持)

```bash
python3 main.py --config config/futures_night.yaml \
                --override "strategy.active_strategy=weak_bear_trend"
```

## 📊 完整候選順序 (WEAK Regime)

當 `active_strategy: weak_bear_trend` 且 `regime=WEAK` 時：

```
候選順序 (由 _strategy_order_for_regime 決定):

1. weak_bear_trend      ← active_strategy 排第一
2. adaptive_orb
3. adaptive_orb_v15
4. trend_continuation_v1
5. counter_vwap
6. spring_upthrust
7. kbar_feature
8. calendar_condor_v2
9. range_mean_reversion_v1

然後 _apply_strategy_policy 會過濾:
- adaptive_orb: BLOCK (enabled_regimes=["TREND", "SQUEEZE"])
- adaptive_orb_v15: BLOCK (enabled_regimes=["TREND", "SQUEEZE"])
- trend_continuation_v1: BLOCK (enabled_regimes=["TREND"])
- counter_vwap: ALLOW (enabled_regimes=["WEAK", "CHOP", "SQUEEZE"])
- spring_upthrust: ALLOW (enabled_regimes=["WEAK", "CHOP", "SQUEEZE"])
- kbar_feature: ALLOW (enabled_regimes=["WEAK", "CHOP"])
- calendar_condor_v2: ALLOW (enabled_regimes=["WEAK", "CHOP"])
- range_mean_reversion_v1: ALLOW (假設在列表中)
- weak_bear_trend: ALLOW (enabled_regimes=["WEAK", "CHOP"])

最終候選:
1. weak_bear_trend      ← 優先評估
2. counter_vwap
3. spring_upthrust
4. kbar_feature
5. calendar_condor_v2
6. range_mean_reversion_v1
```

## ✅ 驗證 weak_bear_trend 是否被調用

### 日誌檢查

```bash
# 查看策略評估日誌
grep "weak_bear_trend" logs/shioaji.log

# 預期輸出:
# [STRATEGY_POLICY][ALLOW] weak_bear_trend: ENABLED
# [WEAK_BEAR_SIGNAL] close=22000 vwap=22050 adx=18.0 mom_velo=-8.0
```

### Router 日誌

```bash
# 查看 router 決策
grep "route_futures_signal\|selected_strategy" logs/shioaji.log

# 預期輸出:
# [Router] selected_strategy=weak_bear_trend action=SELL
```

## 🚨 常見問題

### 問題 1: weak_bear_trend 未被調用

**可能原因**:
1. Regime 不是 WEAK/CHOP
2. Bias 不是 SHORT
3. 配置文件未更新
4. 策略未正確註冊

**解決方案**:
```bash
# 1. 檢查 regime
grep "regime" logs/shioaji.log | tail -20

# 2. 檢查配置文件
cat config/futures_night.yaml | grep active_strategy

# 3. 檢查策略註冊
python3 -c "from core.strategy_registry import StrategyRegistry; \
            r = StrategyRegistry(); r.discover(); \
            print([s for s in r.list_all() if 'weak_bear' in s[0]])"
```

### 問題 2: weak_bear_trend 被 policy 阻止

**可能原因**:
- CAGR < -5% (觸發 kill switch)
- Regime 不匹配

**解決方案**:
```bash
# 檢查政策檢查日誌
grep "STRATEGY_POLICY" logs/shioaji.log | grep weak_bear_trend
```

---

*文檔生成時間：2026-05-07*
*Router 版本：futures_strategy_router.py v2*
