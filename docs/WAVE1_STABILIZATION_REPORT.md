<!-- generated-by: gsd-doc-writer -->
# Wave 1 Stabilization Report

This report summarizes the architectural and operational improvements implemented during **Wave 1** of the Taiwan Trading Unified project. The primary focus of this wave was to establish a stable "Data Chain" and synchronize session logic across futures and options monitors.

## 1. Unified Session Classification Logic
Prior to Wave 1, session boundaries (Day vs. Night) were inconsistently handled across different modules. We have unified this logic in `core/date_utils.py` to ensure a single source of truth for trading days and session types.

- **Centralized Boundaries**: Fixed Day (08:45-13:45) and Night (15:00-05:00 next day) boundaries.
- **Trading Day Alignment**: `get_trading_day()` now correctly handles the 15:00 rollover to the next trading day, including holiday awareness.
- **Vectorized Support**: Added support for pandas Series and DatetimeIndex to ensure consistency between real-time processing and historical analysis.
- **Key Functions**:
  - `get_trading_day(dt)`: Returns the correct Taifex trading date.
  - `get_session(dt)`: Returns `1` (Day) or `2` (Night).
  - `is_night_session(dt)` / `is_day_session(dt)`: Boolean masks for session filtering.

## 2. Robust Futures Data Monitoring
The `FuturesMonitor` was enhanced to handle real-world connectivity issues and contract expirations without manual intervention.

- **Stale Contract Detection**: Implemented `_check_futures_contract_staleness()` which triggers if TMF (Taiwan Mid-cap Futures) ticks stop arriving for more than 120 seconds.
- **Auto-Recovery & Rollover**:
  - Automatically detects when a front-month contract has expired.
  - Switches to the next valid contract in the queue.
  - Forces re-subscription to the Shioaji quote service to clear hung connections.
- **Virtual Tick Generation**: Added a "Heartbeat" mechanism that generates virtual ticks from MTX (Micro Taiwan Index) data to keep bar-building alive during low-volume periods in the primary contract.

## 3. Options Monitor Synchronization
The `OptionsMonitor` (and its underlying `ShioajiOptionsSmartMonitor`) has been fully synchronized with the core architectural standards established for futures.

- **Unified Session Awareness**: Replaced fragmented time checks with the unified `is_day_session` and `is_night_session` logic.
- **Contract Expiry Guard**: Added validation to `find_best_contracts()` to reject contracts expiring today or earlier, preventing "Zero DTE" execution errors.
- **Indicator Continuity**: Improved the indicator logging to save snapshots every loop iteration, ensuring the dashboard always has the latest Greeks and MTF (Multi-Timeframe) scores.

## 4. Dashboard & Supervisor Integration
To improve the reliability of the system under a process supervisor (like `pm2` or a custom bash loop), we integrated a standardized restart mechanism.

- **`.restart` Flag Monitoring**: Both `FuturesMonitor` and `OptionsMonitor` now monitor for the existence of a `.restart` file in the project root.
- **Clean Shutdown**: Upon detecting the flag, the monitors exit their main loops gracefully, allowing the supervisor to pull the latest code or configurations and restart the processes.
- **Dashboard Sync**: This ensures that when a user triggers a "Restart" from the Streamlit UI, all background monitors synchronize their state and configurations.

## 5. Verification & Testing
The stability of these changes was verified using a comprehensive test suite in `tests/test_data_chain.py`.

### Test Summary:
- **Total Tests**: 39
- **Passing**: 39
- **Failing**: 0

**Key Test Areas**:
- `TestSessionDateStr`: Validated trading day rollovers and holiday skips.
- `TestSessionClassification`: Confirmed day/night boundary accuracy.
- `TestStaleDataDetection`: Verified the recovery logic for hung data streams.
- `TestPnLSilentFailurePrevention`: Ensured all exit actions correctly trigger PnL calculations with fee inclusion.

## 6. Conclusion
Wave 1 has successfully stabilized the core data infrastructure. The system is now resilient to session transitions, contract rollovers, and intermittent API data gaps. The unified session logic provides a solid foundation for the Wave 2 optimization of execution and risk management.

<!-- VERIFY: Ensure the .restart flag is consistently deleted by the supervisor script after monitors exit. -->
