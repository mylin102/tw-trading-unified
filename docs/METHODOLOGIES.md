# Methodologies Reference — tw-trading-unified

This document consolidates the core development methodologies that govern how we build and maintain this trading system.

---

## 1. GSTACK Methodology

**Gstack** is a comprehensive QA/testing and development framework with these core principles:

### Core Ethos (ETHOS.md)

1. **Boil the Lake** — AI-assisted coding makes marginal cost of completeness near-zero. Always do the complete thing:
   - 100% test coverage for modules
   - Full feature implementation, all edge cases
   - Complete error paths
   - Tests are the cheapest lake to boil

2. **Search Before Building** — Three layers of knowledge:
   - **Layer 1**: Tried and true (standard patterns, battle-tested)
   - **Layer 2**: New and popular (current best practices, blog posts)
   - **Layer 3**: First principles (original observations, most valuable)

3. **User Sovereignty** — AI models recommend, users decide:
   - Two AI models agreeing is signal, not mandate
   - User always has context models lack
   - Generation-verification loop: AI generates, user verifies and decides

### Key Metrics (Compression Ratio)

| Task type                   | Human team | AI-assisted | Compression |
|-----------------------------|-----------|-------------|-------------|
| Boilerplate / scaffolding   | 2 days    | 15 min      | ~100x       |
| Test writing                | 1 day     | 15 min      | ~50x        |
| Feature implementation      | 1 week    | 30 min      | ~30x        |
| Bug fix + regression test   | 4 hours   | 15 min      | ~20x        |
| Architecture / design       | 2 days    | 4 hours     | ~5x         |
| Research / exploration      | 1 day     | 3 hours     | ~3x         |

### Completion Status Protocol

Always report status using one of:
- **DONE** — All steps completed successfully. Evidence provided for each claim.
- **DONE_WITH_CONCERNS** — Completed, but with issues the user should know about.
- **BLOCKED** — Cannot proceed. State what is blocking and what was tried.
- **NEEDS_CONTEXT** — Missing information required to continue.

### Escalation Rule

- If attempted 3 times without success → **STOP and escalate**
- If uncertain about security-sensitive change → **STOP and escalate**
- If scope exceeds what can be verified → **STOP and escalate**

---

## 2. SDD (Software Design Document) Methodology

**SDD** defines the architectural principles and contracts for this trading system.

### Core Principles (from docs/SDD.md)

#### 2.1 Single Source of Truth (SSOT)
- `PaperTrader.position` is the **only** truth for position state
- Ledger CSV is a LOG, not a state store
- On restart: recover from API (live) or ledger (paper), then trust in-memory state

#### 2.2 Side Effects After Validation
```
❌ save_trade() → execute_signal()
✅ execute_signal() → if success → save_trade()
```
- Side effects (CSV write, log, notification) MUST happen AFTER core operation succeeds
- If `execute_signal()` returns None, write NOTHING

#### 2.3 Defensive Programming
Every public method's first line does precondition check:

**Entry checks:**
- position == 0 (not already in position)
- margin sufficient (can afford this trade)
- price > 0 (valid price)
- not same bar (already traded this bar)
- price reasonable (no 97.7 ATM Call when underlying ~32000)

**Exit checks:**
- position != 0 (something to exit)
- entry_price > 0 (valid entry price)
- Zero position BEFORE logging
- Pass explicit quantity to log

#### 2.4 No Namespace Pollution
```python
# ❌ This breaks datetime.timedelta:
from datetime import datetime
now = datetime.now()
yesterday = now - datetime.timedelta(days=1)  # AttributeError!

# ✅ Option A: import module
import datetime
now = datetime.datetime.now()
yesterday = now - datetime.timedelta(days=1)

# ✅ Option B: import both explicitly
from datetime import datetime, timedelta
yesterday = datetime.now() - timedelta(days=1)
```

### Module Responsibility

