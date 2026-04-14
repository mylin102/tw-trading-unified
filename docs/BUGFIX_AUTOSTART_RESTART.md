# BUGFIX: 監控無法自動重啟 (Autostart Restart Failure)

**日期**: 2026-04-14
**影響**: 期貨/選擇權監控退出後無法自動重啟，夜盤設定檔未套用
**嚴重度**: HIGH — 交易停擺無警報

---

## 現象

1. `main.py` 退出後，Layer 6 迴圈沒有重啟
2. `pkill main.py` 後無新進程接替
3. 日誌停滯 5+ 分鐘

---

## 根本原因 (3 個獨立 Bug)

### Bug 1: `is_night_session()` 缺 `dt` 參數 → 啟動即 Crash

**檔案**: `main.py:248, 322`

```python
# ❌ 錯誤寫法 — 缺 dt 參數，Type Error 立即 crash
_config_file = "futures_night.yaml" if is_night_session() else "futures.yaml"

# ✅ 正確寫法 — 傳入 datetime.now()
from core.date_utils import is_night_session
from datetime import datetime as _dt
_is_night = is_night_session(_dt.now())
_config_file = "futures_night.yaml" if _is_night else "futures.yaml"
```

**日誌證據**:
```
Critical crash: is_night_session() missing 1 required positional argument: 'dt'
```

### Bug 2: 兩個 `autostart.sh` 實例同時運行 → 資源競爭

**根因**: 重複執行 `bash autostart.sh`，舊實例沒有被清理

```
PID 31859 (09:35啟動) → Layer 6 → main.py PID 73867
PID 47106 (14:50啟動) → Layer 6 → main.py PID 73472
```

兩者共用同一個 `unified.log`，導致：
- 兩個 `tee` 程序寫入同一檔案
- Layer 7 健康檢查看到 2 個 PID 以為正常
- 實際上只有一個實例能成功訂閱 Shioaji API

### Bug 3: Layer 6 cooldown + 遺留 cooldown 檔案 → 重啟被阻斷

**檔案**: `autostart.sh` — `record_crash()` 函數

```bash
record_crash() {
    local recent_crashes=$(awk -v cutoff="$one_hour_ago" '$1 >= cutoff && $2 == "futures"' "$CRASH_LOG" | wc -l)
    if [ "$recent_crashes" -ge "$MAX_CRASHES_PER_HOUR" ]; then
        sleep 600  # 冷卻 10 分鐘!!!
    fi
}
```

**問題**: 
- `record_crash()` 每次 `main.py` 退出都會記錄
- crash 3 次/小時 → 睡眠 600 秒 → 期間 Layer 6 完全不嘗試重啟
- `/tmp/trading_crash_count` 和 `logs/crash_tracker.log` 是遺留狀態，重啟 autostart 時沒清除
- `sleep 600` 是阻塞式的，期間整個 Layer 6 迴圈停擺

**Exponential backoff 放大問題**:
```bash
SLEEP_TIME=$(( BASE_SLEEP * (2 ** (RETRY_COUNT > 5 ? 5 : RETRY_COUNT)) ))
# RETRY_COUNT=5 → 15 * 32 = 480s (8 分鐘)
# RETRY_COUNT=6+ → 600s (10 分鐘上限)
```

兩個機制疊加 → 最長可能 600 + 600 + 480 = **28 分鐘**無重啟嘗試。

---

## 修復 (已提交 2 個 commits)

| Commit | 修復 | 檔案 |
|--------|------|------|
| `d9263e9` | 日夜盤設定切換 + VWAP 30pts 過濾 | `main.py`, `monitor.py` |
| `23f39d4` | `is_night_session()` 缺 dt 參數 crash | `main.py` |

### Fix 1: `main.py` — session 感知設定載入 (兩處都要改)

