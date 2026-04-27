# Data Safety Architecture
Scope: tw-trading-unified futures module

## Overview

保护原始 API 数据不因进程崩溃而丢失。核心原则：

> **原始 CSV = 真相来源** — 内存缓存（deque/DataFrame）只是运行时性能层

## 数据层

```
Shioaji API
    │
    ├── Tick callback ──→ RawTickWriter ──→ logs/raw_ticks/{code}_{day}_ticks.csv
    │                          │
    │                    [CSV before memory]        
    │                          │
    │                    内存 deque (self._tick_bars_deque)
    │                          │
    │                    _get_tick_bars_df() ──→ 5m bars DataFrame
    │
    └── api.kbars() ──→ RawKbarWriter ──→ logs/raw_kbars/{code}_{day}_kbars.csv
                             │
                       [CSV before computation]
                             │
                        1m kbar DataFrame
```

## 组件

### 1. RawTickWriter (已完成)
- **文件**: `strategies/futures/squeeze_futures/data/tick_writer.py`
- **输出**: `logs/raw_ticks/{contract}_{YYYYMMDD}_ticks.csv`
- **字段**: `timestamp, trading_day, symbol, price, volume, bid_price, ask_price, ts_int`
- **调用点**: `FuturesMonitor.on_tick()` — 在内存操作之前
- **特性**:
  - 惰性初始化（第一个真实 TMF 标记时）
  - 每 100 条记录刷新
  - 写入失败静默处理（不崩溃 tick 回调）
  - 重启时构造函数无法打开

### 2. RawKbarWriter (新建)
- **文件**: `strategies/futures/squeeze_futures/data/kbar_writer.py`
- **输出**: `logs/raw_kbars/{contract}_{YYYYMMDD}_kbars.csv`
- **字段**: `ts, Open, High, Low, Close, Volume, Amount, trading_day`
- **调用点**: `_save_raw_kbars()` 在 `_fetch_today_kbars()` 中 — API 响应后、数据处理前

### 3. 启动重建 (新建)
- **方法**: `FuturesMonitor._rebuild_bars_from_raw_ticks()`
- **调用点**: `run()` 中的 Phase A.5（仓位恢复后、Phase B 后台填充前）
- **逻辑**:
  1. 检查今天是否有原始 tick CSV
  2. 读取所有 tick → 按时间戳排序 → 按 5 分钟桶分组
  3. 为每个桶构建 OHLCV 条
  4. 用重建的条填充 `_tick_bars_deque`
  5. 设置 `_last_bar_ts` 防止重新处理
  6. 将 `_current_bar` 设置为最后一个条的价格的当前位置

## 数据流优先级

所有数据流强制执行三个来源，按优先级排列：

### 数据源优先级
| 优先级 | 来源 | 描述 |
|----------|--------|-------------|
| 1（主） | Tick-based bars (deque) | 来自 `_get_tick_bars_df()` 的 5m 条，由 `RawTickWriter` CSV 支持 |
| 2（次要） | API kbars (backfill) | 频率限制为 120s 的 `_periodic_backfill_bars()`，由 `RawKbarWriter` CSV 支持 |
| 3（第三级） | Legacy API kline | 频率限制为 300s 的 `client.get_kline()`，已弃用 |

### 启动流程
```
Phase A:  仓位恢复 (api.list_positions)
Phase A.5: 从原始 tick CSV 重建 5m 条 (读取 CSV → 构建条 → 填充 deque)
Phase B:  后台 K-bar 填充 (速率有限, 异步)
Phase C:  正常运行 - tick 入站 → CSV → deque → 指标 → 策略
```

## 严格规则

### 通用数据安全规则
1. `strategy/` 和 `indicator/` 中的代码 **不能** `import shioaji` 或调用 `api.*`。
2. 仅 `ingestion/` 层可以使用 Shioaji API。
3. 每次 `api.kbars()` 响应必须在处理前保存到原始 CSV。
4. Ticks 必须在任何内存使用前保存到原始 CSV。
5. CSV 写入失败 **绝不能** 使 tick 回调或 API 调用崩溃。

### Shioaji API 存取紀律（2026-04-24 制定）

此規則明確劃分 strategy loop 中哪些 API 操作是允許的、哪些是禁止的。

#### 正常策略路徑
**Strategy 只能從 canonical bars 讀取資料**（即 tick-based bars → deque → canonical bar pipeline）。
Strategy 永遠不直接使用 Shioaji API 的回傳值做信號判斷。

```
策略信號路徑:
  Tick callback → RawTickWriter CSV → _tick_bars_deque → _get_tick_bars_df() → canonical bars → indicator → signal
                                                                     ↑
  api.kbars() → RawKbarWriter CSV → _periodic_backfill_bars() ──────┘
                                                                     ↑
  client.get_kline() → _save_raw_kbars() ──→ 僅在 primary 全空時 fallback
```

#### 允許的 API 存取
| # | 情境 | 說明 | 強制條件 |
|---|------|------|----------|
| 1 | `on_tick()` callback 接收 | Shioaji 主動推送的即時 tick，屬於被動接收 | 寫入 RawTickWriter CSV 後才能進記憶體 |
| 2 | startup backfill | `setup()` 中的 `_fetch_today_kbars()` 或 `client.get_kline()` | 寫入 RawKbarWriter CSV 後才能計算指標 |
| 3 | scheduled backfill | `_periodic_backfill_bars()` 每 120s 一次 | 只允許從 `_periodic_backfill_bars()` 間接呼叫，不允許 strategy_tick 直接呼叫 |
| 4 | 合約元資料 / stale check | `_check_futures_contract_staleness()` 檢查 tick age、rollover、delivery date | 不產生 signal、不注入 deque；recovery kline fetch 必須 `_save_raw_kbars()` |

#### 禁止的 API 存取
| # | 行為 | 違反後果 |
|---|------|----------|
| 1 | `strategy_tick()` 中直接呼叫 `api.kbars()` 或 `client.get_kline()` | ❌ 違反 ingestion 層分層；`_fetch_today_kbars()` 有 runtime guard 攔截 |
| 2 | 信號生成依賴 live `get_kline()` 回傳值 | ❌ 規避 canonical bar pipeline；規避 CSV persistence；資料在 process crash 時遺失 |
| 3 | API 回傳值在持久化前被用於計算 | ❌ 違反 CSV first 原則；process crash 時資料無法 recovery |

#### 例外：contract staleness recovery
`_check_futures_contract_staleness()` 中的 kline fetch 是**僅有的 recovery 例外**，但必須滿足：
- 寫入 `_save_raw_kbars()` 後才能更新 `last_tick_at`
- 不直接注入 `_tick_bars_deque`（資料透過 canonical bar pipeline 進入）
- 有獨立 rate limit（`_last_recovery_kline_at`，120s）
