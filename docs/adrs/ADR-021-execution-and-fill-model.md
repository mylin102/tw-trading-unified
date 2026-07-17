# ADR-021: Execution and Fill Model (執行與成交撮合模型)

## Status
Proposed (Draft)

## Context (背景)
當動態軌跡重播因決策改變而觸發**路徑分歧 (Counterfactual Forking)**（ADR-020）後，Replay 引擎將作廢所有的歷史內生成交回報（`BROKER_FILL`）。此時，虛擬策略引擎所發出的所有虛擬委託（`VIRTUAL_ORDER_SUBMIT`）必須由模擬撮合機制接管，以產生反事實的虛擬成交事件（`VIRTUAL_FILL`）。

建立一個高擬真的成交模型是極其複雜的，如果我們試圖在第一版中直接建立涵蓋「滑價、排隊排隊深度、網路延遲、券商拒單」的黑盒模型，將會使得：
1. **模型不可辨識**：我們無法釐清策略績效的改變是源於「策略參數的優化」還是「撮合模型假設的漏洞」。
2. **調試與認證困難**：基線還原認證將變得極難對齊。

因此，我們必須對執行與成交模型採用**層次化、插件化 (Layered Decorator Pattern)** 的架構設計，並在第一版中僅採用最基礎的確定性理想撮合。

## Decision (決策)
我們決定在 Trajectory Replay 中引入**裝飾器模式的成交模型 (Layered Execution Model)**。具體設計如下：

### 1. 裝飾器層次架構 (Layered Decorator Architecture)
撮合引擎採用組合裝飾器模式，每一層都是一個獨立的邏輯元件，可以被動態裝配、替換或繞過：

```
Virtual Order (VIRTUAL_ORDER_SUBMIT)
        ↓
┌─────────────────────────────────┐
│ IdealTouchModel (理想基礎撮合)   │ ➔ 判定價格是否觸及、是否具備最基本成交條件
└─────────────────────────────────┘
        ↓
┌─────────────────────────────────┐
│ LatencyDecorator (延遲裝飾器)   │ ➔ 注入網路傳輸與券商處理延遲 (時間軸向偏移)
└─────────────────────────────────┘
        ↓
┌─────────────────────────────────┘
│ SlippageDecorator (滑價裝飾器)   │ ➔ 依據市場量能與委託口數模擬滑價價格損耗
└─────────────────────────────────┘
        ↓
┌─────────────────────────────────┐
│ FeeAndTaxDecorator (稅費裝飾器)  │ ➔ 計算期交稅與手續費 (PnL 扣減)
└─────────────────────────────────┘
        ↓
Virtual Fill Event (VIRTUAL_FILL)
```

### 2. 第一版認證標準：理想確定性撮合 (Idealized Deterministic Execution)
在 v1.0.0 Trajectory Replay 的第一版實作中，我們將僅啟用基礎 `IdealTouchModel` 與 `FeeAndTaxDecorator`，將其他裝飾器設為 No-op：
* **撮合價格與時點**：虛擬委託在發出後，立刻與當前或下一個最鄰近的 `MARKET_TICK` 進行撮合：
  * **市價單 (Market Order) / 立即成交否則取消單 (IOC)**：依當前 `MARKET_TICK` 的 Ask Price (買進) 或 Bid Price (賣出) 立即成交，成交量受限於報價口數。
  * **限價單 (Limit Order)**：當後續 `MARKET_TICK` 的成交價（Last Price）或對價觸及或優於限價時，判定成交。
* **零延遲與零滑價**：`Latency == 0ms`，`Slippage == 0 pt`。
* **真實性標記 (Realism Flag)**：此階段產出的所有軌跡績效，在報告與 Provance 元數據中必須強制標記為：
  ```json
  "performance_realism": "IDEALIZED"
  ```
  禁止向研究人員宣稱此結果代表「生產實戰擬真盈虧 (Production Realistic Performance)」。

### 3. 可插拔接口契約 (Swappable Contract Interface)
```python
class ExecutionModel(ABC):
    @abstractmethod
    def process_order(self, order: VirtualOrder, current_tick: MarketTick) -> list[VirtualFill]:
        pass

class ExecutionDecorator(ExecutionModel):
    def __init__(self, base_model: ExecutionModel):
        self._base = base_model
        
    def process_order(self, order: VirtualOrder, current_tick: MarketTick) -> list[VirtualFill]:
        fills = self._base.process_order(order, current_tick)
        return self.decorate_fills(fills, current_tick)
        
    @abstractmethod
    def decorate_fills(self, fills: list[VirtualFill], current_tick: MarketTick) -> list[VirtualFill]:
        pass
```

## Consequences (後果)
1. **研究控制變因**：研究人員可以通過明確啟用或禁用特定的 Decorator（如 `SlippageDecorator`），單獨隔離評估「滑價對策略的敏感度影響」。
2. **防止錯誤樂觀**：`performance_realism = IDEALIZED` 的標籤將在系統層面警示使用者，避免其將理想撮合下的超額利潤直接等價為實戰結果。
3. **第一版實現難度降低**：基礎 `IdealTouchModel` 的確定性行為極易撰寫測試與對齊歷史，大幅降低了第一版的交付風險。
