# Documentation Consolidation Plan

Scope: branch `mac-m1`

This plan defines how to consolidate the current documentation set without losing operational history for this trading system. Because code and runtime behavior differ by branch, all architecture, operations, deployment, and readiness docs are treated as branch-scoped unless explicitly marked repo-wide.

## Goals

- Keep a small set of canonical docs easy to find and maintain.
- Move time-bound reports into dated archive folders.
- Make branch scope explicit so readers do not assume a doc applies to all branches.
- Eliminate ambiguous names such as `FINAL`, `LATEST`, and `V2`.

## Naming Rules

- Canonical docs use fixed names and do not include dates.
- Historical snapshots use `YYYY-MM-DD-branch-topic.md`.
- All new doc filenames use lowercase kebab-case.
- Avoid `final`, `latest`, `new`, `v2`, and similar time-sensitive labels.
- If a doc is branch-specific, include either the branch in the filename for archived docs or a `Scope: branch <name>` line near the top for canonical docs.

## Scope Rules

- Repo-wide docs: `RULES.md`, `AGENTS.md`, `CHANGELOG.md`, future doc style guide.
- Branch-scoped canonical docs: architecture, operations, deployment, strategy behavior, live trading guides, checklists.
- Historical snapshots: reports, reviews, analyses, migration summaries, session notes, incident writeups.

## Target Structure

```text
README.md
RULES.md
CHANGELOG.md
AGENTS.md
docs/
  architecture.md
  operations.md
  strategies.md
  daily-trading-checklist.md
  docs-style-guide.md
  branches/
    mac-m1/
      branch-notes.md
  archive/
    readiness/
    reviews/
    analysis/
    sessions/
    migration/
```

## Action Legend

- `keep`: keep as a canonical doc, possibly rewritten.
- `merge into`: absorb content into another canonical doc, then retire the source.
- `rename`: keep content but rename to fit the new convention.
- `archive`: preserve as historical reference under `docs/archive/...`.
- `drop`: remove after content is merged or confirmed obsolete.

## Root-Level Mapping