**啟動路徑** (line ~247):
```python
from core.date_utils import is_night_session
from datetime import datetime as _dt
_is_night = is_night_session(_dt.now())
_config_file = "futures_night.yaml" if _is_night else "futures.yaml"
console.print(f"[dim]📋 Futures config: {_config_file}[/dim]")
```

**自動重啟路徑** (line ~318):
```python
from core.date_utils import is_night_session
from datetime import datetime as _dt2
_is_night = is_night_session(_dt2.now())
_config_file = "futures_night.yaml" if _is_night else "futures.yaml"
```

### Fix 2: `monitor.py` — VWAP 30pts 最小距離閾值

```python
# 夜盤 VWAP exit 加入雜訊過濾
vwap_distance = abs(last_price - vwap)
_min_vwap_distance = 30  # pts, round-trip friction ~8pts + buffer
if vwap_violated and vwap_distance >= _min_vwap_distance:
    self._vwap_violation_bars += 1
```

---

## 標準操作程序 (SOP)

### 安全重啟監控

```bash
# 1. 確認所有舊進程
ps aux | grep -E "autostart|main\.py" | grep -v grep

# 2. 清除所有 autostart 實例和子進程
pkill -15 -f "autostart.sh"
pkill -15 -f "python.*main.py"
sleep 7

# 3. 確認清除乾淨
ps aux | grep -E "autostart|main\.py" | grep -v grep
# 應該沒有任何輸出

# 4. 清除遺留 cooldown 狀態
rm -f /tmp/trading_crash_count /tmp/archive.lock /tmp/tw_trading_unified.lock

# 5. 啟動單一 autostart 實例
cd /Users/mylin/Documents/mylin102/tw-trading-unified
nohup bash autostart.sh > /dev/null 2>&1 &

# 6. 等待 30 秒驗證
sleep 30
ps aux | grep "python.*main.py" | grep -v grep  # 應有 1 個 PID
tail -5 logs/unified.log | grep "started (PAPER)"  # 應看到啟動訊息
grep "Futures config" logs/unified.log | tail -1   # 應顯示正確設定檔
```

### 驗證清單

| 檢查 | 指令 | 預期結果 |
|------|------|----------|
| autostart 單一實例 | `pgrep -c -f autostart.sh` | ≥ 1 |
| main.py 運行中 | `pgrep -f "python.*main.py"` | 1 個 PID |
| 設定檔正確 | `grep "Futures config" logs/unified.log \| tail -1` | `futures_night.yaml` (夜盤) |
| 無 crash | `grep "Critical crash" logs/unified.log \| tail -1` | 應是舊記錄 |
| 數據更新 | `stat -f %m logs/unified.log` | 距今 < 60 秒 |

---

## 預防措施

### 1. 測試 `is_night_session()` 時必須傳參數

```python
# ❌
is_night_session()

# ✅
is_night_session(datetime.now())
```

### 2. 啟動前只允許一個 autostart 實例

```bash
# 先確認沒有舊實例再啟動
pgrep -f "autostart.sh" | wc -l  # 應為 0
```

### 3. 不要用 `| head` 測試 main.py

```bash
# ❌ SIGPIPE 會殺死 main.py
python3 main.py | head -60

# ✅ 用完整日誌
nohup python3 main.py >> /tmp/test.log 2>&1 &
```

### 4. 定期檢查健康狀態

```bash
tail -1 logs/unified.log | grep "狀態"
stat -f "%m" logs/unified.log  # 最後更新時間
```

---

## 架構限制 (待改進)

| 問題 | 目前狀況 | 建議 |
|------|----------|------|
| 無自動重啟邏輯 | Layer 7 只檢測不重啟 | 在 Layer 7 加入主動重啟 |
| cooldown 太長 | 10 分鐘冷卻 + exponential backoff | 降低至 60s，或改為固定 backoff |
| 遺留狀態沒清理 | `/tmp/trading_crash_count` 重啟時保留 | 在 `autostart.sh` 啟動時清除 |
| 無警報機制 | 期貨停了只寫日誌 | 加入 LINE/Email 通知 |
