# Rule: Multi-Host Change, Synchronization, and Deployment Provenance

## 1. Purpose

本專案同時運行於多台主機，至少包括：

* `air4`：本機開發與主要操作環境
* `mini`：遠端執行與部署環境

Agent 不得僅以目前 shell、SSH session、IP、目錄名稱或先前對話推測操作目標。

任何程式碼修改、cron 修改、PM2 restart、套件安裝、設定變更或資料修復，都必須建立完整的 **Target Provenance** 與 **Synchronization Evidence**。

核心原則：

> 未確認主機身份，不得修改。
> 未確認 Git 基線，不得同步。
> 未驗證 runtime，不得宣稱部署完成。
> 未提供證據，不得聲稱 Air4 與 Mini 已一致。

---

## 2. Host Roles

除非使用者明確指定其他流程，預設角色如下：

| Host | Role                      | Allowed Operations                                |
| ---- | ------------------------- | ------------------------------------------------- |
| Air4 | Source / Development Host | 修改程式碼、執行測試、建立 commit                              |
| Mini | Deployment / Runtime Host | 拉取已確認 commit、修改 host-local runtime 設定、restart 與驗收 |

預設同步方向：

```text
Air4 working tree
    ↓
Git commit
    ↓
Remote repository
    ↓
Mini git pull / checkout exact commit
    ↓
Runtime restart
    ↓
Runtime verification
```

禁止將 `scp`、手動複製或雙邊直接編輯作為正常程式碼同步方式。

---

## 3. Mandatory Host Preflight

在任何 write operation 前，Agent 必須執行並記錄：

```bash
echo "===== TARGET PREFLIGHT ====="
date "+%Y-%m-%d %H:%M:%S %Z"
hostname
scutil --get ComputerName 2>/dev/null || true
uname -n
uname -m
pwd
git rev-parse --show-toplevel
git rev-parse --short HEAD
git status --short
```

若主機有部署身份檔，還必須讀取：

```bash
cat .deployment-target
```

建議內容：

Air4：

```text
air4
```

Mini：

```text
mini
```

### Fail-closed conditions

發生以下任一情況，Agent 必須停止 write operation：

* `hostname` 與預期目標不符
* `.deployment-target` 與預期目標不符
* repo root 不符合預期
* branch 不符合預期
* working tree 存在不明修改
* 無法判斷目前是在 local shell 還是 SSH remote shell
* 使用者要求修改 Mini，但目前證據只顯示 Air4
* 使用者要求修改 Air4，但目前證據只顯示 Mini

不得以「看起來應該是」繼續修改。

---

## 4. Explicit Target Declaration

Agent 在開始修改前，必須明確記錄：

```text
Change target:
Host:
Deployment identity:
IP or SSH alias:
Repository:
Branch:
Commit before:
Operation type:
```

範例：

```text
Change target: Mini
Host: mts-mini
Deployment identity: mini
SSH alias: mini
Repository: /Users/mylin/tw-trading-unified
Branch: main
Commit before: a1b2c3d
Operation type: deploy + cron update + PM2 restart
```

「本機」、「遠端」、「目前這台」等模糊描述不得作為正式 target identity。

---

## 5. Source-of-Truth Policy

### 5.1 Application code

程式碼的 source of truth 必須是 Git commit。

包括：

* `ui/`
* `scripts/`
* `core/`
* `strategies/`
* `tests/`
* tracked config templates
* migration scripts

正常情況下：

* Air4 修改並 commit
* Mini checkout 或 pull 同一 commit
* Mini 不直接修改 tracked source files

### 5.2 Host-local configuration

以下內容可由各主機獨立管理，不要求 byte-for-byte 同步：

* `.env`
* API credentials
* PM2 local environment
* host-specific paths
* local Python virtual environment
* machine-specific ports
* secrets
* crontab
* temporary data
* log files
* runtime state files

但所有 host-local 差異都必須被明確列入部署紀錄。

### 5.3 Generated data

以下資料不得以 Git 作為常規同步機制：

* CSV market data
* JSONL runtime logs
* exported trades
* cache files
* `/tmp` state
* dashboard runtime artifacts

Agent 必須區分：

```text
code synchronization
configuration synchronization
runtime synchronization
data freshness
```

不得因程式碼一致就宣稱資料一致。

---

## 6. Code Synchronization Procedure

### Phase A — Modify on Air4

在 Air4：

```bash
git status --short
git rev-parse --short HEAD
```

完成修改後：

```bash
git diff --check
git diff --stat
git diff -- <changed-files>
```

執行相關測試後再 commit：

```bash
git add <changed-files>
git commit -m "<descriptive message>"
git rev-parse --short HEAD
```

