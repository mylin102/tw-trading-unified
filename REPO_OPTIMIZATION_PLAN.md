# tw-trading-unified Repo 優化計劃

**建立日期**: 2026-07-06
**作者**: Hermes Agent
**範圍**: 整個 repo 的健康度、可維護性、效能優化
**Branch**: `chore/repo-hygiene-p0` (與 ADR-010 runtime 修復線分離)

---

## 當前狀態總覽 (P0A baseline, 2026-07-06)

| 指標 | 數值 | 評估 |
|------|------|------|
| Python 檔案數 (不含 venv) | 575 | 過多 |
| Python 總行數 | 123,081 | 偏高 |
| 根目錄散落 .py 檔案 | ~30 test_*.py + 7 debug + 分析腳本 | ❌ 混亂 |
| 根目錄 .md 檔案 | 53 | ❌ 過多 |
| docs/ 下 .md | 144 | 偏多但尚可接受 |
| tests/ 測試 | 125 | ✅ 覆蓋度不錯 |
| 備份目錄 | backup_20260427 + 15 個 backups/* | ❌ 該 quarantine |
| ruff 錯誤 | 1,518 (862 可自動修) | ❌ 嚴重 |
|   - F821 undefined-name | 88 | 高風險, 可能真 bug |
|   - E722 bare-except | 56 | 會吞交易錯誤 |
|   - F401 unused-import | 539 | 低風險 |
|   - F841 unused-variable | 111 | 低風險 |
| .planning/ 目錄 | 71 檔案 | ❌ 廢棄工具鏈遺留 |
| ADR 數量 | 13+ (分散 docs/adr, docs/decisions, docs/adrs, docs/plans) | ⚠️ 目錄不一致 |
| 最大檔案 | monitor.py 6,117 行; ui/dashboard.py 5,285 行; live_options_squeeze_monitor.py 5,207 行 | ❌ 巨型類 |
| duplicate basename 檔案 | monitor.py×3, dashboard.py×2, order_manager.py×2, ... | ⚠️ 同名導致混淆 |

---

## 優化目標

1. **降低認知負荷**: 根目錄清空散落檔案，一目了然 repo 結構
2. **改善可測試性**: 巨型類拆解前先補 characterization tests
3. **降低 lint/型別風險**: 1,518 → < 200 (先修 F821/E722 真風險)
4. **縮減 repo 體積**: quarantine 廢棄目錄、備份、舊 ad-hoc 腳本 (不直接刪)
5. **統一治理結構**: 一致化 ADR、命名、配置、CI/CD
6. **保持交易安全**: 每次改動都跑 pytest，遵守 RULES.md One Fix Per Change 原則

---

## 執行原則 (不可妥協)

1. **One Fix Per Change (RULES.md Rule 5)**: 每個 patch 只做一項工作
2. **Test Before Deploy (RULES.md Rule 10)**: 每個 phase 結束前 `pytest tests/ -v` 全綠
3. **Code Attribution (RULES.md Rule 13)**: 每次改動加 `# 2026-MM-DD Hermes Agent:` 註解
4. **Selective Pruning**: 重構/刪除前先在 git history 留標籤 `archive/2026-07-06-pre-cleanup`
5. **3+ 檔案改動時停下來詢問** (AGENTS.md)
6. **不直接刪**: 廢棄目錄、`_fixed.py`、舊 .md → 一律 quarantine 到 `_archive/<category>/<YYYYMMDD>/` 或 `docs/archive/<YYYYMMDD>/`
7. **搬移前後 pytest baseline 比對**: `pytest --collect-only` 數量穩定才接受搬移
8. **RULES.md 絕對不移**: agent / 人類 workflow 都依賴它

---

## 修訂後的 Phase 排序

原文 P0-P4 已依使用者風險邊界調整為下列順序:

```text
P0A  repo inventory baseline
P0B  root py 搬移 (只搬不刪不改 import)
P0C  docs / ADR 歸位
P0D  ADR consolidation (建 index)
P1A  ruff F821 / E722 only (真風險優先)
P1B  pre-commit + smoke
P3A  characterization tests (為 P2 鋪路)
P2   giant file extraction (先抽 helper, 不改 control flow)
P4   README / CHANGELOG / governance
```

---

### **P0A — Repo inventory lock (baseline)**

只新增報表，不改檔:

- 產出 `docs/reports/repo_inventory_20260706.md`
- 內容:
  - Python 檔數 / 行數
  - ruff error count (含分類)
  - pytest baseline (收集數 + pass 數)
  - root `py` / `md` 完整清單
  - duplicate filename 清單
  - quarantine 與不動的 protected paths
- 之後所有 cleanup 都以這份報表為比對基準
- **驗收**: 報表寫入, 不動任何檔案

---

### **P0B — Root Python relocation**

只搬, 不刪, 不修邏輯, 不改 import path (如有相對 import 受影響, 用 git mv 保住 history):

```text
test_*.py         → tests/legacy/
_debug_kbar*.py   → scripts/debug/
debug_strikes.py  → scripts/debug/
debug_txo.py      → scripts/debug/
analyze_*.py      → scripts/analysis/
*_fixed.py        → scripts/deprecated/fixed_variants/
fixed_*.py        → scripts/deprecated/fixed_variants/
final_validation.py  → scripts/deprecated/
fixed_trading_system.py → scripts/deprecated/
```

#### 前置檢查
- `pytest --collect-only > /tmp/before_collect.txt`
- 記錄 collected tests 數量

#### 每類獨立 commit
1. test_*.py 搬 tests/legacy/ → 跑 pytest --collect-only 確認沒掉 test
2. debug_*.py → scripts/debug/
3. analyze_*.py → scripts/analysis/
4. *_fixed.py → scripts/deprecated/fixed_variants/

#### 搬完後
- `pytest --collect-only > /tmp/after_collect.txt`
- diff 比對, 若下降要判斷是預期 (ad-hoc test 落 collect-only) 還是誤傷
- **驗收**: collected 數量 diff 可解釋, 勿靜默掉 test

---

### **P0C — Docs relocation**

只搬, 不刪, 保留入口:

#### root 必留
```
README.md
RULES.md
CHANGELOG.md
AGENTS.md (agent workflow 依賴)
CLAUDE.md (Agent 上下文, 依 CLAUDE.md mandate)
CONTRIBUTING.md
INSTALL.md
TODOS.md
```

#### 分三類搬移
| 類型 | 目標位置 | 說明 |
|------|---------|------|
| 現行入口文件 | root 保留 | 上述清單 |
| 架構 / ADR / 操作指南 | `docs/` (放對 subfolder) | 仍在用的文件 |
| 過時報告 / 紀錄 | `docs/archive/reports_20260706/` | 時間戳記標明歸檔日期 |

⚠️ 不要全塞 archive: 需逐一判斷是否仍在用 (e.g. `NIGHT_SESSION_IMPROVEMENT_PLAN.md` 若還在執行 → `docs/plans/`)

#### 驗收
- root .md 數量 ≤ 9
- `docs/archive/reports_20260706/` 內每一份都標歸檔日期原因

---

### **P0D — ADR consolidation**

統一到 `docs/adrs/`, 但先做:

1. 建立 `docs/adrs/README.md` index (列 ADR-001 ~ ADR-013 + 現況 status)
2. `git mv` 已散落:
   ```
   docs/adr/ADR-007-mts-manual-trade-price-authority.md      → docs/adrs/
   docs/adr-013-mts-ghost-position-race-condition.md        → docs/adrs/
   docs/decisions/ADR-006-*.md                               → docs/adrs/
   docs/decisions/adr_003_router_trace_observability.md      → docs/adrs/
   docs/decisions/adr_002_vertical_spread_default.md         → docs/adrs/
   docs/decisions/adr_001_disable_theta_gang.md              → docs/adrs/
   docs/decisions/MTS_SYNCHRONIZATION_AND_REALTIME_FIX_*.md  → docs/adrs/ (評估)
   docs/decisions/ATR_STANDARDIZATION.md                    → docs/adrs/ (評估)
   docs/plans/adr-010-sprint-6b-pm2-checkpoints.md           → docs/adrs/
   ```
3. 留一個 forward note 在原位指向新位置, 避免 agent 連結斷裂
4. **驗收**: `find docs -name 'adr*' -o -name 'ADR*'` 只剩 `docs/adrs/` 一個目錄 (除 forward notes)

---

### **P0E — Quarantine (not delete) stale tooling dirs**

⚠️ 不直接刪, 全部 quarantine:

```
.planning/      → _archive/tooling_stale/20260706/.planning/
.kiro/          → _archive/tooling_stale/20260706/.kiro/
.gemmacli/      → _archive/tooling_stale/20260706/.gemmacli/
.qwen/          → _archive/tooling_stale/20260706/.qwen/
.copilot/       → _archive/tooling_stale/20260706/.copilot/
.gsd/           → _archive/tooling_stale/20260706/.gsd/
backup_20260427/ → _archive/backups/20260706/backup_20260427/
backups/options_reset_*/ → _archive/backups/20260706/options_reset_*/
backups/stock_reset_*/   → _archive/backups/20260706/stock_reset_*/
```

**原因**: 這些可能含 agent prompt / 規格 / 歷史決策 / 還沒 merge 回主線的 hotfix。直接刪會斷上下文。

`.gitignore` 補上:
```
_archive/
_oco_checkpoints/
scratch/
artifacts/
output/
coverage_html/
htmlcov/
```

**驗收**:
- `git status` 不再顯示備份檔
- repo 根目錄可導航性恢復
- 之後 review 確認 quarantine 內容真的可丟, 才 `git rm -r` (留 git history)

---

### **P1A — ruff F821 / E722 only (真風險優先)**

⚠️ 不一次全修, 分批, 避免 diff 噪音:

1. **F821 undefined-name (88 個, 最高風險)**: 可能真 bug, 一個一個人工審
   - `ruff check . --select F821`
   - 每個都看是不是漏 import 還是拼錯
   - 修一類 commit 一次 (e.g. `fix: F821 in strategies/`)

2. **E722 bare-except (56 個, 會吞交易錯誤)**: 換成具體例外類型
   - `ruff check . --select E722`
   - 評估 each `except:` 是不是會吞掉 KeyError / AttributeError 那些該看到
   - 改成 `except KeyError:` 或記 log 後重 raise

3. **F841 unused-variable (111 個)**: 評估是真廢碼還是忘了用
4. **F401 unused-import (539 個) 最後處理**: 低風險但量最大, 用 `ruff check . --select F401 --fix`

**驗收**:
- `ruff check . --select F821,E722` 0 errors
- 全部 pytest 仍綠

---

### **P1B — pre-commit + smoke test**

1. 確認 `.pre-commit-config.yaml` 是否實際裝置
2. 加入:
   ```yaml
   - ruff check --fix
   - ruff format
   - pytest tests/ -q --timeout=30 -k "contract or legacy"
   ```
3. **驗收**: 任一 commit 都自動跑 lint + smoke test

---

### **P3A — Characterization tests (P2 鋪路)**

⚠️ 拆巨型檔前必先補契約測試, 鎖定當前行為:

必須新增:
```
tests/contracts/test_futures_monitor_shape.py       (P2.1 前置)
tests/contracts/test_dashboard_data_contract.py    (P2.2 前置)
tests/contracts/test_squeeze_monitor_contract.py   (P2.3 前置)
```

**方針**: 先鎖定行為介面 / 輸入輸出, 重構才安全。每個契約測試必須能在「反向 refactor」時 fail, 否則不算契約。

**驗收**:
- 3 個契約測試檔存在
- 涵蓋 monitor / dashboard / options_monitor 的主要 public API + data shape

---

### **P2 — Giant file extraction (高風險)**

⚠️ 任何 monitor.py / dashboard.py / live_options_squeeze_monitor.py 改動:
1. P3A 契約測試必須先綠
2. 按 RULES.md Rule 5: 一個 commit 只拆一個面向
3. 每次拆解前後跑 regression contract:
   ```bash
   pytest tests/strategies/test_squeeze_fire_scout.py -q   # 預期 21 passed
   ```
4. **先 extract pure function / helper, 不先改 control flow**

#### P2.1 `strategies/futures/monitor.py` (6,117 行) — 最後才動

**原因**: 還在 ADR-010 磨合期, 剛合併 trading safety guard。

拆解藍圖:
```
strategies/futures/
  monitor.py                  → root coordinator (< 500 行)
  indicator_engine.py         → 指標計算 (從 monitor.py 抽出, pure function)
  bar_regime_processor.py      → bar_regime + override 邏輯 (pure)
  manual_trade_handler.py      → manual_trade_flag 流程
  mts_state_manager.py         → MTS 狀態 save/restore
