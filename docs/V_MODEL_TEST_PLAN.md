# V-Model 測試計畫 — tw-trading-unified

## V-Model 對應

```
需求分析 ──────────────────────────────── 驗收測試 (UAT)
    │                                         │
    ▼                                         ▼
  系統設計 ────────────────────────── 系統測試 (Integration)
      │                                     │
      ▼                                     ▼
    模組設計 ──────────────────── 模組測試 (Unit)
        │                               │
        ▼                               ▼
      實作 ──────────────────── 程式碼審查
```

---

## Level 1: 單元測試 (Unit Tests)

每個 bug 對應一個測試，確保不再發生。

### 1.1 TradeExecutor 測試
```python
# tests/test_trade_executor.py

def test_no_duplicate_entry():
    """Bug: 重複進場 50+ 筆"""
    trader = PaperTrader("TMF", 100000, 10, 20, 0, 0)
    r1 = trader.execute_signal("BUY", 32800, "2026-04-02", lots=2, max_lots=2)
    r2 = trader.execute_signal("BUY", 32800, "2026-04-02", lots=2, max_lots=2)
    assert r1 is not None
    assert r2 is None  # 已滿倉，不能再進
    assert trader.position == 2

def test_exit_zeroes_position():
    """Bug: EXIT 後 position 沒歸零導致重複 EXIT"""
    trader = PaperTrader("TMF", 100000, 10, 20, 0, 0)
    trader.execute_signal("BUY", 32800, "2026-04-02", lots=2, max_lots=2)
    trader.execute_signal("EXIT", 32900, "2026-04-02", lots=2, max_lots=2)
    assert trader.position == 0
    r = trader.execute_signal("EXIT", 32900, "2026-04-02", lots=2, max_lots=2)
    assert r is None  # 沒有持倉，不能出場

def test_be_offset_covers_fees():
    """Bug: BE offset 2 pts < 手續費 8 pts"""
    trader = PaperTrader("TMF", 100000, 10, 20, 0, 0)
    trader.execute_signal("SELL", 32335, "2026-04-02", lots=2, max_lots=2,
                          stop_loss=50, break_even_trigger=50)
    trader.update_trailing_stop(32285)  # 浮盈 50 pts → 觸發 BE
    assert trader.be_triggered
    # BE stop 應該在 entry - 10，不是 entry - 2
    assert trader.current_stop_loss == 32335 - 10

def test_stop_loss_uses_market_price():
    """Bug: 停損用 stop_loss level 而非市場價"""
    # 停損觸發時，出場價應該是市場價（可能更差），不是停損線
    pass  # 在 monitor 層測試

def test_pnl_includes_fees():
    """Bug: CSV PnL 沒扣手續費"""
    trader = PaperTrader("TMF", 100000, 10, 20, 0, 0.00002)
    trader.execute_signal("BUY", 32000, "2026-04-02", lots=2, max_lots=2)
    result = trader.execute_signal("EXIT", 32010, "2026-04-02", lots=2, max_lots=2)
    # 毛利 = 10 * 10 * 2 = 200
    # 手續費 = 20 * 2 * 2 = 80
    # 稅 = (32000+32010) * 10 * 0.00002 * 2 ≈ 25.6
    # 淨利 = 200 - 80 - 25.6 ≈ 94.4
    assert trader.trades[-1]["pnl_cash"] < 200  # 必須小於毛利
    assert trader.trades[-1]["pnl_cash"] > 0    # 但還是正的
```

