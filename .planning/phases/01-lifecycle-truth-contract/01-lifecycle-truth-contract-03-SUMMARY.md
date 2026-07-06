# Plan 03 Summary

## Objective

Move futures position mutation onto confirmed deal handling only, so submit-time intent/order tracking no longer changes `PaperTrader.position` before execution is actually confirmed.

## Completed

- Added pending lifecycle metadata in `FuturesMonitor` so submit-time `intent_id` / `order_id` context survives until the confirmed deal arrives
- Introduced `_apply_confirmed_futures_deal()` as the only lifecycle callback path that applies futures fills into `PaperTrader.execute_signal(...)`
- Updated `_submit_order_via_manager()` so both live and paper submit paths create lifecycle orders first and defer position/PnL mutation until confirmed deal callbacks
- Routed partial and terminal futures fills through the same confirmed-deal handler, preserving fee/tax-inclusive exit math and avoiding optimistic submit-side state changes
- Added regression coverage for live submit-without-position-change, incremental partial fills, and confirmed partial exits with persisted `deal_id` linkage

## Files Modified

- `strategies/futures/monitor.py`
- `tests/test_order_lifecycle/test_position_apply_on_confirmed_deal.py`
- `tests/test_order_lifecycle/test_system_lifecycle.py`

## Verification

- `python3 -m pytest tests/test_order_lifecycle/test_position_apply_on_confirmed_deal.py tests/test_order_lifecycle/test_system_lifecycle.py -v`
- `python3 -m pytest tests/test_order_recovery.py::test_futures_monitor_records_exit_lifecycle_without_restart tests/test_order_lifecycle/test_position_apply_on_confirmed_deal.py tests/test_order_lifecycle/test_system_lifecycle.py -v`
- `python3 -m pytest tests/ -v`

## Notes

- Submit-time lifecycle orders now carry reason/comment context so restart recovery and dashboard exports still see entry/exit intent details.
- Confirmed deal handling deduplicates by `deal_id` when available, preventing duplicate broker callbacks from double-applying position changes.
