CROSS‑REGIME 快速自我檢查表

目的
----
快速驗證目前 release 是否在生產環境下安全運行（feed、新鮮度、regime 判斷、decision gating、系統穩定性）。

檢查清單
--------
1) feed 層
- TX tick 有進來（觀察 log 或 feed age）
- TMF tick 有進來
- feed age 大部分時間 < 10 秒（警戒線 45s，重啟門檻 120s）

驗證項目: 在 runtime log 中應常見: `Feed age TX=2s TMF=1s OPT=3s`

2) regime 層
- TX 偶爾顯示 TREND（UP/DOWN）與 CHOP（非固定）
- TMF 出現 BREAKOUT_READY 和 MEAN_REVERT

驗證項目: agent log 印出 `[CROSS] tx=... tmf=... reason=...`，且 tx/tmf 不總是相同字串

3) decision 層
- allow_trade 不會一直 True 也不會一直 False（視情況切換）
- ORB / VWAP 權重會根據 regime 切換（log 可見 orb_w / vwap_w）

驗證項目: log 範例: `[CROSS] ... allow=True orb_w=0.40 vwap_w=0.80 reason=CHOP_FADE_BREAKOUT`

4) stability
- 一日內自動 restart 次數 < 3（正常）
- 無連續 restart loop（連續 3 次短時間重啟視為 loop）

驗證項目: 監控系統或 supervisor log 應記錄 restart 次數；若發生頻繁重啟，查明 feed age 與 Shioaji event codes

自動化/操作指引
----------------
- 立即檢查 feed age:
  - 在主系統上: `python3 main.py --dry-run`，觀察 60s 內的 feed age 與 CROSS log
- 執行回測（快速 smoke）:
  - `python3 backtest_p0_p1.py` → 比對總交易/勝率/總損益
- 測試 dispatcher → TX bar builder:
  - 用 unit test: `pytest tests/test_integration_cross_regime_dispatcher.py -q`

失敗對應（建議）
----------------
- 若 TX 或 TMF 長時間沒 tick: 檢查訂閱、resolve_tx_contract、Shioaji event（12/13/20），並重啟代理。
- 若 regime 固定不變: 檢查 TxBarBuilder 匯入頻率、時間對齊（已加入 time-alignment 檢查）。
- 若 allow_trade 永遠 True/False: 檢查 cross_regime policy 引數與 feed freshness 標誌。

紀錄位置
--------
- 主要 log: `logs/` 下的 runtime logs 或 systemd / supervisor 日誌
- 回測輸出: 在 console 或 /tmp (視運行方式)

備註
----
- 建議每日一次手動巡檢，並在 PR 合併前跑一次 paper dry-run 並附上日誌片段到 PR。

四個檢查點總覽（A–D，放在不同位置）
----------------------------------
A. tick callback（dispatcher）
- 每筆 tick 都更新 heartbeat：FeedHealth.mark_tick('TX'/'TMF')。
- 同步呼叫 TxBarBuilder.on_tick() 更新 TX bar。
- 不在此處做策略判斷（僅心跳與 bar 更新）。

B. main health loop（系統級 watchdog）
- 每 30 秒 檢查一次（health_check_at）
- 若 TX 或 TMF 任一超過 120 秒沒 tick → 直接 break 以觸發 supervisor/autostart 重啟
- 此層是 process 存活判斷，不做單筆下單決策。

C. strategy tick（策略級保護）
- 每次進入 _strategy_tick() 時先檢查：TX/TMF fresh、TX/TMF bars time-aligned
- 若任一檢查失敗：直接 return，skip 本輪策略執行
- 目標：避免在 stale 或不同步資料上做決策

D. before send order（執行前護欄）
- 在下單前最後再檢查：position、margin（paper cap 40,000 TWD）、price>0、feed freshness、stop_loss>=10 pts
- 任一失敗則拒絕下單並 log 原因

快速檢查命令（摘要）
--------------------
- Run smoke dry-run: `python3 main.py --dry-run`（觀察 60-120s 內的 feed age 與 CROSS 日誌）
- Quick backtest: `python3 backtest_p0_p1.py`
- Tests: `python3 -m pytest tests/ -q`

備註：請把 A/B/C/D 四個檢查點分別放在對應檔案/位置以提升可靠性，切勿只在某一處檢查。