```

#### P2.2 `ui/dashboard.py` (5,285 行)

按 tab 分檔:
```
ui/
  dashboard.py            → entry + router (< 300 行)
  pages/futures_page.py
  pages/options_page.py
  pages/stocks_page.py
  pages/orders_page.py
  pages/regime_page.py
```

#### P2.3 `strategies/options/live_options_squeeze_monitor.py` (5,207 行)

拆: execution, greeks, snapshot, exit, dashboard 各抽出

先刪兩個 `_fixed_method.py` / `_fixed_snapshot.py` dead 變體 → `scripts/deprecated/fixed_variants/`

#### P2.4 同名檔案重新命名/合併

- `core/attribution_dashboard.py` vs `ui/attribution_dashboard.py` → 合併或業務前綴
- `strategies/futures/squeeze_futures/ui/dashboard.py` vs `ui/dashboard.py` → 前者搬到 `ui/legacy_squeeze/` 或 deprecated
- `core/order_management/order_manager.py` vs `strategies/options/options_engine/engine/order_manager.py` → `futures_order_manager.py` vs `options_order_manager.py`

---

### **P4 — README / CHANGELOG / governance (4 週內)**

#### P4.1 關鍵流程文件對齊
- 補 `docs/architecture/current_state.md` (一行畫 system 拓樸)
- 刪除過時 readiness report (歸檔到 `docs/archive/reports_20260706/`)
- 保留 `docs/operations/` 下實際 operational runbook

#### P4.2 CHANGELOG 規範化
- 採用 Keep a Changelog 格式
- 每個 PR 必須更新 CHANGELOG.md

#### P4.3 README 重整
- 更新: 安裝步驟、執行步驟、專案結構、開發流程、測試、文件連結

#### P4.4 pyproject.toml 完整化
- 把 requirements.txt 的依賴搬入 `[project.dependencies]`
- 加入 `[tool.ruff]`, `[tool.pytest.ini_options]`, `[project.scripts]`

---

## 優先級與風險矩陣 (修訂)

| Phase | 改動範圍 | 交易邏輯風險 | ROI | 預估時間 |
|-------|---------|------------|-----|---------|
| P0A | 建立 baseline 報表 | 無 | 高 (後面比對基準) | 半天 |
| P0B | root .py 搬移 | 極低 (只搬不改) | 高 | 1 天 |
| P0C | docs / .md 歸位 | 無 | 中 | 半天 |
| P0D | ADR 整合 + index | 無 | 中 | 半天 |
| P0E | quarantine 廢棄目錄 | 極低 | 高 | 半天 |
| P1A | F821/E722 ruff 修 | 中 (可能真 bug) | 中 | 2-3 天 |
| P1B | pre-commit | 低 | 中 | 半天 |
| P3A | characterization tests | 無 | 高 (P2 鋪路) | 3-5 天 |
| P2.1 | monitor.py 拆解 | **高** | 高 | 1-2 週 (最後才動) |
| P2.2 | dashboard.py 拆解 | 中 | 中 | 1 週 |
| P2.3 | options monitor 拆解 | **高** | 中 | 1 週 |
| P2.4 | 同名檔案重整 | 低 | 低 | 2 天 |
| P4.1-P4.4 | 文件 / 配置 | 無 | 低 | 1 週 |

---

## 「不建議動」清單 (整段計劃期間)

下列檔案/目錄在 P0-P2 都不應動 (除非顯式標 entry):

- `core/order_management/` (ADR-010 剛上線, 磨合期) — P2.1 之後才評估
- `strategies/plugins/futures/active/squeeze_fire_scout.py` (有 regression contract, 21 passed)
- `tests/test_adr_010_*.py` (ADR-010 契約測試)
- `tests/strategies/test_squeeze_fire_scout.py` (regression contract)
- `config/*.yaml` (生產配置)
- `data/taifex_raw/` (歷史資料)
- `docs/adrs/ADR-009-position-lifecycle-oca.md` (除非有明確升級)
- `docs/adrs/ADR-010-broker-level-release-oco.md`
- `RULES.md` (agent / 人類 workflow 依賴)

---

## Branch 與 versi珞化策略

```bash
# 開新 branch (與 ADR-010 runtime 修復線分離)
git checkout -b chore/repo-hygiene-p0

# 出發前打 tag, 以防需 rollback
git tag archive/2026-07-06-pre-cleanup

# P0 各階段獨立 commit (one fix per change):
# chore(repo): P0A — repo inventory baseline report
# chore(repo): P0B — relocate root test_*.py to tests/legacy/
# chore(repo): P0B — relocate root debug_*.py to scripts/debug/
# chore(repo): P0B — relocate root analyze_*.py to scripts/analysis/
# chore(repo): P0B — relocate *_fixed.py to scripts/deprecated/fixed_variants/
# chore(repo): P0C — relocate stale root .md to docs/archive/reports_20260706/
# chore(repo): P0D — consolidate ADRs to docs/adrs/ + index
# chore(repo): P0E — quarantine stale tooling dirs to _archive/
```

P0 完成後 PR review, 確認沒誤傷才 merge 回 master。P1/P2 各自再開 branch。

---

## 後續規劃 (P2 之後, 未排程)

- Strategy plugin hot-reload 機制驗證
- Backtest engine 統一 (目前兩套: squeeze_futures + backtest_engine)
- 多市場擴展準備 (現有期貨 + 選擇權 + 股票各一套, 未來可考慮共用底層)
- Strategy analytics 自動匯出 → dashboard (減少手動回測腳本數量)

---

## 附錄 A: P0A 基準線數字 (即將由 P0A 產出)

P0A 報表產出後填入, 後續所有改動以此為基準:

```
Python 檔數: 575 (不含 venv)
Python 總行數: 123,081
ruff errors: 1,518 (F401=539, F541=299, E402=175, E701=143, F841=111, F821=88, E722=56, E712=31, F811=28, syntax=17, E702=17, E741=9, E401=4, F522=1)
pytest collected: ___ (待 P0A 量測)
pytest passed: ___ (待 P0A 量測)
root .py count: ~40
root .md count: 53
docs/ .md count: 144
backups/options_reset_*: 14
backup_20260427/: 1
.planning/ files: 71
duplicate basenames: monitor.py×3, dashboard.py×2, order_manager.py×2, attribution_dashboard.py×2, strategy_router.py×2, schemas.py×2, risk_manager.py×2, notifier.py×2, monte_carlo.py×2, indicators.py×2, entry_strategies.py×2, downloader.py×2, data_storage.py×2, adaptive_orb_v2.py×2
giant files (>1000 lines):
  strategies/futures/monitor.py            6117
  ui/dashboard.py                           5285
  strategies/options/live_options_squeeze_monitor.py  5207
  strategies/plugins/futures/active/tmf_spread.py      2314
  core/order_management/order_manager.py              1272
  strategies/stocks/monitor.py                        1148
  core/futures_strategy_router.py                    1122
  main.py                                            1086
```
