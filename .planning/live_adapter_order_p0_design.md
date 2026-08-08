# Live Adapter Order P0 — design（shioaji 1.7.0 OrderType.MTL 不存在）

**狀態**: DESIGN + RED tests（codex 2026-08-08 授權獨立 P0，與 Phase-2
certification 分離）; 先 design + RED，再 implementation/review 分離。
**No deploy**。authority 凍結、dirty files 不動。

## 1. 問題（實證）

- Mini shioaji **1.7.0**: `OrderType = [FOK, IOC, ROD]`（**無 MTL**）;
  `FuturesPriceType = [LMT, MKP, MKT]`; `Action = [Buy, Sell]`;
  `sj.constant.*` 已 deprecated（→ `sj.OrderType` 等）
- `strategies/futures/squeeze_futures/data/shioaji_client.py:202`
  `order_type=sj.constant.OrderType.MTL` → **AttributeError at build** →
  try/except 吞掉 → `return None` — **live adapter 路徑每單必失敗且靜默**
  （P0: live order-build failure, not tuning）

## 2. 修正契約（含 three-line correction B）

1. **market/market-protection order 對映到 SDK-valid enum**（cite 安裝版
   API, 不猜）: `price=0`（market）→ `price_type=MKP` + **`order_type=ROD`**
   （ROD = 當日有效標準單; IOC/FOK 為特殊執行約束 — market-protection
   safety stop 用 LMT+ROD, 已 valid, 不動）
2. **sj.Order 用非 deprecated enums 建構**（`sj.OrderType` /
   `sj.FuturesPriceType`, 不用 `sj.constant.*`）; recording API 成功時
   **恰一次 intended call**
3. **typed `AdapterOrderError`**（stable code + context: method/contract/
   order/underlying）— adapter 不發明 durable ledger（缺 authoritative
   trade/order context）; **durable event/order-manager propagation 由
   caller 擁有**（monitor 3847/5208 捕捉 → 寫 durable failure event →
   回傳 structured code）— 取代 ambiguous None; API 若仍 None-returning,
   typed failure 不得被吞
4. 涵蓋 adapter 全部 place/update/cancel 路徑; **PAPER 行為不變**
   （adapter 為 live-only, paper_fill_sim 不經此）; 測試零真實 order

## 3. 範圍

- 只改 `shioaji_client.py`（place_order / update_order / cancel_order +
  必要 helper）; 不碰 monitor/order manager/main/config
- 既有 caller（monitor 3847/5208）簽名不變

## 4. 交付順序

1. 本設計 + RED tests（本輪）→ codex 審查 → 2. implementation（adapter
   only）→ 3. GREEN + 回報 → 4. 獨立 deploy 決策。
