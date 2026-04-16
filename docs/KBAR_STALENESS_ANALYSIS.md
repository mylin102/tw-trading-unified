# Shioaji K-Bar 斷線/停滯 — 根因分析與修復方案

## 問題

期貨 K-bar 資料常停滯 27 分鐘以上，導致交易無法執行。

## Shioaji 官方文檔確認的事實

來源：https://sinotrade.github.io/tutor/callback/event_cb/

| 機制 | 說明 |
|------|------|
| 自動重連 | API 預設最多重連 **50 次**（Solace broker） |
| Event Code 12 | `RECONNECTING_NOTICE` — 連線斷了，開始重連 |
| Event Code 13 | `RECONNECTED_NOTICE` — 重連成功 |
| Event Code 16 | `SUBSCRIPTION_OK` — 訂閱成功確認 |
| Event Code 20 | 重連後收到 "unknown publisher flow name" — GD flow 恢復失敗 |
| 官方最佳實踐 | **"Best way is keep your network connection alive."** |

### 官方建議的監控方式

```python
@api.quote.on_event(event_code, event)
def event_cb(code, event):
    if code == 12:
        print("⚠️ 斷線中，開始重連...")
    elif code == 13:
        print("✅ 重連成功")
    elif code == 16:
        print("✅ 訂閱成功")
    elif code == 20:
        print("❌ 重連後 GD flow 失敗，需重新訂閱")
```

## 我們目前的程式碼

### ✅ 已經有的機制

| 機制 | 位置 | 說明 |
|------|------|------|
| `api_is_healthy()` | `main.py:132` | 每 5 分鐘檢查 `list_positions` |
| 資料停滯偵測 | `main.py:270-285` | 5 分警告、10 分重啟 |
| 二次確認防誤判 | `main.py` | 不立即重啟，先等 5 分鐘 |
| 自動重啟 (5次) | `main.py:212-260` | Thread 死了會重建 |
| TMF staleness check | `monitor.py:372` | 2 分鐘沒 tick 就嘗試 rollover |
| kbar 頻率限制 | `monitor.py:1053` | 每 2 分鐘才 fetch 一次 |

### ❌ 沒有的（缺口）

| 缺口 | 影響 |
|------|------|
| **沒有註冊 `@api.quote.on_event` 回呼** | 斷線/重連完全不知道發生什麼事 |
| **不知道是斷線還是市場沒成交** | 27 分鐘停滯可能是 TMF 真的沒成交，不是斷線 |
| **沒有主動 ping/keepalive** | 官方說 "keep connection alive"，但我們沒做 |
| **沒有 fallback 資料來源** | `api.kbars()` 是唯一來源，失敗就全盲 |
| **沒有重連後重新訂閱** | 即使重連成功，subscription 可能已經失效 |

## 根因分析：為什麼會停滯 27 分鐘？

### 可能原因 A：TMF 市場真的沒成交（最常見）

- 台指期在 **08:45-08:48**（開盤前）、**13:25-13:45**（日盤收盤→夜盤開盤之間）、**14:00-14:10** 等時段，流動性極低
- `api.kbars()` 返回空資料或舊資料
- 今天期貨日盤被 volume filter 擋了 77 次（vol=126, avg=742），本身就沒成交

### 可能原因 B：Shioaji WebSocket 斷線但沒重連

- 沒有 `on_event` 回呼，我們不知道斷線是否發生
- 重連 50 次是 Solace 層面，但 subscription 狀態不保證恢復
- Event code 20 (unknown publisher flow) 需要手動重新訂閱

### 可能原因 C：`api.kbars()` 本身的限制

- `api.kbars(contract, start=date_str, end=date_str)` 是一次性查詢，不是 streaming
- 如果當天資料還沒推送到伺服器，返回的是舊資料
- 沒有 timeout 機制，可能 hang 住

## 修復方案

### 方案 1：加 Event Callback（最優先，30 分鐘）

```python
# main.py — 在啟動時註冊
@api.quote.on_event
def event_cb(event_code, event):
    if event_code == 12:
        console.print("[yellow]⚠️ Shioaji 斷線，開始重連...[/yellow]")
        global _connection_dropped
        _connection_dropped = True
    elif event_code == 13:
        console.print("[green]✅ Shioaji 重連成功[/green]")
        _connection_dropped = False
        # 重新訂閱
        if fm.contract:
            api.quote.subscribe(fm.contract, quote_type='tick')
    elif event_code == 20:
        console.print("[red]❌ GD flow 失敗，需重新訂閱所有 contract[/red]")
        _resubscribe_all()
```

### 方案 2：加 Keepalive Ping（每 60 秒）

```python
# 定期呼叫 snapshots 保持連線
def keepalive_loop():
    while True:
        try:
            api.snapshots([api.Contracts.Futures.TMF[0]])
        except Exception:
            pass
        time.sleep(60)
```

### 方案 3：K-bar fetch fallback 鏈

```python
def _fetch_today_kbars_safe(self):
    # 1. 嘗試 api.kbars()
    # 2. 如果回傳空，改用 tick 累積重建
    # 3. 如果 tick 也停了，改用 1d kbar + 插值
    # 4. 如果全失敗，return None（策略層會跳過）
```

### 方案 4：區分「市場沒成交」vs「真的斷線」

```python
# 檢查是否為非交易時段
def is_market_active():
    now = datetime.now()
    # 日盤 08:45-13:45
    day_active = (now.hour == 8 and now.minute >= 45) or \
                 (9 <= now.hour < 13) or \
                 (now.hour == 13 and now.minute <= 45)
    # 夜盤 15:00-05:00
    night_active = now.hour >= 15 or now.hour < 5
    return day_active or night_active

# 只有 market_active 且沒資料才報錯
```

## 建議優先順序

| 優先 | 方案 | 預期效果 | 工時 |
|------|------|----------|------|
| P0 | 加 `on_event` callback | 知道到底是不是斷線 | 15 min |
| P1 | 區分市場活躍/非活躍 | 避免誤報 | 10 min |
| P2 | 重連後自動重新訂閱 | 恢復資料流 | 20 min |
| P3 | kbar fallback 鏈 | 降低完全盲的風險 | 30 min |

## 外部參考

- Shioaji Event Callback 官方文件：https://sinotrade.github.io/tutor/callback/event_cb/
- Shioaji Login 文件：https://sinotrade.github.io/tutor/login/
- Shioaji GitHub：https://github.com/Sinotrade/Shioaji
- Shioaji 論壇搜尋 "斷線"：https://forum.goodinfo.tw/c/shioaji/