```
┌─────────────────────────────────────────────────────┐
│                    main.py                          │
│  職責：啟動、訂閱、分發、健康檢查                      │
│  不做：交易邏輯、狀態管理                              │
├─────────────────────────────────────────────────────┤
│          FuturesMonitor                             │
│  職責：指標計算、策略信號產生、呼叫 Executor           │
│  不做：直接操作 position、直接寫 CSV                   │
├─────────────────────────────────────────────────────┤
│          OptionsMonitor                             │
│  職責：Greeks 計算、信號產生、呼叫 Executor            │
│  不做：直接操作 position、直接寫 CSV                   │
├─────────────────────────────────────────────────────┤
│          TradeExecutor (新增)                        │
│  職責：驗證 → 執行 → 記錄（唯一寫入點）                │
│  保證：execute 成功才寫紀錄，position 是唯一真相源      │
├─────────────────────────────────────────────────────┤
│          DataStorage                                │
│  職責：持久化（CSV/JSON），不做業務邏輯                 │
└─────────────────────────────────────────────────────┘
```

### Data Flow (Corrected)

```
Strategy Signal
    │
    ▼
TradeExecutor.execute()
    ├─ 1. Precondition check (position, margin, price)
    ├─ 2. PaperTrader.execute_signal() → position 更新
    ├─ 3. 成功？
    │   ├─ Yes → save_trade() + log_trade()
    │   └─ No  → return None（不寫任何東西）
    └─ 4. return TradeResult
```

### Interface Contracts

#### TradeExecutor.execute()
```python
def execute(self, signal: str, price: float, lots: int, **kwargs) -> Optional[TradeResult]:
    """
    Preconditions:
      - signal in ("BUY", "SELL", "EXIT", "PARTIAL_EXIT")
      - price > 0
      - lots > 0

    Postconditions:
      - 成功：position 更新、紀錄寫入、return TradeResult
      - 失敗：position 不變、不寫紀錄、return None

    Invariants:
      - abs(position) <= max_positions at all times
      - 不會在同一根 bar 重複進場
      - entry price 必須通過合理性檢查
    """
```

---

## 3. V-Model Testing Methodology

**V-Model** ensures comprehensive testing at every level of development.

### V-Model Mapping

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

### Level 1: Unit Tests

Each bug gets a test to ensure it never happens again:

**TradeExecutor Tests:**
- `test_no_duplicate_entry` — Bug: 重複進場 50+ 筆
- `test_exit_zeroes_position` — Bug: EXIT 後 position 沒歸零導致重複 EXIT
- `test_be_offset_covers_fees` — Bug: BE offset 2 pts < 手續費 8 pts
- `test_stop_loss_uses_market_price` — Bug: 停損用停損價而非市場價
- `test_pnl_includes_fees` — Bug: CSV PnL 沒扣手續費

**Options Position Tests:**
- `test_no_duplicate_paper_entry` — Bug: 重啟後重複進場 5 次
- `test_exit_clears_position_before_log` — Bug: EXIT 被重複觸發 5 次
- `test_pnl_multiplied_by_quantity` — Bug: PnL 沒乘口數
- `test_paper_margin_check` — Bug: 40000 本金買了 92500 的 ATM
- `test_entry_price_sanity` — Bug: 進場價 97.7 的 ATM Call

**Strategy Plugin Tests:**
- `test_squeeze_breakout_long`
- `test_squeeze_breakout_no_signal_when_squeeze_on`
- `test_trend_follow_requires_ema_alignment`
- `test_each_strategy_returns_valid_format`

### Level 2: Integration Tests

- **Full trade cycle**: entry → position management → exit
- **Restart recovery**: 重啟後持倉恢復正確
- **Strategy switch no orphan**: 切換策略不會留下孤立持倉
- **Dashboard ↔ Config ↔ Monitor roundtrip**

### Level 3: System Tests

- **Replay backtest verification**: 用今晚數據 replay
- **Stress testing**: 快速 tick 不會產生重複交易
- **Overnight date handling**: 凌晨 00:00~05:00 使用前一天的日期

### Level 4: UAT Checklist

Before every deployment:

