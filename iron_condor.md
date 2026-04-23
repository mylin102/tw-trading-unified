這是一份針對 台指期 (TX/MTX) 環境設計的 Iron Condor (鐵鷹式策略) 實戰指南。
由於台指期貨具有「同月份自動平倉」的特性，這份文件將說明如何正確使用台指選擇權 (TXO) 來建構此策略，並結合你現有的 VWAP 偏離 指標進行進場優化。
------------------------------
## 🦅 台指期 Iron Condor (鐵鷹) 策略說明書## 1. 策略核心觀念
Iron Condor 是一種「非方向性」的選擇權策略，旨在賺取時間價值 (Theta)。

* 預期：指數在結算前會維持在特定區間內震盪。
* 獲利來源：權利金的時間衰減 (Time Decay)。
* 適用環境：低波動率、盤整盤、或股價回歸 VWAP 中心時。

------------------------------
## 2. 策略結構 (Four-Legged Structure)
在同一個到期月份（例如 6 月台指選），同時建立兩組「信用價差」：
## 上方：看空價差 (Bear Call Spread)

* 賣出 (Sell)：較近的價外 Call (例如 Current + 500) $\rightarrow$ 收錢
* 買入 (Buy)：較遠的價外 Call (例如 Current + 600) $\rightarrow$ 付錢(保險)

## 下方：看多價差 (Bull Put Spread)

* 賣出 (Sell)：較近的價外 Put (例如 Current - 500) $\rightarrow$ 收錢
* 買入 (Buy)：較遠的價外 Put (例如 Current - 600) $\rightarrow$ 付錢(保險)

💡 關鍵： 因為四隻腳的履約價都不同，所以不會像期貨一樣被自動平倉抵銷。

------------------------------
## 3. 結合 VWAP 偏離進場 (量化邏輯)
利用你手上的資料進行進場過濾，提高勝率：

| 指標狀態 | 動作 | 意義 |
|---|---|---|
| 價格 > VWAP + 2σ | 建構上方 Call Spread | 價格過熱，預期回歸中心，增加上方壓力。 |
| 價格 < VWAP - 2σ | 建構下方 Put Spread | 價格超跌，預期有撐，增加下方支撐。 |
| 價格在 VWAP 附近 | 雙邊同時建構 | 典型的 Iron Condor，預期今日為悶盤。 |

------------------------------
## 4. 費用與損益計算 (永豐 Shioaji 成本參考)

* 單邊腳手續費：20 (Broker) + 5*2 (Exchange) = 30 元。
* 整組 IC 成本：$30 \times 4 = 120$ 元 (不含稅)。
* 最大獲利：收取的總權利金淨額。
* 最大虧損：$(價差間距 \times 50) - 收取的權利金$。

------------------------------
## 5. Shioaji Python 實作範例
使用 OptionComplexOrder 進行下單，以確保保證金折抵並防止分開成交。

import shioaji as sjfrom shioaji.constant import Action, OptionCode, OrderType
# 1. 定義合約 (以 6 月為例，假設建構 20400/20500 鐵鷹)# 註：合約代碼需依當時市場代碼為準leg1 = api.Contracts.Options.TXO.TXO20400F4  # Sell Callleg2 = api.Contracts.Options.TXO.TXO20500F4  # Buy Callleg3 = api.Contracts.Options.TXO.TXO19600F4  # Sell Putleg4 = api.Contracts.Options.TXO.TXO19500F4  # Buy Put
# 2. 建立複合訂單 (以 Call Spread 為例)order = sj.Order(
    price=15,               # 兩口之間的點數差
    quantity=1,
    action=Action.Sell,     # 賣出價差
    price_type=OrderType.LMT,
    order_type=sj.constant.TFTOrderType.ROD
)
# 3. 下單 (需確認永豐 API 對於四隻腳組合的支援格式)# 通常建議拆成 Call Spread 與 Put Spread 兩組兩隻腳的下單

------------------------------
## 6. 風險控管與注意事項 (Theta Gang 必讀)

   1. IV Crush：在重大事件（如法說會）前 IV 高，權利金肥；事件後 IV 掉，利於賣方。
   2. 倒貨風險 (Gap Down)：台指期夜盤若跳空 200 點，可能會直接擊穿你的 Put Side，需設定 API 自動監控 Delta 曝險。
   3. 流動性：請選擇「月選（TXO）」而非「週選（W1/W2）」，除非你追求極高的時間衰減速度，但週選跳空風險極大。

------------------------------
## 7. 總結

* 不要用純期貨做 Iron Condor，因為會被平倉。
* 使用同月份、不同履約價的選擇權。
* 利用 VWAP 偏離度 當作你的進場導引，在極端偏離時賣出反向權利金。


