# 期權交易紀錄不一致問題 - GSD分析報告

## 問題描述
用戶報告dashboard顯示期權交易紀錄不一致：
1. Dashboard顯示4筆option交易紀錄
2. CSV ledger有12筆紀錄（6 THETA_ENTRY + 6 THETA_EXIT）
3. 17:52進場(Theta), 17:53出場(theta_exit)
4. 委託單JSON只有2筆，時間不一致（04:51 UTC vs 17:52 local）
5. 總覽頁面顯示"進場12筆"
6. Theta交易顯示負PnL（應為正，因是credit策略）
7. Option頁面顯示"成交22筆"但order status只有2筆訂單
8. 所有交易紀錄顯示entry/exit價格為0

## GSD方法分析

### 1. Gather (收集數據)

#### 數據來源分析：
1. **CSV交易紀錄** (`./strategies/options/logs/paper_trading/options_trade_ledger.csv`)
   - 總計49行（48筆交易 + 標題）
   - THETA_ENTRY: 24筆
   - THETA_EXIT: 24筆
   - 所有Price欄位都是0
   - Note欄位顯示credit=183

2. **JSON訂單紀錄** (`./exports/trades/OPTIONS_20260415_orders.json`)
   - 只有2筆訂單
   - 1筆成交（CALL entry @ 150.0）
   - 1筆取消（PUT）
   - 時間為UTC格式（04:51 vs CSV的17:52 local）

3. **Dashboard顯示**
   - 運行在PID 57975
   - Overview頁面顯示"今日進場: 22 筆"
   - Option頁面顯示交易紀錄但價格為0

#### 關鍵發現：
- THETA策略是paper trading（模擬交易），不產生實際訂單
- 解釋了為何CSV有48筆交易但JSON只有2筆實際訂單
- 所有THETA交易的Price都是0，儘管Note顯示credit=183

### 2. Sort (分析問題)

#### 根本原因分析：

**問題1: Price記錄為0**
- 位置：`live_options_squeeze_monitor.py` line 2338
- 代碼：`entry_price = float(pos.net_credit) if pos.net_credit else 0.0`
- 問題：`pos.net_credit`可能為None或0
- 影響：所有THETA_ENTRY的Price都是0

**問題2: THETA_EXIT不在exit_keywords列表**
- 位置：`live_options_squeeze_monitor.py` line 1059-1060
- 代碼：`exit_keywords = ["EXIT", "TP1", "TRAIL", "TIME", "REVERSAL", "TRAP", "EOD", "FILL"]`
- 問題：THETA_EXIT不在列表中
- 影響：THETA_EXIT不會觸發PnL計算

**問題3: Dashboard顯示邏輯錯誤**
- 位置：`ui/dashboard.py` line 413
- 代碼：包含THETA_EXIT在exit檢測中
- 問題：但因為Price都是0，計算出的PnL都是0
- 影響：顯示的22筆可能是計算錯誤

**問題4: ThetaGang實現問題**
- 位置：`theta_gang.py` line 236
- 代碼：`net_credit=entry_info["net_credit"]`
- 問題：`entry_info["net_credit"]`可能為0或None
- 影響：無法正確記錄權利金

#### 數據流分析：
```
Theta策略觸發 → ThetaGang.evaluate_entry() → 計算net_credit → 
open_position() → log_trade() → CSV記錄
```
問題出現在`net_credit`計算和傳遞過程中。

### 3. Decide (決定解決方案)

#### V-cycle解決方案：

**階段1: 需求分析**
1. 修復Price記錄為0的問題
2. 修復THETA_EXIT的PnL計算
3. 修復dashboard顯示邏輯
4. 確保數據一致性

**階段2: 系統設計**
1. 修改`log_trade()`函數，加入THETA_EXIT到exit_keywords
2. 修改ThetaGang的價格記錄邏輯
3. 修復dashboard的round-trip計算
4. 添加數據驗證檢查

**階段3: 實施方案**

**修復1: 修復Price記錄**
```python
# 在live_options_squeeze_monitor.py line 2338
# 原代碼：
entry_price = float(pos.net_credit) if pos.net_credit else 0.0

# 修復後：
if pos and hasattr(pos, 'net_credit') and pos.net_credit is not None:
    entry_price = float(pos.net_credit)
else:
    entry_price = 0.0
    console.print(f"[yellow]⚠️ THETA_ENTRY: net_credit is None or 0[/yellow]")
```