### 1.2 Options Position 測試
```python
# tests/test_options_position.py

def test_no_duplicate_paper_entry():
    """Bug: 重啟後重複進場 5 次"""
    monitor = create_mock_monitor()
    monitor.enter_paper_position("P", mock_signal(score=-80))
    assert monitor.position == 2
    monitor.enter_paper_position("P", mock_signal(score=-80))
    assert monitor.position == 2  # 不變，被 guard 擋住

def test_exit_clears_position_before_log():
    """Bug: EXIT 被重複觸發 5 次"""
    monitor = create_mock_monitor()
    monitor.enter_paper_position("P", mock_signal(score=-80))
    monitor.exit_paper_position("PAPER_EXIT", 800, "test")
    assert monitor.position == 0
    monitor.exit_paper_position("PAPER_EXIT", 800, "test")
    # 第二次不應該寫 log（position 已經是 0）

def test_pnl_multiplied_by_quantity():
    """Bug: PnL 沒乘口數"""
    monitor = create_mock_monitor()
    monitor.position = 2
    monitor.entry_price = 925
    monitor.active_side = "P"
    monitor.exit_paper_position("PAPER_EXIT", 805, "test")
    # PnL = (805 - 925) * 50 * 2 = -12000
    last_trade = read_last_ledger_entry(monitor)
    assert last_trade["PnL"] == -12000

def test_paper_margin_check():
    """Bug: 40000 本金買了 92500 的 ATM"""
    monitor = create_mock_monitor(initial_capital=40000)
    result = monitor._paper_margin_check(entry_price=925)  # 925*50*2=92500
    assert result == False  # 應該被擋

def test_entry_price_sanity():
    """Bug: 進場價 97.7 的 ATM Call"""
    monitor = create_mock_monitor()
    # ATM Call 不可能 < 100 pts when underlying ~32000
    monitor.market_data["C"]["ask"] = 97.7
    monitor.enter_paper_position("C", mock_signal(score=80))
    assert monitor.position == 0  # 應該被擋（價格不合理）
```

### 1.3 Strategy Plugin 測試
```python
# tests/test_entry_strategies.py

def test_squeeze_breakout_long():
    state = make_state(sqz_on=False, score=40, mom_state=3, bullish_align=True)
    signal = strategy_squeeze_breakout(state, default_cfg)
    assert signal is not None
    assert signal["action"] == "BUY"

def test_squeeze_breakout_no_signal_when_squeeze_on():
    state = make_state(sqz_on=True, score=40, mom_state=3)
    signal = strategy_squeeze_breakout(state, default_cfg)
    assert signal is None

def test_trend_follow_requires_ema_alignment():
    state = make_state(sqz_on=False, score=40, bullish_align=False)
    signal = strategy_trend_follow(state, default_cfg)
    assert signal is None  # EMA 不對齊，不進場

def test_each_strategy_returns_valid_format():
    """所有策略的 return 格式一致"""
    for name, fn in STRATEGIES.items():
        state = make_state(sqz_on=False, score=50, mom_state=3, bullish_align=True)
        result = fn(state, default_cfg)
        if result is not None:
            assert "action" in result
            assert "reason" in result
            assert "stop_loss" in result
            assert result["action"] in ("BUY", "SELL")
            assert result["stop_loss"] > 0
```

---

## Level 2: 整合測試 (Integration Tests)

### 2.1 完整交易循環
```python
# tests/test_integration.py

def test_full_trade_cycle():
    """進場 → 持倉管理 → 出場，驗證所有狀態一致"""
    monitor = create_real_monitor(dry_run=True)
    monitor.setup()
    
    # 注入假數據觸發進場
    inject_bars(monitor, trend="up", bars=50)
    monitor._strategy_tick()
    
    assert monitor.trader.position > 0
    assert len(read_trades()) == 1
    
    # 注入反轉數據觸發出場
    inject_bars(monitor, trend="down", bars=10)
    monitor._strategy_tick()
    
    assert monitor.trader.position == 0
    assert len(read_trades()) == 2
    assert read_trades()[-1]["pnl_cash"] != 0

def test_restart_recovery():
    """重啟後持倉恢復正確"""
    monitor1 = create_real_monitor()
    monitor1.enter_paper_position("P", mock_signal())
    assert monitor1.position == 2
    
    # 模擬重啟
    monitor2 = create_real_monitor()
    monitor2._recover_position_from_api()
    assert monitor2.position == 2
    assert monitor2.entry_price == monitor1.entry_price

def test_strategy_switch_no_orphan():
    """切換策略不會留下孤立持倉"""
    monitor = create_real_monitor()
    # 用策略 A 進場
    monitor.cfg["strategy"]["active_strategy"] = "squeeze_breakout"
    monitor._strategy_tick()  # 進場
    # 切換到策略 B
    monitor.cfg["strategy"]["active_strategy"] = "trend_follow"
    # 出場邏輯不受策略切換影響
    monitor._strategy_tick()  # 應該正常管理持倉
```

