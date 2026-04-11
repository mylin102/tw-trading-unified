🚀 量化交易策略開發與 AI 審查指南
本指南涵蓋了從 GitHub 資源獲取、選擇權核心概念到使用 AI (如 Codex/Copilot) 進行策略審查的完整流程。
📂 1. GitHub 高品質資源匯總
期貨交易 (Futures)
PyTrendFollow: 系統化趨勢跟隨策略，支援合約自動捲展。
Freqtrade: 功能強大的開源交易機器人，適合回測與實盤。
Machine Learning for Trading: 專業級機器人與 AI 策略實作教學。
選擇權定價與回測 (Options)
QuantLib-Python: 金融工程業界標準庫，用於精確計價與希臘字母計算。
Opstrat: 策略盈虧視覺化工具，快速畫出損益圖。
Lean (QuantConnect): 支援多腳選擇權（Multi-leg）的專業回測引擎。
💡 2. 選擇權核心概念速查 (Moneyness)
術語	定義	對買權 (Call) 影響	對賣權 (Put) 影響
ATM (平價)	履約價 ≈ 現價	時間價值最高，對波動敏感	同買權
OTM (價外)	無內含價值	履約價 > 現價 (便宜/高槓桿)	履約價 < 現價
ITM (價內)	具有內含價值	履約價 < 現價 (貴/連動高)	履約價 > 現價
🛠️ 3. 信用價差與進階策略
信用價差 (Credit Spread)
核心：賣高買低，賺取淨權利金（Net Credit）。
類型：
Bear Call Spread: 看空或盤整（賣低買高 Call）。
Bull Put Spread: 看多或盤整（賣高買低 Put）。
鐵蝴蝶策略 (Iron Condor)
組合：同時持有 Bear Call + Bull Put。
目標：預期股價在區間內震盪（橫盤）。
獲利來源：Theta (時間衰減) 與 IV (隱含波動率) 下降。
🔍 4. 使用 AI (Codex/Copilot) 進行策略審查
當您將 Python 策略提交給 AI 審查時，請專注於以下四大維度：
A. 邏輯缺陷檢查
前視偏差 (Look-ahead Bias)：檢查是否使用了「未來數據」計算信號。
存活者偏差 (Survivorship Bias)：檢查數據源是否包含已下市標的。
B. 效能優化 (Performance)
向量化 (Vectorization)：將 for 迴圈改為 Pandas/NumPy 向量運算。
內存管理：優化大型時間序列數據的讀取方式。
C. 選擇權專項風險
Greeks 監控：審查代碼是否具備 Delta 中性調整或 Vega 風險預警。
滑價模擬：檢查回測是否考慮了 OTM 合約極大的 Bid-Ask Spread。
D. 推薦 Prompt 範例
"這是我的一個 Python 鐵蝴蝶策略。請幫我檢查是否有前視偏差，優化信號產生的計算效率，並建議在遇到隱含波動率暴增時的防禦性邏輯。"
📅 5. 後續行動建議
安裝工具：pip install QuantLib opstrat pandas。
單元測試：要求 AI 為策略中的「信號產生模組」撰寫 Unit Test。
視覺化驗證：先用 opstrat 確認策略損益區間符合預期。