| Current file | Action | Target / Notes |
| --- | --- | --- |
| `README.md` | keep | Rewrite as the single entry point with links to canonical docs. |
| `RULES.md` | keep | Repo-wide trading safety invariants. |
| `AGENTS.md` | keep | Agent workflow rules. |
| `CHANGELOG.md` | keep | Repo-wide change history. |
| `INSTALL.md` | merge into | `docs/operations.md` |
| `REPO_MAP.md` | merge into | `README.md` or `docs/architecture.md` depending on content depth. |
| `QUANT_LAB_OPERATIONS.md` | merge into | `docs/operations.md` |
| `FINAL_LAUNCH_CHECKLIST.md` | merge into | `docs/daily-trading-checklist.md`, then archive any point-in-time assertions. |
| `MARKET_OPEN_CHECKLIST.md` | merge into | `docs/daily-trading-checklist.md` |
| `PRE_MARKET_FINAL_CHECKLIST.md` | merge into | `docs/daily-trading-checklist.md` |
| `SYSTEM_READINESS_CHECKLIST.md` | merge into | `docs/daily-trading-checklist.md` |
| `MARKET_OPEN_READINESS_REPORT.md` | archive | `docs/archive/readiness/YYYY-MM-DD-mac-m1-market-open-readiness.md` |
| `FINAL_MARKET_OPEN_READINESS_REPORT.md` | archive | `docs/archive/readiness/YYYY-MM-DD-mac-m1-market-open-readiness.md` |
| `FINAL_SYSTEM_READINESS_REPORT.md` | archive | `docs/archive/readiness/YYYY-MM-DD-mac-m1-system-readiness.md` |
| `SYSTEM_READY_CONFIRMATION.md` | archive | `docs/archive/readiness/YYYY-MM-DD-mac-m1-system-ready-confirmation.md` |
| `LIVE_TRADING_RISK_ASSESSMENT.md` | archive | `docs/archive/analysis/YYYY-MM-DD-mac-m1-live-trading-risk-assessment.md` |
| `LIVE_TRADING_TEST_REPORT.md` | archive | `docs/archive/reviews/YYYY-MM-DD-mac-m1-live-trading-test-report.md` |
| `TRADING_SYSTEM_CONSISTENCY_GSD_REPORT.md` | archive | `docs/archive/reviews/YYYY-MM-DD-mac-m1-trading-system-consistency.md` |
| `OPTION_TRADING_CONSISTENCY_GSD_REPORT.md` | archive | `docs/archive/reviews/YYYY-MM-DD-mac-m1-option-trading-consistency.md` |
| `OPTION_TRADING_CONSISTENCY_FIX_REPORT.md` | archive | `docs/archive/reviews/YYYY-MM-DD-mac-m1-option-trading-consistency-fix.md` |
| `OPTION_DATA_GAP_FIX_REPORT.md` | archive | `docs/archive/analysis/YYYY-MM-DD-mac-m1-option-data-gap-fix.md` |
| `option_data_gap_analysis.md` | rename | `docs/archive/analysis/YYYY-MM-DD-mac-m1-option-data-gap-analysis.md` |
| `DATA_REPAIR_REPORT.md` | archive | `docs/archive/analysis/YYYY-MM-DD-mac-m1-data-repair-report.md` |
| `SYSTEM_UPGRADE_REPORT_20260417.md` | rename | `docs/archive/migration/2026-04-17-mac-m1-system-upgrade.md` |
| `GPT_ADAPTIVE_TRADING_REVIEW.md` | rename | `docs/archive/reviews/2026-04-17-mac-m1-adaptive-trading-review.md` |
| `NIGHT_SESSION_TRADING_REVIEW.md` | archive | `docs/archive/reviews/YYYY-MM-DD-mac-m1-night-session-trading-review.md` |
| `NIGHT_SESSION_IMPROVEMENT_PLAN.md` | merge into | `docs/operations.md` if still active; otherwise archive under `analysis`. |
| `ADAPTIVE_TRADING_FRAMEWORK_DESIGN.md` | merge into | `docs/strategies.md` or a branch-specific strategy design doc. |
| `DATA_RECORDING_STRATEGY.md` | merge into | `docs/operations.md` if this is active policy; otherwise archive. |
| `CEO_DECISION_SUMMARY_CANSLIM.md` | archive | `docs/archive/analysis/YYYY-MM-DD-mac-m1-ceo-decision-summary-canslim.md` |
| `CEO_STRATEGIC_ASSESSMENT_CANSLIM_INTEGRATION.md` | archive | `docs/archive/analysis/YYYY-MM-DD-mac-m1-canslim-integration-assessment.md` |
| `CEO_STRATEGIC_REVIEW_CHIP_ANALYSIS.md` | archive | `docs/archive/analysis/YYYY-MM-DD-mac-m1-chip-analysis-review.md` |
| `CANSLIM_WATCHLIST_MIGRATION_REPORT.md` | archive | `docs/archive/migration/YYYY-MM-DD-mac-m1-canslim-watchlist-migration.md` |
| `CHIP_ANALYSIS_IMPLEMENTATION_PLAN.md` | archive | `docs/archive/analysis/YYYY-MM-DD-mac-m1-chip-analysis-implementation-plan.md` |
| `P2_OPTIMIZATION_BACKTEST_REPORT.md` | archive | `docs/archive/reviews/YYYY-MM-DD-mac-m1-p2-optimization-backtest.md` |
| `SQUEEZE_BACKTEST_IMPORT_CONFIRMATION.md` | archive | `docs/archive/migration/YYYY-MM-DD-mac-m1-squeeze-backtest-import.md` |
| `STOCK_DATA_DOWNLOAD_COMPLETE_REPORT.md` | archive | `docs/archive/migration/YYYY-MM-DD-mac-m1-stock-data-download.md` |
| `STOCK_SYSTEM_ANALYSIS.md` | archive | `docs/archive/analysis/YYYY-MM-DD-mac-m1-stock-system-analysis.md` |
| `STRONG_SECTOR_STOCK_SELECTION_SYSTEM.md` | merge into | `docs/strategies.md` if still active; otherwise archive. |
| `BUG_REPORT_ODD_LOT.md` | archive | `docs/archive/analysis/YYYY-MM-DD-mac-m1-odd-lot-bug-report.md` |
| `CLAUDE.md` | drop | Consolidate tool-specific agent notes into one repo-wide policy if still needed. |
| `GEMINI.md` | drop | Consolidate tool-specific agent notes into one repo-wide policy if still needed. |
| `QWEN.md` | drop | Consolidate tool-specific agent notes into one repo-wide policy if still needed. |
| `TODOS.md` | keep | Optional operational backlog, but not part of canonical docs set. |

