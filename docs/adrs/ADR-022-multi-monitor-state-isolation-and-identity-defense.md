# ADR-022: Multi-Monitor State File Isolation & Fail-Closed Identity Defense

* **Status**: Accepted
* **Date**: 2026-07-23
* **Authors**: Antigravity AI Team / Trading Systems Engineering
* **Scope**: `strategies/futures/monitor.py`, `strategies/plugins/futures/active/tmf_spread.py`

---

## 1. Context & Incident Analysis

### 1.1 The Incident
During live trading on the `mini` host running concurrent `TMF` and `MTX` monitors in a single process (`main.py --config futures,futures_mtx`), an automated entry order filled (`MTS_ENTRY`: SHORT 44644 / LONG 44855). However, immediately after fill confirmation, the system status degraded to `FLAT / WAITING_FOR_SIGNAL`, logging:

```text
⚠️ [POSITION_AUTHORITY] State file says FLAT but strategy has _has_position=True
— force-syncing strategy to FLAT
```

This triggered an infinite loop where `Fills-led recovery` restored memory position to `True`, followed by `POSITION_AUTHORITY` reading disk (`FLAT`) and wiping the position back to `FLAT`.

### 1.2 Root Cause Analysis
The failure was traced to **Runtime Context Leakage & Module-Level Singleton Collision**:

```text
TMF Monitor / MTX Monitor (Same Process)
        │
        ├─ Thread-Local injects instance state path:
        │    TMF → /tmp/mts_position_state.json
        │    MTX → /tmp/mts_position_state_futures_mtx.json
        │
        └─ tmf_spread.py writes via module-level constant _MTS_STATE_FILE
                         │
                         ▼
             MTX writes state into TMF file
                         │
                         ▼
        MTX own file remains has_position: false
                         │
                         ▼
         POSITION_AUTHORITY detects desync
                         │
                         ▼
         _reset("POSITION_AUTHORITY_FLAT") wipes position
```

1. **State Path Misalignment**: `tmf_spread.py` contained a hardcoded module constant `_MTS_STATE_FILE = os.getenv("MTS_STATE_PATH", "/tmp/mts_position_state.json")`. When MTX executed `_write_mts_state()`, it overwrote TMF's state file instead of its own, leaving `/tmp/mts_position_state_futures_mtx.json` stuck at `has_position: false`.
2. **Authority Lock Desync**: `POSITION_AUTHORITY` in `FuturesMonitor` read MTX's disk file (`has_position: false`), saw strategy memory `_has_position == True`, and triggered a force-reset.
3. **Lifecycle Label Ambiguity**: When both legs were held (`released_leg is None`), line 3210 in `tmf_spread.py` evaluated `action=f"TRAILING_{self._side}"` to `"TRAILING_None"`, producing a semantic contradiction with `release_state: BOTH_HELD`.

---

## 2. Decision & Architecture Principles

To eliminate multi-instance state file collisions and prevent context leakage, we adopt a **3-Layer Defense-in-Depth Architecture**:

```text
Layer 1: Instance-Scoped Path Resolution (_get_state_file_path)
        ↓
Layer 2: Identity Metadata Injection (ticker, monitor_id)
        ↓
Layer 3: Authority-Side Fail-Closed Identity Validation
```

### 2.1 Dynamic State Path Resolution
* Removed hardcoded reliance on static `_MTS_STATE_FILE` in `_write_mts_state()`, `_write_mts_telemetry()`, and `_read_mts_state()`.
* Introduced `_get_state_file_path()` in `tmf_spread.py`:
  - Dynamically resolves `_mts_position_state_path()` via Thread-Local context.
  - Honors pytest monkeypatches during unit testing without leaking state across suites.

### 2.2 Identity Metadata & Fail-Closed Defense
* State JSON files now mandate identity fields:
  ```json
  {
    "ticker": "MTX",
    "monitor_id": "futures_mtx",
    "has_position": true
  }
  ```
* Hardened `POSITION_AUTHORITY` in `FuturesMonitor`:
  - If disk `ticker` mismatches `self.ticker`:
    ```python
    self._mts_entry_blocked = True
    self._mts_reconciliation_pending = True
    _authority_has_pos = True  # Prevent resetting in-memory position
    ```
  - **Fail-Closed**: Blocks new entry orders and flags pending reconciliation instead of blindly resetting strategy state.

### 2.3 Lifecycle Invariant Realignment
* Aligned action label formatting:
  - When `released_leg is None`: action label = `"SPREAD"` (Phase: `PositionPhase.SPREAD`, `BOTH_HELD`).
  - When `released_leg is not None`: action label = `f"TRAILING_{self._side}"` (`TRAILING_LONG` or `TRAILING_SHORT`).

---

## 3. Verification & Compliance

### 3.1 Unit & Contract Test Suite
* Added `tests/contracts/test_multi_monitor_interleaving.py`:
  - `test_multi_monitor_state_file_isolation`: Proves concurrent TMF/MTX writes remain 100% isolated.
  - `test_interleaved_tick_thread_local_scoping`: Verifies thread-local scope cleanup across tick iterations.
  - `test_identity_mismatch_fail_closed`: Verifies entry blocking and position preservation when identity mismatch occurs.
* Test suite results: `16 passed in 0.63s`.

### 3.2 Live Production Verification
* Deployed to `mini` host running dual `TMF,MTX` monitors.
* Verified state file `/tmp/mts_position_state.json` recovered trade `mts-auto-154700-685` (`SHORT 44644 / LONG 44855`) with `state: SPREAD`, `total_upl: +40.0 pts`.
* Zero `POSITION_AUTHORITY` desync warnings logged.

---

## 4. Lessons Learned

1. **Never Rely on Module-Level File Path Constants in Multi-Instance Processes**:
   Any module loaded in a multi-tenant process must resolve file paths dynamically from instance configuration or thread/execution context.
2. **Fail-Closed Over Silent Fallback**:
   A mismatch between authority identity and monitor runtime should enter a protected state (block entry, preserve position) rather than quietly skipping checks or swallowing exceptions.
3. **Validate Invariants at File Schema Boundaries**:
   State JSON files must embed identity metadata (`ticker`, `monitor_id`) so readers can verify authority ownership before acting on persistent state.
