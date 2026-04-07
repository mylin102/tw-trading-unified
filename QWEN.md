## Qwen Added Memories
- ELITE STRATEGIES (去蕪存菁): 從 10 個策略精簡到 3 個。ELITE #1 Counter-VWAP (PF=1.95, 唯一真實回測驗證), ELITE #2 PSAR Breakout (PF=1.42 文獻估計), ELITE #3 Vol-Squeeze (PF=1.3 理論估計)。淘汰 7 個虧損策略 (night session, pure breakout, VWAP bounce, momentum burst, cumulative delta, volume reversal, gap reversal)。檔案: strategies/futures/elite_strategies.py, docs/ELITE_STRATEGIES.md, docs/ELITE_IMPLEMENTATION_SUMMARY.md, docs/ELITE_QUICK_REFERENCE.md
- BACKTEST RESULTS (2026 Q1): Counter-VWAP 最佳組合 ATR_SL=2.0x, Confirm=5bars, VWAP=true → PF=1.95, Win%=40.7%, PnL=+32,285, MaxDD=-7.2%, 86 trades。Breakout 最差 PF=1.02, Win%=27.3%, MaxDD=-25.8%, 444 trades。VWAP 是核心獲利機制: 有 VWAP PF=1.58 vs 無 VWAP PF=0.23 (7x 差異)。夜盤全部虧損 PF=0.00-0.20, 98% losing combos。回測數據在 exports/vbt_counter_sweep.csv, exports/vbt_breakout_sweep.csv
- BUGS FIXED: 1) 選擇權/期貨 重複進場 bug - position guard 失效導致 3-4 次重複進場，修復: enter_paper_position 加 max_positions 檢查, _recover_position_from_api 改為完整持倉計算 (ENTRY/TP1/EXIT 都要算)。2) R:R 倒賠 bug - TP1 100pts 比停損 108pts 還近，修復: TP1→200pts, 追蹤啟動→100pts, 保本→100pts。3) 選擇權評分反轉太敏感 60→40, 停損 10%→20%, 追蹤啟動 15%→8%。4) Dashboard KeyError 'action' 加欄位檢查。5) 摩擦成本未計入 PnL 已修復 (含手續費+稅)。6) 選擇權 ledger recovery 沒算 TP1 減碼已修復。
- CONFIG: config/futures.yaml - auto_select=true, active_strategy=counter_vwap, counter_mode enabled+auto_regime, PSAR min_adx=10 (夜盤適用), 口數=1, max_positions=1。config/options_strategy.yaml - V2 mode, entry_score=60, stop_loss_pct=0.2, TP1=0.5, trailing=0.15, 口數=1, max_positions=1。夜盤時間過濾已停用 (time_filter_enabled=false, skip_hours=[])。Dashboard 設定頁有口數/持倉限制/策略選擇/ATR/TP1 可調。
- METHODOLOGIES: GSTACK 方法論 (Boil the Lake 完整性/Search First/User Sovereignty), SDD 軟體設計 (Single Source of Truth/Side Effects After Validation/Defensive Programming), V-Model 測試 (Level1 Unit/Level2 Integration/Level3 System/Level4 UAT), GSD 工作流 (見下方)。104 個測試全部通過。核心文件: docs/METHODOLOGIES.md, docs/SDD.md, docs/V_MODEL_TEST_PLAN.md, docs/LIVE_TRADING_GUIDE.md (含摩擦成本分析: 期貨 ~206 TWD/2口, 選擇權 ~110-160 TWD/口)。
- LIVE TRADING READINESS: Phase 1 Paper 觀察中。Dashboard ⚙️ 設定頁有「實盤就緒度檢查」面板顯示 8 項檢查狀態 (🟢🟡🔴)。進入 Phase 2 條件: ≥10 筆交易, PF≥1.3, Win%≥30%, MaxDD≥-15%, 觀察≥7天, 停損 100% 觸發, 無重複進場, 選擇權 PnL 含手續費。Phase 2 建議: 1 口 TMF, max_daily_loss=2%, 5 交易日驗證。摩擦成本: 期貨損益兩平點 +3-4 pts。
- DIR STRUCTURE: strategies/futures/elite_strategies.py (3 elite strategies), strategies/futures/monitor.py (integrated elite), strategies/options/live_options_squeeze_monitor.py (fixed position guard), backtest/signal_generator.py (merged elite registry), core/live_readiness.py (8 readiness checks), ui/dashboard.py (lot size + readiness panel), config/futures.yaml + options_strategy.yaml (current settings), docs/ (all documentation), scripts/backtest_elite_strategies.py + validate_elite_strategies.py + show_performance.py + sweep_elite_params.py
- FULL Q1 BACKTEST (40,140 bars, 2.5 months): Counter-VWAP PF=1.95 WR=40.7% PnL=32,285 DD=-7.2% T=86. PSAR PF=1.13 WR=18% DD=-63% T=543. Vol-Squeeze PF=1.23 WR=20.4% DD=-55.3% T=812. 1-week data overfitted (PSAR was 4.16, Vol was 4.57). Only Counter-VWAP is truly profitable with acceptable risk. PSAR/Vol-Squeeze barely profitable with massive DD. Data downloaded from Shioaji API via patch_historical_data.py, saved as data/tmf_full_2026.csv (20MB, 55 daily files).

## GSD (Get Shit Done) Methodology
Source: ~/.gemini/get-shit-done/

### Dev Context (default mode)
- Concise, action-oriented responses. Lead with code change, brief rationale after.
- Skip preamble — assume developer has full context.
- Use inline code refs (`file:line`) over prose descriptions.
- Flag side effects / breaking changes immediately.
- Surface next actionable step at end of every response.
- Low verbosity. One-liner explanations unless non-obvious.

### Gate Discipline (all work)
- **Pre-flight**: Validate preconditions before starting. Block if missing.
- **Revision**: After producing output, check quality. Loop max 3 iterations.
- **Escalation**: If revision stalls, present options to user, wait.
- **Abort**: If continuing causes damage/waste, stop immediately, preserve state.

### Anti-Patterns (never do)
- Never walk through checklists one-by-one. Start broad, dig where interesting.
- No corporate speak. Plain language only.
- No premature constraints — understand problem before narrowing solutions.
- No `git add .` or `git add -A` — stage specific files only.
- No multiple next actions without priority — one primary, alternatives secondary.
- Never create artifacts user didn't approve.

### Bug Patterns (check before hypothesizing, ~80% coverage)
1. Null/Undefined Access — property on null, missing return, wrong branch
2. Async/Timing — missing await, race condition, stale closure, handler before setup
3. State Management — shared state mutation, stale state, multiple sources of truth
4. Off-by-One/Boundary — loop start/end, `<` vs `<=`, empty collection
5. Import/Module — circular dep, wrong export, missing extension, case sensitivity
6. Type/Coercion — string vs number comparison, implicit coercion, falsy surprises
7. Error Handling — swallowed error, wrong catch, unhandled rejection

### Workflow
1. **Discover** — trace full data/pipeline chain (SDD single source of truth)
2. **Plan** — identify root cause, propose fix, present to user
3. **Execute** — minimal diff, change only what's necessary, tests pass
4. **Verify** — run tests, confirm fix works, no regressions
5. **Ship** — summarize changes, suggest next step
