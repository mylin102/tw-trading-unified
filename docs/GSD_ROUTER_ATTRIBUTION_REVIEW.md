我看過你這份 GSD 計畫了，整體品質其實已經是「可以直接進 implementation」的等級，而且方向是對的。下面我幫你做的是**工程審查 + 少量修正建議**，讓你上線時不會卡住。

我會聚焦在「會踩雷的地方」，不是重複你已經寫對的部分。

---

# 🔎 核心評價（先講結論）

這份設計：

* 架構：✅ 正確（router + attribution 解耦）
* 分階段：✅ 合理（Phase 1 → 2 → 3）
* V-model：✅ 有效（不是裝飾）
* 風險控制：⚠️ 有兩個點需要修

👉 **最關鍵問題只有兩個：**

1. `candidate_count` 定義會錯（目前設計會失真）
2. CSV flush 策略會在 production 爆 I/O 或丟資料

---

# ⚠️ 問題 1：candidate_count 定義錯誤（很重要）

你現在寫：

> candidate_count = router_df 裡該 strategy 的 row 數

這在現在的 logging 模式 **不一定正確**。

因為：

```text
[counter_vwap, spring_upthrust, kbar_feature]
```

如果第一個就 winner：

* counter_vwap → logged
* spring_upthrust → shadowed
* kbar_feature → shadowed

👉 OK，三筆都有 → candidate_count 正確

但如果你未來優化 logging（例如只記 evaluated + winner）：

👉 candidate_count 會變少 → starvation 計算錯

---

## ✅ 建議修法（簡單且正確）

**candidate_count 應該定義為：**

> 出現在 candidate list 的次數（不是 router_df row 數）

所以你要確保：

👉 **每個 candidate 都要有一筆 row（即使 shadowed）**

你現在設計其實有做到，但建議你：

### 在 spec 補一句（很重要）

```md
candidate_count is defined as the number of times a strategy appears in the candidate list,
which must be explicitly logged, including shadowed strategies.
```

✔ 這句是避免未來 refactor 把 attribution 搞壞

---

# ⚠️ 問題 2：CSV flush 設計會出事

你寫：

> 每 N bars 或每秒 flush

這個在實戰會遇到兩種問題：

---

## 問題 A：高頻時 I/O 爆掉

如果你是：

* futures tick / 1s bar
* 多商品（TX / TMF）

👉 每秒寫 CSV = 很快變 bottleneck

---

## 問題 B：crash 時資料全沒

如果你改成：

* 每 1000 bars flush

👉 crash → 全部沒寫

---

## ✅ 建議（production 等級）

用「雙模式」：

### Mode 1：buffer（預設）

```python
if len(router_rows) >= 500:
    flush()
```

### Mode 2：time-based fallback

```python
if now - last_flush >= 10 sec:
    flush()
```

### Mode 3：shutdown hook（超重要）

```python
atexit.register(recorder.export_csv)
```

---

## 👉 最佳實務一句話

> **size-based + time-based + shutdown flush 三重保護**

---

# 🔧 Phase 1（router integration）我會加一個小優化

你現在寫：

```text
每個 candidate 評估點記錄
```

我會建議加一個 helper（避免 router code 爆炸）：

---

## 建議加入 helper

```python
def log_router_event(
    recorder,
    ts,
    symbol,
    regime,
    name,
    order,
    status,
    evaluated,
    winner=False,
    signal=None,
    note=""
):
    if recorder is None:
        return

    recorder.log_router_row(
        timestamp=ts,
        symbol=symbol,
        regime=regime,
        strategy_name=name,
        candidate_order=order,
        status=status,
        evaluated=evaluated,
        winner=winner,
        signal_side=getattr(signal, "side", None),
        signal_type=getattr(signal, "type", None),
        notes=note,
    )
```

👉 router 裡只寫：

```python
log_router_event(...)
```

這會讓 router 邏輯乾淨很多。

---

# 🔍 Phase 2（report）我建議加一個 killer feature

你現在做：

* starvation
* winner_count

很好

但**還差一個會直接讓你找到 alpha 的東西**

---

## ⭐ 建議加：Priority Impact Score

定義：

```text
priority_impact = shadowed_count / winner_count
```

解讀：

| 值 | 意義          |
| - | ----------- |
| 高 | 被壓很多（應該往前排） |
| 低 | 已經優先（或沒機會）  |

---

## 實戰用途

你會看到：

```text
kbar_feature:
  eval=60
  winner=18
  shadowed=300
```

👉 priority_impact = 16.6

這代表：

> 這個策略可能很好，但幾乎沒機會

---

# 🧠 Phase 2（report）再補一個

## 加一個：Regime Efficiency Matrix

```text
strategy × regime → pnl
```

你會得到：

```text
              TREND   WEAK   STRETCHED
counter_vwap   -20     80      200
kbar_feature    50     120      30
```

👉 這直接告訴你：

* 哪個策略該在哪個 regime 被優先

---

# 📄 Phase 3（文件同步）

這部分你寫得很好，我只補一個：

---

## ❗不要刪 supports_regime 段落

你現在寫：

> 移除 `strategy.supports_regime()`

我會建議：

👉 **不要刪，改成「optional / future enhancement」**

因為：

* 你之後一定會需要
* attribution 做完你會想加回來

---

# 🧭 我幫你重寫一版關鍵段落（可直接貼）

你可以把這段放進 spec：

```md
## Attribution Logging Invariant

For each bar:

- All candidate strategies MUST be logged
- Exactly one strategy MAY be marked as winner
- All remaining candidates MUST be marked as shadowed

This invariant ensures:

- correct candidate_count
- accurate starvation_index
- reproducible router behavior
```

---

# 🏁 最後結論

這份 GSD：

👉 已經可以進實作
👉 但要做這 4 個小修正

---

## ✅ 必改（真的重要）

1. 明確定義 `candidate_count`
2. CSV flush 改為 size + time + shutdown

---

## ⭐ 強烈建議（會讓你多賺錢）

3. 加 `priority_impact`
4. 加 regime × strategy pnl matrix

---

## 🧩 可延後

5. shadow replay
6. supports_regime（但先留接口）



