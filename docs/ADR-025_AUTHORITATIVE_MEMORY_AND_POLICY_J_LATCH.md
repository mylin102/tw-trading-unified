# ADB-025: Authoritative Memory Architecture & Monotonic Policy J Latch

## Status
Accepted / Implemented (2026-07-31)

## Context
During live execution, a data direction inversion bug was identified where runtime strategy state (such as Policy J Peak Net Exit PnL) was being overwritten by disk state reads during live tick processing. When disk state files were missing fields or stale, runtime memory was corrupted, causing latched ARMED states to revert to MONITORING.

## Decision

1. **Sole Runtime Authority**: Live strategy RAM is the sole authority for strategy state during LIVE_EXECUTION.
2. **Write-Only Projections**: Runtime projections (telemetry, dashboard, notification) are strictly write-only outputs from the strategy engine (RAM -> Disk).
3. **Startup-Only Recovery**: Disk state snapshots may be consumed only during explicit STARTUP_RECOVERY.
4. **Reconciliation for Reconnects**: Broker reconnects (LIVE_RECONCILIATION) use selective fact reconciliation (positions, fills, orders), NOT generic snapshot reloads. Strategy state (Peak PnL, ARMED status) is never overwritten during reconnects.
5. **Broker Fact Supremacy**: Broker confirmed positions and internal fill ledgers outrank disk state snapshots for execution-fact reconstruction.
6. **Monotonic Policy J State Machine**: Policy J transitions monotonically (MONITORING -> ARMED -> TRIGGERED -> COMPLETED). Reverse transitions (ARMED -> MONITORING or TRIGGERED -> ARMED)=