# ADR-023: Reporting Attribution Governance, Dual-Config Taxonomy, and Dashboard Sync Protocol

- **Status**: Accepted
- **Date**: 2026-07-27
- **Author**: Gemini CLI & Engineering Team
- **Context**: MTS Spread Trading & Governance

---

## 1. Context & Problem Statement

Prior to this architecture update, several operational and reporting governance issues were identified in the MTS spread trading platform:

1. **Producer–Consumer Schema Drift & False Attribution**:
   - Trading engine producers logged order submissions with `event="ORDER_SUBMITTED"` and `strategy="MTS_EXIT"` / `strategy="MTS_RELEASE"`, whereas daily report generators expected `EXIT_REMAINING` / `RELEASE_NEAR_SUBMITTED`.
   - Missing metadata caused trades to display `release_reason: UNKNOWN`.
   - Naive fallback logic previously assigned `fill_type="EXIT"` with missing reasons to `TRAIL`, introducing cohort pollution and false attribution risk.

2. **Session Parameter Overwrite (Configuration Mutation Bug)**:
   - Synchronizing Day (`futures.yaml`) and Night (`futures_night.yaml`) session configurations via Dashboard save treated session-specific strategy parameters (e.g. `atr_multiplier_stop`) as global invariants, silently overwriting night session settings.

3. **Trading Day Rollover Table Disappearance**:
   - In TAIFEX accounting rules, at 15:00:00 (Night session open), the trading day advances to `T+1` (e.g. `2026-07-28`).
   - Querying strictly by the active trading day when night session had 0 completed trades caused daytime completed trades (from calendar date `2026-07-27`) to be omitted from the UI table.

4. **Missing Policy J Live State Visibility**:
   - Policy J (Combined UPL Trail) activated after net PnL reached 200 TWD, but operators lacked real-time visibility into the peak PnL and exact pullback trigger line.

---

## 2. Decision Outcomes & Governance Rules

### 2.1 5-Level Provenance Hierarchy (Reason Evidence Classification)
`resolve_exit_reason()` and `resolve_release_reason()` in `scripts/generate_daily_report.py` enforce a strict 5-level evidence provenance hierarchy:

| Level | Provenance Name | Source / Criteria | Fallback Policy |
|---|---|---|---|
| **Level 1** | `EXPLICIT_EVENT` | Explicit event contract (`EXIT_REMAINING`, `EMERGENCY_FLATTEN`, `MANUAL`) | Highest priority |
| **Level 2** | `ORDER_METADATA` | Producer event contract in `ORDER_SUBMITTED` (`exit_reason`, `release_reason`) | Validated order metadata |
| **Level 3** | `LIFECYCLE_STATE` | `PositionLifecycle` state or shadow summary | Lifecycle decision log |
| **Level 4** | `NARROW_TRAIL_INFERENCE` | Single-leg + trail warmed up + trail triggered | Constrained inference |
| **Level 5** | `INSUFFICIENT_EVIDENCE` | Insufficient evidence available | Preserves `UNKNOWN`, **NEVER** fabricates `TRAIL` |

### 2.2 Leg-Role Reason Separation
- **`release_reason`**: Reasons for first-leg hedge release (e.g., `RELEASE_STOP`, `ATR_DYNAMIC`, `FIXED_FALLBACK`).
- **`exit_reason`**: Reasons for final-leg position exit (e.g., `TRAIL`, `STOP`, `COMBINED_EXIT`, `MANUAL`).

### 2.3 Cross-Session Taxonomy
Each trade record tracks session transition boundaries:
- `entry_session`: `day` / `night`
- `release_session`: `day` / `night`
- `exit_session`: `day` / `night`
- `cross_session_trade`: `bool` (`True` if entry, release, or exit span across different sessions).

### 2.4 Dual-Config Parameter Taxonomy (`SESSION_SPECIFIC_MTS_PARAMS`)
`ui/dashboard.py` enforces explicit parameter classification:
```python
SESSION_SPECIFIC_MTS_PARAMS = {"atr_multiplier_stop", "atr_multiplier_trail"}
```
- **Shared Parameters** (e.g., `min_atr`, `combined_upl_activation_net_pnl_twd`): Synchronized across counterpart configs (`futures.yaml` $\leftrightarrow$ `futures_night.yaml`).
- **Session-Specific Parameters**: Preserved independently for Day and Night sessions during Dashboard saves.

### 2.5 Unified Log Parsing & Rollover Fallback Protocol
- `ui/dashboard.py` delegates directly to `scripts.generate_daily_report.parse_logs` to guarantee 100% logic parity.
- When current trading day has 0 completed trades (e.g. night session rollover), the Dashboard automatically falls back to calendar date (`datetime.datetime.now().strftime("%Y-%m-%d")`) or recent historical trades.

### 2.6 Policy J Live Notification Banner
When Policy J is enabled and position is held:
- **`ARMED & TRACKING`**: Displayed when `Peak Net Exit PnL >= activation_twd` (200 TWD). Banner shows:
  - Peak Net Exit PnL (e.g. `+1,620 TWD`)
  - Trigger Line: `Peak - giveback_twd` (e.g. `+1,570 TWD`)
- **`MONITORING`**: Displayed when active UPL is below activation threshold.

---

## 3. Verification & Compliance

1. **Dual-Config Unit Tests (`tests/ui/test_ui_dual_config_sync.py`)**:
   - `7 passed` (Cases 1~7).

2. **Attribution Governance Unit Tests (`tests/reporting/test_reporting_attribution_governance.py`)**:
   - `10 passed` (Cases 1~10).

3. **Live System Verification**:
   - Mini PM2 processes (`dashboard` PID `85479`, `trading-system` PID `82142`) online and clean.