## `docs/` Mapping

| Current file | Action | Target / Notes |
| --- | --- | --- |
| `docs/TECHNICAL_ARCHITECTURE.md` | rename | `docs/architecture.md` |
| `docs/ORDER_LIFECYCLE_ARCHITECTURE.md` | merge into | `docs/architecture.md` or split section in `docs/operations.md` if execution-focused. |
| `docs/ORDER_LIFECYCLE_TECH_SPEC.md` | merge into | `docs/architecture.md` |
| `docs/SDD.md` | merge into | `docs/architecture.md` |
| `docs/SDD_PLUGGABLE_STRATEGY_MODULE.md` | merge into | `docs/strategies.md` |
| `docs/SQUEEZE_INTEGRATION_SDD.md` | merge into | `docs/strategies.md` |
| `docs/STOCK_MODULE_SDD.md` | merge into | `docs/strategies.md` |
| `docs/V_CYCLE_REQUIREMENTS.md` | merge into | `docs/docs-style-guide.md` or `docs/architecture.md` if still active engineering process. |
| `docs/V_CYCLE_SYSTEM_DESIGN.md` | merge into | `docs/architecture.md` |
| `docs/V_MODEL_PLUGGABLE_STRATEGIES.md` | merge into | `docs/strategies.md` |
| `docs/V_MODEL_TEST_PLAN.md` | merge into | `docs/docs-style-guide.md` or future engineering process doc. |
| `docs/LIVE_TRADING_GUIDE.md` | merge into | `docs/operations.md` |
| `docs/QUANT_LAB_HANDBOOK.md` | merge into | `docs/operations.md` |
| `docs/ADAPTIVE_STRATEGY_README.md` | merge into | `docs/strategies.md` |
| `docs/ADAPTIVE_STRATEGY_FRAMEWORK.md` | merge into | `docs/strategies.md` |
| `docs/ADAPTIVE_STRATEGY_GSD_PLAN.md` | archive | `docs/archive/analysis/YYYY-MM-DD-mac-m1-adaptive-strategy-gsd-plan.md` |
| `docs/HOWTO_CREATE_STRATEGY_PLUGIN.md` | merge into | `docs/strategies.md` or a focused `docs/strategy-plugin-spec.md` if kept separate. |
| `docs/CHIP_STRATEGY_GUIDE.md` | merge into | `docs/strategies.md` |
| `docs/ELITE_STRATEGIES.md` | merge into | `docs/strategies.md` |
| `docs/ELITE_QUICK_REFERENCE.md` | merge into | `docs/strategies.md` |
| `docs/TMF.md` | merge into | `docs/strategies.md` if active; otherwise archive. |
| `docs/trading_strategy_guide.md` | merge into | `docs/strategies.md` |
| `docs/STOCK_TRADING_GUIDE.md` | merge into | `docs/operations.md` or `docs/strategies.md` depending on content. |
| `docs/SHIOAJI_API_REFERENCE.md` | keep | Branch-specific reference if actively maintained; otherwise move to `docs/branches/mac-m1/`. |
| `docs/DASHBOARD_UI_DESIGN.md` | archive | `docs/archive/analysis/YYYY-MM-DD-mac-m1-dashboard-ui-design.md` |
| `docs/BACKTEST_DASHBOARD_PLAN.md` | archive | `docs/archive/analysis/YYYY-MM-DD-mac-m1-backtest-dashboard-plan.md` |
| `docs/backtest_dashboard_review.md` | rename | `docs/archive/reviews/YYYY-MM-DD-mac-m1-backtest-dashboard-review.md` |
| `docs/ELITE_IMPLEMENTATION_SUMMARY.md` | archive | `docs/archive/migration/YYYY-MM-DD-mac-m1-elite-implementation-summary.md` |
| `docs/WAVE1_STABILIZATION_REPORT.md` | archive | `docs/archive/reviews/YYYY-MM-DD-mac-m1-wave1-stabilization.md` |
| `docs/DAY_NIGHT_CONFIG_ANALYSIS.md` | archive | `docs/archive/analysis/YYYY-MM-DD-mac-m1-day-night-config-analysis.md` |
| `docs/KBAR_STALENESS_ANALYSIS.md` | archive | `docs/archive/analysis/YYYY-MM-DD-mac-m1-kbar-staleness-analysis.md` |
| `docs/strategy_review.md` | rename | `docs/archive/reviews/YYYY-MM-DD-mac-m1-strategy-review.md` |
| `docs/SESSION_REVIEW_20260413.md` | rename | `docs/archive/sessions/2026-04-13-mac-m1-session-review.md` |
| `docs/METHODOLOGIES.md` | merge into | `docs/strategies.md` or `docs/operations.md` depending on actual scope. |
| `docs/BUGFIX_AUTOSTART_RESTART.md` | archive | `docs/archive/analysis/YYYY-MM-DD-mac-m1-autostart-restart-bugfix.md` |
| `docs/GSD_NIGHT_SESSION_FIX.md` | archive | `docs/archive/analysis/YYYY-MM-DD-mac-m1-night-session-fix.md` |
| `docs/SQUEEZE_FAILURE_STRATEGY.md` | archive | `docs/archive/analysis/YYYY-MM-DD-mac-m1-squeeze-failure-strategy.md` |
| `docs/kbar.md` | keep | Keep if it is an enduring domain reference; otherwise move to `docs/branches/mac-m1/`. |
| `docs/greeks.md` | keep | Keep if it is an enduring domain reference; otherwise move to `docs/branches/mac-m1/`. |
| `docs/option_callback.md` | keep | Technical reference, likely branch-specific. |
| `docs/五檔報價Callback.md` | rename | `docs/level2-quote-callback.md` if retained. |
| `docs/零股交易策略.md` | rename | `docs/odd-lot-trading-strategy.md` if retained. |

## Recommended First Wave

1. Create the canonical set: `docs/architecture.md`, `docs/operations.md`, `docs/strategies.md`, `docs/daily-trading-checklist.md`, `docs/docs-style-guide.md`.
2. Rewrite `README.md` to point only to those canonical docs.
3. Move dated reports into `docs/archive/...` with explicit branch names.
4. Replace retired source docs with short redirect stubs only if existing links are still heavily used.
5. After link cleanup, remove redundant root-level report files.

## Open Decisions

- Whether `docs/SHIOAJI_API_REFERENCE.md`, `docs/kbar.md`, and `docs/greeks.md` should remain canonical references or move under `docs/branches/mac-m1/`.
- Whether strategy-specific docs should stay as one `docs/strategies.md` or split into `docs/strategy-plugin-spec.md` plus a separate strategy catalog.
- Whether tool-specific files such as `CLAUDE.md`, `GEMINI.md`, and `QWEN.md` still have unique value after consolidation.
