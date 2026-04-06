# SDD: Taiwan Stock Module (Odd-Lot Optimized)

## 1. 模組概述 (Overview)
本模組旨在將台股（特別是零股）交易整合進 `tw-trading-unified`。其核心設計目標是「共用 Session、資金隔離、數據解耦、合規執行」。

## 2. 核心架構 (Architecture)
採用「分析與執行分離」架構：
*   **分析端 (Analysis)**: 使用 Round Lot (整股) K 線資料進行技術指標運算 (Squeeze, SMA, ADX)。
*   **執行端 (Execution)**: 偵測訊號後，切換至 Odd Lot (零股) 模式進行 Snapshots 報價獲取與 Order 下單。

## 3. 關鍵限制與解決方案 (Constraints)
| 限制條件 | 解決方案 | 實作位置 |
| :--- | :--- | :--- |
| **零股不支援當沖** | 建立 `entry_day` 鎖定機制，強制禁止同一交易日賣出。 | `backtest/stock_engine.py` |
| **手續費低消 (20 TWD)** | 在成本計算函數中加入 `max(20, amount * rate)` 邏輯。 | `backtest/stock_engine.py` |
| **API 登入限制** | 透過 `core/shioaji_session.py` 維持單一持久 Session。 | `core/` |

## 4. 資料流 (Data Flow)
1.  **Scanner**: 每小時掃描 Watchlist -> 抓取整股 Kbars -> 算出指標 -> 標記狀態 (Squeezing/Fired)。
2.  **Monitor**: 偵測狀態變化 -> 獲取零股 Snapshot -> 計算可買股數 (Capital / Price) -> 發送 Odd Lot Order。
3.  **Risk Manager**: 持續檢查「今日買入鎖定」狀態與「13:20 強制出場」邏輯。

## 5. 數據結構 (Data Structures)
`StockState`: 包含 `df_5m_round` (分析用), `last_odd_price` (執行用), `entry_day`, `qty`。