Agent 必須記錄：

```text
Source host:
Commit before:
Commit after:
Files changed:
Tests executed:
Test result:
```

### Phase B — Push exact commit

```bash
git push
```

不得只說「程式碼已同步」。必須提供 commit hash。

### Phase C — Deploy exact commit to Mini

在 Mini 先執行 preflight：

```bash
hostname
cat .deployment-target
cd /expected/repo
git status --short
git rev-parse --short HEAD
```

若 working tree 非 clean，禁止直接 `git pull`。

部署：

```bash
git fetch --all --prune
git checkout <expected-branch>
git pull --ff-only
```

或更嚴格：

```bash
git fetch origin
git checkout --detach <exact-commit>
```

部署後必須確認：

```bash
git rev-parse --short HEAD
```

Mini commit 必須等於 Air4 已確認的部署 commit。

---

## 7. Prohibited Synchronization Patterns

除非使用者明確要求緊急 hotfix，禁止：

```text
Air4 直接改一份
Mini 再手動改另一份
```

禁止將以下方式當作正式同步完成證據：

* 比較修改時間
* 看到檔名相同
* agent 記得自己改過
* SSH command 沒報錯
* PM2 顯示 online
* Dashboard 頁面可以打開
* `scp` 完成
* 兩台都有某個 function name
* cron 看起來類似

檔案 `mtime` 會被以下操作污染：

* bulk copy
* migration
* restore
* `touch`
* `scp`
* editor save
* archive extraction

因此不得使用 `mtime` 判斷程式碼同步狀態。

---

## 8. Emergency Hotfix Rule

只有在無法立即可走 Git 流程、且使用者明確要求緊急修復時，Agent 才可直接修改 Mini。

直接修改前必須保存：

```bash
git status --short
git diff > /tmp/pre-hotfix.patch
shasum -a 256 <target-files>
```

修改後必須：

```bash
git diff --check
git diff -- <target-files>
shasum -a 256 <target-files>
```

並立即標記：

```text
HOTFIX_APPLIED_ON_MINI
NOT_YET_SYNCHRONIZED_TO_AIR4
```

之後必須將 hotfix 回灌至 Air4，建立正式 commit，再重新部署。

在回灌完成前，不得宣稱兩台已同步。

---

## 9. Cron Synchronization Rule

Cron 屬於 host-local runtime configuration，不能因 Git 程式碼更新而假設已同步。

修改 cron 前：

```bash
crontab -l > /tmp/crontab.before
```

修改後：

```bash
crontab -l > /tmp/crontab.after
diff -u /tmp/crontab.before /tmp/crontab.after || true
```

每條 cron 必須明確指定：

* absolute repo path
* absolute Python path
* explicit ticker
* log destination
* working directory
* expected script

範例：

```cron
30 20 * * * cd /Users/mylin/tw-trading-unified && /Users/mylin/tw-trading-unified/.venv/bin/python scripts/update_calendar_spread.py --ticker tmf >> logs/calendar_spread_cron.log 2>&1
```

禁止依賴：

* cron 預設 `PATH`
* cron 預設 `cwd`
* 隱式 ticker
* 不明 virtualenv
* 相對 log path

Agent 必須分別驗證 Air4 與 Mini 的 cron，不得以其中一台的結果推論另一台。

---

## 10. PM2 and Runtime Deployment Rule

Git commit 一致不代表 runtime 已使用新程式碼。

每次 restart 前必須確認：

```bash
pm2 describe dashboard
```

至少檢查：

```text
script path
exec cwd
interpreter
status
process id
```

restart 後：

```bash
pm2 restart dashboard
pm2 describe dashboard
pm2 logs dashboard --lines 100
```

Agent 必須驗證：

* PM2 `cwd` 指向預期 repo
* PM2 script path 正確
* 新 PID 或新 restart count 已產生
* runtime log 出現新版本行為
* 沒有 import error
* 沒有讀到另一份 repo
* 沒有舊 process 仍占用 port

不得只因 `pm2 restart` 回傳 success 就宣稱部署完成。

---

## 11. File Identity Verification

需要確認兩台 tracked files 是否相同時，使用：

```bash
shasum -a 256 \
  ui/dashboard.py \
  scripts/update_calendar_spread.py
```

但正式同步判定仍以 Git commit 為主。

判定優先級：

```text
1. Git commit hash
2. Git working tree cleanliness
3. File content hash
4. Runtime loaded path
5. Runtime behavior
```

不得使用 `mtime` 作為同步判定。

---

## 12. Dashboard Calendar Spread Verification

針對 Calendar Spread 修復，兩台主機必須分別確認：