- [ ] `python3 -m pytest tests/ -v` 全部通過 (83 tests)
- [ ] `python3 main.py --dry-run` 啟動無 error
- [ ] Dashboard 可開啟，策略切換正常
- [ ] 模擬交易：進場 1 次、出場 1 次、PnL 正確
- [ ] 重啟後持倉恢復正確
- [ ] 本金 40,000 限制生效（ATM 被擋）
- [ ] Ctrl-C 觸發優雅關閉，無 "Python quit unexpectedly" 彈窗
- [ ] macOS 安全測試通過：`python3 -m pytest tests/test_macos_safety.py -v`

### Implementation Priority

| Priority | Test | Prevents Bug |
|----------|------|-------------|
| P0 | `test_no_duplicate_entry` | 重複下單 50 筆 |
| P0 | `test_exit_clears_position_before_log` | EXIT 重複 5 次 |
| P0 | `test_paper_margin_check` | 超過本金限制 |
| P1 | `test_pnl_includes_fees` | PnL 計算錯誤 |
| P1 | `test_be_offset_covers_fees` | BE 不夠 cover 手續費 |
| P1 | `test_restart_recovery` | 重啟後重複開單 |
| P2 | `test_entry_price_sanity` | 進場價 97.7 |
| P2 | `test_strategy_returns_valid_format` | 策略插件格式錯誤 |
| P2 | `test_overnight_date_handling` | 跨日日期錯誤 |

---

## 4. How They Work Together

### GSTACK + SDD + V-Model Integration

1. **GSTACK** provides the **principles**:
   - Do the complete thing (Boil the Lake)
   - Search before building (avoid reinvention)
   - User decides (generation-verification loop)

2. **SDD** provides the **architecture**:
   - Single source of truth
   - Side effects after validation
   - Defensive programming (precondition checks)
   - Module responsibility boundaries

3. **V-Model** provides the **verification**:
   - Unit tests for every bug
   - Integration tests for workflows
   - System tests for edge cases
   - UAT checklist before deployment

### Development Workflow

```
1. User Request
   │
2. GSTACK: Search first (has someone solved this?)
   │
3. SDD: Design with contracts (preconditions, postconditions, invariants)
   │
4. Implementation: Boil the lake (complete thing, not shortcut)
   │
5. V-Model: Test at every level (unit → integration → system → UAT)
   │
6. GSTACK: User decides (present recommendation, ask for approval)
   │
7. Deploy: All tests pass, UAT checklist complete
```

---

## 5. Quick Reference Commands

```bash
# Run all tests (V-Model Level 1)
python3 -m pytest tests/ -v

# Run macOS safety tests
python3 -m pytest tests/test_macos_safety.py -v

# Lint (gstack health)
ruff check .

# Shell check
shellcheck autostart.sh

# Dry-run test
python3 main.py --dry-run

# Syntax check
python3 -c "import py_compile; py_compile.compile('main.py', doraise=True)"
```

---

## 6. Known Bug Patterns (from SDD Section 1)

| Bug | Root Cause | Classification |
|-----|-----------|----------------|
| BE offset 2 pts < 手續費 | Hard-coded constant, not linked to cost model | Design flaw |
| Options duplicate entry 5x 10 lots | `position = paper_lots` overwrites, not checks | State management |
| PnL not multiplied by quantity | `log_trade` didn't use position qty | Calculation error |
| Stop loss uses stop price not market price | Passed stop_loss level instead of market price | Logic error |
| CSV PnL didn't deduct fees | `_execute_trade` calculated PnL separately | Duplicate logic |
| `datetime.timedelta` crash | `from datetime import datetime` overwrote module | Naming conflict |
| EXIT triggered 5x repeatedly | `manage_open_position` triggers every poll, position zeroed but log still runs | State race condition |
| Futures duplicate orders 50+ | `save_trade` before `execute_signal` + JSON full-rewrite CSV | Execution order |
| Entry price 97.7 unreasonable | Contract switch zeroed bid/ask, fallback to wrong quote | Data validation |

---

**Last Updated:** 2026-04-07
**Status:** Active — All 83 V-Model tests passing, SDD contracts enforced, GSTACK principles applied
