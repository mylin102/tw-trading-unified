# ADR-027: Post-Release Time-Stop for Naked Single-Leg Exposure (CLOSED — H0)

- **Status**: CLOSED (2026-11-18) — 研究結案,H0 成立,不實作。見 §8。
- **動機來源**: 使用者提供的美股 VWAP 日內策略規格書(`~/taishin-stock/VWAP_day.md`)
  §3.4/§4.4 的 `force_close_time` 概念。
- **本文件第一版(已修正)誤判範圍,見 §0 修正說明。**
- **This ADR defines target design only. No runtime change — closed as H0, never reached implementation.**

---

## 0. 修正說明(第一版的錯誤)

第一版把 `force_close_time` 套用到**整個 MTS 系統**(含雙腿 SPREAD phase),
理由是「跟 taishin-stock 一樣缺少收盤強制平倉」。**這個前提是錯的**,使用者
當場糾正:

> MTS 因為是雙腿,所以不需要刻意強制當沖,而是允許留倉等待價差擴大。
> 剩下單腿時反而可以考慮強制當日平倉。

這跟 `ADR-006`(2026-05-14,Accepted)自己寫的風險分解完全一致,不是新論點:

> After release, the remaining leg becomes a **naked directional futures
> position** protected only by a trailing stop. **This is the single
> largest risk in the strategy.**
>
> Missing protections (deferred): **Post-release max hold bars (time-stop)**

也就是說:
- **SPREAD phase(雙腿都開著)**:兩腿互為避險,留倉等價差擴大是**刻意設計**,
  不是缺口,**本 ADR 不動這段**。
- **SINGLE_LEG phase(已 release,只剩一腿裸曝險)**:ADR-006 自己六個月前
  就列為「已知風險、缺乏保護」,`VWAP_day.md` 的 `force_close_time` 概念
  在這裡才有正確的對應——限制裸方向性曝險的持有時間,不是限制避險價差的
  持有時間。

本文件第一版關於「重用 ADR-024 COMBINED_EXIT 路徑」的部分**整段作廢**——
那是雙腿同時平倉的機制,不適用於這裡(這裡只有一腿,是單腿出場,不是雙腿)。

---

## 1. Context(唯讀事實)

### 1.1 ADR-006 的既有風險分解(本 ADR 的唯一前提來源)

```
Phase 1 Entry  : 雙腿,近月多/遠月空(或反向),方向由 spread_z 符號決定
Phase 2 Release: 輸的那腿碰到 release_stop_points(預設 20pt)先停損出場
Phase 3 Trail  : 剩下那腿變成「裸方向性期貨部位」,只靠 trailing stop 保護
```

ADR-006 的原話:「Phase 3 converts to directional exposure」是「the single
largest risk in the strategy」,且明確列出「Missing protections (deferred)」
第一項就是「Post-release max_hold_bars: Time-stop on naked directional leg
after release」——**六個月前就知道、就寫下來,但從未實作**。

### 1.2 為什麼 SPREAD phase 不需要這個機制

雙腿互為避險(近月多+遠月空,或反向),部位本身對台指期的方向性風險是
大幅抵銷的,風險集中在**價差本身**的波動,不是期貨價格的隔夜跳空。
留倉等價差擴大是策略的核心邏輯,不是意外留下的倉位——跟 taishin-stock
單腿當沖的「使用者決定留倉過夜」性質不同,但方向一致:**都是刻意決策,
不是缺口**。本 ADR 第一版把這個刻意決策誤判為缺口,是分析錯誤。

### 1.3 為什麼 SINGLE_LEG phase 不一樣

Release 之後,避險的另一腿已經出場,剩下的部位是**純方向性期貨曝險**,
跟直接裸做多/裸做空台指期沒有本質差異——隔夜跳空、無限期持有的風險
完全沒有被抵銷。這正是 `VWAP_day.md` 的 `force_close_time`(美股日內
策略不留倉的理由:隔夜跳空風險)對應到的情境。

---

## 2. Problem(研究問題)

**H1**:SINGLE_LEG phase(release 後裸方向性曝險)若加上時間停損
(post-release max_hold_bars 或明確的收盤強制平倉),能在不大幅犧牲
trailing stop 本來能捕捉到的順勢獲利的前提下,降低隔夜跳空風險與
無限期持有的尾部風險。

