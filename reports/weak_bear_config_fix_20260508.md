# weak_bear_trend 配置載入修復記錄

## 問題發現

**時間**: 2026-05-08 03:08
**現象**: Dashboard 顯示 `ADX_TOO_HIGH`，即使配置文件設置 `max_adx: 35.0`

## 根本原因

**代碼問題**:
```python
# 錯誤的代碼 (第 57 行和第 99 行)
params = context.config.get("params", {})  # ❌ 路徑錯誤
```

**實際配置結構**:
```yaml
# config/futures_night.yaml
strategy:
  params:  # ← params 在 strategy 區塊內
    max_adx: 35.0
    ...
```

**影響**:
- 策略使用硬編碼 default 值 `max_adx: 20.0`
- 配置文件中的 `35.0` 未被讀取
- 錯過 WEAK+SHORT 進場機會 (ADX 20-35 區間)

## 修復方案

**修改文件**: `strategies/plugins/futures/weak_bear_trend.py`

**修改位置**: 2 處

### 修改 1: init() 方法 (第 57 行)

```python
# 修改前
params = context.config.get("params", {})

# 修改後
params = context.config.get("strategy", {}).get("params", {})
```

### 修改 2: on_bar() fallback (第 99 行)

```python
# 修改前
params = context.config.get("params", {}) if context.config else {}

# 修改後
params = context.config.get("strategy", {}).get("params", {}) if context.config else {}
```

### 附加修改: default 值更新

```python
# max_adx default 從 20.0/22.0 更新為 35.0
self.max_adx = params.get("max_adx", 35.0)  # 配置優先，default 35.0
```

## 驗證

**驗證工具**: `scripts/verify_weak_bear_config_load.py`

**驗證結果**:
```
✅ 策略內部的 max_adx = 35.0
✅ 配置文件中的 max_adx = 35.0
✅ 所有參數正確載入:
   - stop_atr_mult: 1.0
   - take_profit_atr_mult: 2.0
   - min_mom_velo_bearish: -8.0
   - max_vwap_dist_atr: 0.5
```

## 部署

**重啟命令**:
```bash
pm2 restart trading-system --update-env
```

**PM2 狀態**:
- trading-system: ✅ 在線 (PID 42291)
- dashboard: ✅ 在線 (PID 40169)

## 預期效果

**修改前**:
- ADX < 20: 進場
- ADX 20-35: ❌ 被阻止 (ADX_TOO_HIGH)
- ADX > 35: 被阻止

**修改後**:
- ADX < 35: ✅ 進場 (如果其他條件滿足)
- ADX > 35: 被阻止

## 後續監控

**Dashboard**: http://localhost:8500
**監控面板**: 左側邊欄 → "📊 auto_select 監控中心"

**觀察指標**:
- weak_bear_trend 是否不再顯示 `ADX_TOO_HIGH`
- ADX 20-35 區間是否有進場信號
- 勝率與盈虧比變化

---

**修復完成時間**: 2026-05-08 03:16
**修復狀態**: ✅ 已驗證並部署
**下次檢查**: 訪問 Dashboard 查看實時表現