```text
requested ticker
selected file
resolved path
file date
file mtime_ns
row count
minimum timestamp
maximum timestamp
```

預期 log 格式：

```text
[Calendar Spread]
host=<hostname>
ticker=tmf
path=<resolved-path>
rows=<row-count>
range=<min-ts> ~ <max-ts>
```

驗收條件：

1. TMF 頁面只能選到 `tmf_calendar_spread_*.csv`
2. 不得選到 `mxf_` 或 `mtx_`
3. 較新的跨產品 `mtime` 不得改變選擇
4. CSV 更新後 cache 必須失效
5. Dashboard 可正確顯示 stale data warning
6. 資料過期與選檔錯誤必須分開回報

正確選到舊 TMF 檔案代表：

```text
source selection correct
data freshness failed
```

不得將兩者混為同一問題。

---

## 13. Data Freshness Is Not Code Synchronization

Agent 必須明確區分以下狀態：

```text
Code synchronized: yes / no
Runtime restarted: yes / no
Correct file selected: yes / no
CSV generation healthy: yes / no
Latest data timestamp:
```

例如：

```text
Code synchronized: YES
Runtime restarted: YES
Correct TMF file selected: YES
CSV generation healthy: NO
Latest data timestamp: 2026-07-20
```

不得因 CSV 仍停在舊日期，就推論 Dashboard patch 沒部署。

同樣地，也不得因 Dashboard 能顯示新資料，就推論兩台程式碼完全一致。

---

## 14. Mandatory Post-Change Evidence

每次修改完成後，Agent 必須輸出：

```text
Target host:
Hostname:
Deployment identity:
Repository:
Branch:
Commit before:
Commit after:
Working tree:
Files changed:
File hashes:
Cron changed:
PM2 processes restarted:
Runtime path:
Verification commands:
Verification results:
Known remaining issues:
```

若同時操作 Air4 與 Mini，必須分成兩段：

```text
AIR4 RESULT
MINI RESULT
```

不得將兩台結果混寫成單一摘要。

---

## 15. Required Status Vocabulary

Agent 只能使用以下具體狀態：

* `MODIFIED_ON_AIR4`
* `COMMITTED_ON_AIR4`
* `PUSHED_TO_REMOTE`
* `DEPLOYED_TO_MINI`
* `MINI_RUNTIME_RESTARTED`
* `MINI_RUNTIME_VERIFIED`
* `HOTFIX_ONLY_ON_MINI`
* `NOT_SYNCHRONIZED`
* `CODE_SYNCHRONIZED`
* `RUNTIME_NOT_VERIFIED`
* `DATA_FRESHNESS_FAILED`
* `ROOT_CAUSE_UNCONFIRMED`

禁止在缺乏證據時使用：

* 已完成
* 已同步
* 已部署
* 已修好
* 兩台都改了
* 現在沒問題

---

## 16. Completion Criteria

只有以下條件全部成立，Agent 才能宣稱「Air4 與 Mini 已同步並完成部署」：

```text
[ ] Air4 target identity verified
[ ] Mini target identity verified
[ ] Air4 working tree status known
[ ] Mini working tree status known
[ ] Air4 changes committed
[ ] Commit pushed
[ ] Mini deployed to exact expected commit
[ ] Mini tracked working tree clean
[ ] Cron verified independently on Mini
[ ] PM2 cwd and script path verified
[ ] Required processes restarted
[ ] Runtime logs confirm new behavior
[ ] Remaining infrastructure or data issues listed separately
```

若缺少任何一項，必須使用較窄的狀態描述，例如：

```text
Code committed on Air4, Mini deployment not verified.
```

或：

```text
Mini patched and restarted, but Air4 synchronization is pending.
```

---

## 17. Standard Agent Report Template

```text
## Change Target

Host:
Deployment identity:
Repository:
Branch:
Commit before:

## Changes

Files changed:
Cron changed:
Runtime configuration changed:

## Synchronization

Air4 commit:
Remote push:
Mini commit:
Commit match:
Working tree status:

## Runtime

PM2 process:
PM2 cwd:
Restart performed:
Runtime verification:

## Data

Selected ticker:
Selected CSV:
Latest timestamp:
Freshness status:

## Final Status

Code synchronization:
Runtime deployment:
Runtime verification:
Data freshness:
Remaining issues:
```

---

## 18. Final Governing Principle

> Agent 必須把「修改發生在哪裡」、「程式碼是否同步」、「runtime 是否重啟」、「資料是否更新」視為四個獨立問題。
> 每一項都必須由主機身份、Git commit、檔案 hash、cron 狀態與 runtime log 提供證據。
> 缺乏任何一段證據時，必須 fail closed，不得以敘述性推測補足。
