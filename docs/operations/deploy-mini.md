# Mini：單一部署入口

2026-10-12 Codex。這是 PAPER 工作流程；不自動切 LIVE，不碰 stock-runner。

## 原則

程式版本由各程序自己的 Git HEAD / `LRC_RELEASE_SHA` 識別；可變參數由
`TRADING_RUNTIME_DIR/config/futures.paper.yaml` 的 SHA-256 識別。
MTS 和 dashboard 即使暫時分屬不同 release，也讀同一份 PAPER profile。
缺少 runtime profile 時保留舊 baseline 行為；已安裝但無效時不得退回其他檔案。
`futures_live.yaml` 保持原有 sealed profile 與 LIVE 認證流程。

## 一次性設定

在 Mini 建立 host-local JSON（不放 Git、不含憑證），例如：

```json
{
  "mts_root": "/Users/myllin_mini/Documents/mylin102/tw-trading-unified-release18",
  "dashboard_root": "/Users/myllin_mini/Documents/mylin102/tw-trading-unified-dashboard-legpnl-20261005",
  "runtime_dir": "/Users/myllin_mini/Documents/mylin102/tw-trading-unified-runtime",
  "python_bin": "/Users/myllin_mini/Documents/mylin102/tw-trading-unified-release15/.venv/bin/python3",
  "pm2_bin": "/Users/myllin_mini/.local/bin/pm2",
  "pm2_home": "/Users/myllin_mini/.pm2",
  "launchd_plist": "/Users/myllin_mini/Library/LaunchAgents/pm2.myllin_mini.plist"
}
```

路徑必須以 Mini 的 `.deployment-target` 與 PM2 實際值重新核對，範例不是授權。
不得複製身份檔。腳本只接受 Mini/PAPER，錯誤主機會在呼叫 PM2 前拒絕。
Python interpreter 透過 `TRADING_PYTHON_BIN` 明確指定；虛擬環境目錄不是應用程式版本。

## 程式部署（首次安裝 runtime profile 也走這條路）

1. Air4 完成受影響測試並提交；最後重新記錄 freeze manifest 並提交。
2. 透過 Git 推送候選到 Mini，確保兩個 target 倉庫可解析核准的完整 SHA。
   不用 SCP 複製程式碼、不自動 fetch/pull、不自動選「最新」版本。
3. 在 Mini 執行同一入口。以下省略 `--apply` 是唯讀 preflight/plan：

```sh
export TRADING_PYTHON_BIN=/Users/myllin_mini/Documents/mylin102/tw-trading-unified-release15/.venv/bin/python3
bash scripts/deploy-mini.sh --site /absolute/path/mini-deploy.json release \
  --mts-sha <approved-40-character-SHA> \
  --dashboard-sha <approved-40-character-SHA>
```

確認計畫並取得此次停啟交易程序的明確授權後：

```sh
bash scripts/deploy-mini.sh --site /absolute/path/mini-deploy.json --apply release \
  --mts-sha <approved-40-character-SHA> \
  --dashboard-sha <approved-40-character-SHA> --maintenance-approved
```

流程：身份與模式檢查 → candidate freeze／ecosystem 檢查 → 自有 ENTRY 維護鎖
→ 回退資訊 → 停止兩個受影響程序且確認舊 PID 消失 → Git fast-forward
→ 原子發布 runtime profile → canonical ecosystem 啟動 MTS／重新綁定 dashboard
→ 真實 dashboard classifier、SHA、cwd、interpreter、模式、持倉、四路行情訂閱
→ 120 秒穩定觀察 → PM2 save 與 dump/launchd 核對 → 解鎖 → VERIFIED receipt。

已安裝 profile 的 quote-width 會保留；其他 runtime 參數與原 template 不同時
會要求先審查，不默默覆蓋。其他程序若共用即將變更的 source root，會拒絕部署，
避免它繼續跑舊記憶體／新磁碟程式的混合版本。

## 參數熱更新

首次程式部署完成後：

```sh
bash scripts/deploy-mini.sh --site /absolute/path/mini-deploy.json --apply params \
  --max-spread-width 4
```

本命令只改 `mts.params.max_spread_width`。不改 release 檔案、不推進 HEAD、
不呼叫 PM2 停啟或 save；等待引擎發布新 config hash 並以 dashboard 實際判斷函式驗證。
目前只開放這個參數，其他調參不會被此命令連帶修改。
PAPER 不查券商持倉或委託；程式部署後原本引擎仍會建立正常市場行情連線。

## 失敗與證據

預設僅計畫；`--apply` 才會改動。正式結果寫在
`<runtime>/logs/deployments/codex-<transaction-id>/`，包含前態、參數備份、
觀察樣本與 `complete.json` 或 `failed.json`，不寫 PM2 原始 credential dump。

有持倉時照常以 PAPER ledger 校驗，從不強制平倉或手改帳本/state/context。
只移除本次自己建立、且 token 相符的維護鎖；既有鎖絕不覆蓋。
開盤時驗證 state freshness，休市只略過 freshness，不略過身分／持倉／模式檢查。

任何失敗都停止後續步驟。若已建立維護鎖，就保留鎖與 `failed.json`，不保存成功、
不自動 rollback／重啟／改寫交易狀態。操作者依 `before.json` 比對實際進程、Git HEAD、
profile 與 ledger 後決定修復／回退；不要直接刪鎖重跑。部分部署過程的狀態不是成功上線。

LIVE 仍走既有 `scripts/deployment/deploy_release.sh` 的 broker reconciliation、
freeze、promotion gates；本入口收到 `--mode live` 會拒絕，不會代為啟用實盤。

## 此 slice 的交付界線

僅離線程式與測試。本 slice 不建立 Mini site JSON、不更新線上原始碼、
不啟停服務、不查券商、不修改持倉。需要另一次核准的維護部署才會在線上生效。