**H0**:trailing stop 本身已經足夠保護,時間停損只會提早砍掉本來會
繼續順勢的部位,淨結果更差(ADR-006 §Future Research Topics 第 4 項
「Volatility-scaled trail」也在排隊,時間停損可能跟它的角色重疊或衝突,
需要先確認兩者關係,不要同時引入兩個互相干擾的新機制)。

---

## 3. 設計(Target State,尚未實作)

### 3.1 兩個可能的實作形式(先用回測決定選哪個,不先假設)

**形式 A — 固定根數時間停損**(ADR-006 原文用語:`max_hold_bars`):
release 後,若持有超過 N 根 bar(或 N 分鐘)仍未觸及 trailing stop,
強制出場,不管當時盈虧。

**形式 B — 收盤強制平倉**(VWAP_day.md 的 `force_close_time` 概念):
release 後的裸腿,到了當天(或當個 session)收盤前,無條件市價平倉,
不留倉過夜——但盤中沒有固定根數限制,trailing stop 仍是主要出場機制。

這兩者不是互斥的,也可以是 A 為主、B 為最後防線(例如 A 設 60 根 bar
但如果 60 根 bar 還沒到就先碰到收盤,B 先生效)。**哪一種(或兩者的組合)
比較好,要先用 ADR-006 既有的回測資料跑數字,不能憑直覺選。**

### 3.2 範圍(只動 SINGLE_LEG phase 的出場判斷)

```
只在 phase == "SINGLE_LEG" 且 release 已發生時生效
不影響 SPREAD phase 的任何邏輯(entry、release 觸發條件全部不動)
不影響 Policy J(COMBINED_EXIT,那是 SPREAD phase 的機制,跟這裡無關)
```

這跟第一版相比範圍小得多——只新增一個「SINGLE_LEG 出場判斷」的額外條件,
不涉及雙腿平倉、不重用也不修改 ADR-024 的 COMBINED_EXIT 路徑。

### 3.3 Dry-run 先行

同樣先用 observe 模式:記錄「如果現在有這個時間停損,會不會觸發、觸發點
跟 trailing stop 實際觸發點的時間差」,不先送單。

---

## 4. 驗收測試矩陣(VERIFY)

| # | 情境 | 預期行為 |
|---|---|---|
| 1 | SPREAD phase,任何時間 | 完全不受影響(這是本 ADR 最重要的回歸測試——證明沒有不小心碰到雙腿留倉邏輯) |
| 2 | SINGLE_LEG phase,持有時間超過時間停損門檻,trailing stop 未觸發 | 強制出場 |
| 3 | SINGLE_LEG phase,trailing stop 先觸發 | 照舊,時間停損不介入 |
| 4 | Release 剛發生,phase 從 SPREAD 轉 SINGLE_LEG 那一刻 | 時間停損計時器正確從這一刻起算,不是從 entry 起算 |
| 5 | observe 模式 | 只記錄,不送單 |
| 6 | Mutation:拿掉時間停損判斷 | 對應測試必須精準失敗 |

---

## 5. 範圍與限制

**本 ADR 涉及檔案**(若進入實作階段,待確認後才知道精確清單):
- `strategies/plugins/futures/active/tmf_spread.py`(SINGLE_LEG phase 出場判斷)
- 對應測試檔(不計入門檻)

**明確不做**:
- 不動 SPREAD phase 任何邏輯
- 不動 Policy J / COMBINED_EXIT(ADR-024,跟這裡無關)
- 不碰 taishin-stock
- 不同時引入「固定根數時間停損」跟「波動度調整 trailing stop」兩個新機制
  (ADR-006 §Future Research Topics 第 1 項跟第 4 項),一次只驗證一個變數

---

## 6. Decision

