# Technical Debt & Future Work

- **Remove legacy backtest scripts:** Once `ui/backtest_dashboard.py` (port 8501) is fully functional and validated, delete all `scripts/backtest_*.py` files to keep the codebase DRY and avoid confusion about the authoritative backtest engine.