### 2.2 Dashboard ↔ Config ↔ Monitor
```python
def test_dashboard_config_roundtrip():
    """Dashboard 改 config → Monitor 讀到新值"""
    save_yaml(FUTURES_CFG, {"strategy": {"active_strategy": "trend_follow"}})
    cfg = load_yaml(FUTURES_CFG)
    assert cfg["strategy"]["active_strategy"] == "trend_follow"
```

---

## Level 3: 系統測試 (System Tests)

### 3.1 Replay 回測驗證
```bash
# 用今晚數據 replay，驗證每個策略的交易次數和 PnL 合理
python3 scripts/backtest_tonight.py
# 預期：
#   - 每個策略最多 1 筆持倉
#   - PnL 計算含手續費
#   - 沒有重複交易
```

### 3.2 壓力測試
```python
def test_rapid_tick_no_duplicate():
    """快速 tick 不會產生重複交易"""
    monitor = create_real_monitor()
    for i in range(100):
        monitor.on_tick(None, make_tick(32800 + i))
    # 最多只有 1 筆進場
    assert len(read_trades()) <= 2  # 1 entry + maybe 1 exit
```

### 3.3 夜盤跨日測試
```python
def test_overnight_date_handling():
    """凌晨 00:00~05:00 使用前一天的日期"""
    with mock_time("2026-04-03 02:00:00"):
        storage = DataStorage("TMF")
        assert storage.date_str == "20260402"
```

---

## Level 4: 驗收測試 (UAT Checklist)

### 每次部署前必須通過：

- [ ] `python3 -m pytest tests/ -v` 全部通過 (83 tests)
- [ ] `python3 main.py --dry-run` 啟動無 error，indicator CSV 正常寫入
- [ ] Dashboard 可開啟，策略切換正常
- [ ] 模擬交易：進場 1 次、出場 1 次、PnL 正確
- [ ] 重啟後持倉恢復正確
- [ ] 本金 40,000 限制生效（ATM 被擋）
- [ ] Ctrl-C 觸發優雅關閉，無 "Python quit unexpectedly" 彈窗
- [ ] macOS 安全測試通過：`python3 -m pytest tests/test_macos_safety.py -v`

---

## CI 自動化（建議）

```yaml
# .github/workflows/test.yml
name: Trading System Tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -r requirements.txt
      - run: python -m pytest tests/ -v --tb=short
      - run: python -c "import py_compile; [py_compile.compile(f, doraise=True) for f in ['strategies/futures/monitor.py','strategies/options/live_options_squeeze_monitor.py']]"
```

---

## 優先實作順序

| 優先級 | 測試 | 防止的 Bug |
|--------|------|-----------|
| P0 | `test_no_duplicate_entry` | 重複下單 50 筆 |
| P0 | `test_exit_clears_position_before_log` | EXIT 重複 5 次 |
| P0 | `test_paper_margin_check` | 超過本金限制 |
| P1 | `test_pnl_includes_fees` | PnL 計算錯誤 |
| P1 | `test_be_offset_covers_fees` | BE 不夠 cover 手續費 |
| P1 | `test_restart_recovery` | 重啟後重複開單 |
| P2 | `test_entry_price_sanity` | 進場價 97.7 |
| P2 | `test_strategy_returns_valid_format` | 策略插件格式錯誤 |
| P2 | `test_overnight_date_handling` | 跨日日期錯誤 |