- **Evidence Level**: E0
- **Recommended next step**:
  1. 用 ADR-006 已經提到的回測資料(或補抓更長區間),量測 SINGLE_LEG phase
     的實際持有時間分布——現在到底有多少部位、持有多久、trailing stop
     平均在多久後觸發,先搞清楚現狀規模,不要假設問題很嚴重就直接動手
  2. 在同一份資料上做反事實:如果加一個時間停損(先測幾個候選門檻值,
     比照 R-005 的多重比較紀律,預先指定一組主配置,其餘標敏感度),
     SINGLE_LEG phase 的淨 PnL/MDD 會怎麼變
  3. 回測顯示有改善才進 observe 模式;沒有改善就標記 H0 成立,結案
- **Impact on strategy**: 無(回測階段對任何現有邏輯零影響)

## 7. Failure Modes / Open Questions

- 需要先查 SINGLE_LEG phase 目前的實際樣本量——如果 release 本身很少發生
  (多數部位停在 SPREAD phase 就平倉),這整個問題可能樣本不足,量測不出
  有意義的結論。
- 時間停損跟 ADR-006 排隊中的「Volatility-scaled trail」角色可能重疊,
  需要先確認兩者如果都做,是互補還是衝突。
- 「收盤強制平倉」版本(形式 B)需要查清楚 SINGLE_LEG phase 的部位在
  收盤時间點的實際流動性——跟第一版 ADR-027 查過的「緩衝時間夠不夠成交」
  是同一類問題,但現在只需要對單腿期貨查,不是雙腿。

---

## 8. 結論(2026-11-18,§6 步驟 1 執行結果 → H0 成立,結案)

**資料來源**:正式 PAPER ledger `tw-trading-unified-runtime/paper/logs/
mts_spread_events.jsonl`(真實資料,非回測模擬)。

**EXIT_LOG 出場原因分布**(總計 1,529 筆):

```
COMBINED_EXIT(Policy J 雙腿合併出場)   1,403 筆  91.8%
TRAIL(release 後移動停利)                119 筆   7.8%
RELEASE_STOP(release 停損)                 7 筆   0.5%
```

SINGLE_LEG phase 真實樣本量 = 126 筆(TRAIL + RELEASE_STOP),**樣本充足**,
推翻 §7 原本「樣本可能不足」的疑慮。

**SINGLE_LEG phase 持有時間分布**(從 RELEASE_NEAR/FAR_SUBMITTED 到最終
EXIT_LOG,125/126 筆配對成功):

```
min:     0.0 分
p25:     0.2 分
median:  4.0 分
p75:     9.7 分
p90:    30.0 分
max:   329.9 分(約 5.5 小時,樣本中最長的單筆,仍未到隔夜)
mean:   13.9 分
```

**判定:H0 成立。**

1. 中位數僅 4 分鐘、p90 僅 30 分鐘——trailing stop 已經讓絕大多數
   SINGLE_LEG phase 很快結束,不是長時間裸曝險。
2. 樣本中最長的一筆(329.9 分,約 5.5 小時)仍遠遠稱不上「隔夜留倉」——
   §1.3 原本設想的「跟直接裸做多/裸做空沒有本質差異、隔夜跳空風險完全
   沒被抵銷」的極端情境,在目前的 trailing stop 參數下**沒有觀測到任何
   一筆真正跨夜的案例**。
3. ADR-006 六個月前標記的「Missing protections (deferred): post-release
   max_hold_bars」確實是個已知理論風險,但**實測資料顯示現有 trailing
   stop 機制已經把這個風險控制在可接受範圍內**,沒有證據支持額外加一層
   時間停損會帶來淨改善——反而可能像 H0 擔心的那樣,提早砍掉本來會
   繼續走的部位。

**本 ADR 到此結案,不進入 §3 設計實作、不進 observe 模式。** 若未來
trailing stop 參數改變(例如 `trail_distance_points` 放大)或市場波動度
結構性改變,這個分布可能需要重新量測——不是一次性結論,是「在目前參數
下成立」的結論。

**本 ADR 的連帶發現(不在此 ADR 範圍內,已記錄供日後參考)**:
- `core/order_management/order_manager.py` 的 `_order_id_audit_path()`
  路徑 bug(已修復,見 git commit,獨立於本 ADR 的研究問題)
- 原先懷疑的「事件 ledger 10/3 起靜默停寫 6 週」是調查過程中的路徑解析
  錯誤(漏算 PAPER 模式 `paper/` 前綴),**不是真實存在的問題**,已撤回