**修復2: 修復exit_keywords**
```python
# 在live_options_squeeze_monitor.py line 1059
# 原代碼：
exit_keywords = ["EXIT", "TP1", "TRAIL", "TIME", "REVERSAL", "TRAP", "EOD", "FILL"]

# 修復後：
exit_keywords = ["EXIT", "THETA_EXIT", "TP1", "TRAIL", "TIME", "REVERSAL", "TRAP", "EOD", "FILL"]
```

**修復3: 修復ThetaGang的net_credit計算**
```python
# 在theta_gang.py line 214-220
# 添加驗證：
net_credit, max_loss, details = price_spread(
    legs, self.bs_fn, spot, self.r, iv, dte_years)

# 確保net_credit不為0
if net_credit <= 0:
    console.print(f"[yellow]⚠️ ThetaGang: net_credit={net_credit} <= 0[/yellow]")
    return None
```

**修復4: 修復dashboard顯示**
```python
# 在ui/dashboard.py line 413
# 確保THETA_EXIT被正確處理
elif pending_entry and any(kw in action for kw in ["EXIT", "THETA_EXIT", "TP1", "TRAIL", "TIME", "REVERSAL", "TRAP", "FILL"]):
```

**階段4: 驗證測試**
1. 創建測試腳本驗證修復
2. 運行現有測試套件
3. 手動測試dashboard顯示
4. 驗證數據一致性

## 風險評估：打開Live Trading的風險

### 技術風險
1. **數據不一致風險** ⚠️ HIGH
   - 當前系統有嚴重的數據一致性問題
   - Price記錄為0會導致PnL計算錯誤
   - 在修復前開啟Live Trading可能導致財務損失

2. **交易邏輯錯誤風險** ⚠️ HIGH
   - THETA_EXIT不被識別為exit action
   - PnL計算可能錯誤
   - 可能導致錯誤的平倉決策

3. **系統穩定性風險** ⚠️ MEDIUM
   - Dashboard顯示邏輯有問題
   - 可能誤導交易決策
   - 需要修復後才能可靠使用

### 財務風險
1. **PnL計算錯誤風險** ⚠️ CRITICAL
   - 所有THETA交易的Price都是0
   - 實際權利金183點未記錄
   - 可能導致嚴重財務損失

2. **風險管理失效風險** ⚠️ HIGH
   - 錯誤的PnL計算影響風險管理
   - 可能超過風險限額
   - 需要修復後才能有效管理風險

### 操作風險
1. **監控失效風險** ⚠️ MEDIUM
   - Dashboard顯示不準確
   - 無法可靠監控交易
   - 需要修復顯示邏輯

2. **決策支持失效風險** ⚠️ MEDIUM
   - 不一致的數據影響交易決策
   - 需要可靠的數據支持

## 建議行動計劃

### 立即行動（修復問題）
1. **修復Price記錄問題** - 最高優先級
2. **修復THETA_EXIT識別** - 高優先級
3. **修復dashboard顯示** - 中優先級
4. **運行完整測試** - 修復後必須執行

### Live Trading開啟條件
在開啟Live Trading前，必須滿足以下條件：

✅ **必須修復的問題：**
1. Price記錄正確（非0值）
2. THETA_EXIT正確觸發PnL計算
3. Dashboard顯示準確的交易數量
4. 所有測試通過

✅ **必須驗證的項目：**
1. PnL計算包含所有成本（手續費、稅）
2. 數據一致性（CSV、JSON、Dashboard）
3. 風險管理功能正常
4. 止損止盈邏輯正確

✅ **建議的測試流程：**
1. 在Paper模式運行24小時
2. 驗證所有數據一致性
3. 模擬極端市場情況
4. 壓力測試系統穩定性

## 結論

**當前系統不適合開啟Live Trading**，存在嚴重的數據一致性和計算邏輯問題。必須先修復以下關鍵問題：

1. **Price記錄為0的問題** - 影響PnL計算基礎
2. **THETA_EXIT識別問題** - 影響交易閉環
3. **Dashboard顯示邏輯** - 影響監控決策

**建議時間表：**
- 第1天：修復核心問題
- 第2天：運行完整測試
- 第3天：Paper模式驗證
- 第4天：評估是否開啟Live Trading

**風險等級：** ⚠️ **HIGH** - 不建議在修復前開啟Live Trading