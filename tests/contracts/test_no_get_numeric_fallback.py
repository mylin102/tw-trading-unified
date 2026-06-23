"""
Contract: No numeric fallback in .get() calls on execution paths.

Any `.get(key, numeric_literal)` pattern in execution, fill, exit, spread,
price handling, or manual trade paths is forbidden. Prices must come from
live tick data or explicitly marked fallback sources.

Uses AST parsing so price changes (e.g. 41800 → 40500) don't silently bypass.
"""
import ast
from pathlib import Path


# ── Source path patterns: execution paths that must never numeric-fallback ──
# NOTE: Config .get(key, default) calls and display-only .get(key, 0) are EXCLUDED.
# Only price/execution-related .get() calls with numeric fallbacks that could
# cause wrong trade entry/exit are forbidden.
EXECUTION_FILE_PATTERNS = [
    "strategies/plugins/futures/active/tmf_spread.py",  # Spread entry/exit pricing
]

# ── .get() keys that represent prices or execution amounts — these must not have numeric fallbacks ──
EXECUTION_PRICE_KEYS = {
    "near_close", "far_close",
    "near_entry", "far_entry",
    "near_last", "far_last",
    "entry_price", "exit_price",
    "avg_fill_price", "filled_price",
    "price", "close",
    "near_upl", "far_upl", "total_upl",
    "near_realized_pnl", "far_realized_pnl", "total_realized_pnl",
    "spread_z", "manual_pnl",
    "trail_peak", "trail_nadir", "trail_stop_price", "distance_to_stop",
}

# ── Allowed get() call patterns (by variable name or context) ──
ALLOWED_GET_KEYS_WITH_DEFAULTS = {
    # Config reads (these are settings, not prices)
    "get(\"live_trading\"", "get(\"dry_run\"", "get(\"max_holding_days\"",
    "get(\"cooldown_bars\"", "get(\"max_drawdown_pct\"", "get(\"margin_per_lot\"",
    "get(\"initial_balance\"", "get(\"atr_period\"", "get(\"synthetic_near_price\"",
    "get(\"allow_synthetic_price\"", "get(\"max_capital_per_trade\"",
    "get(\"position_size\"", "get(\"min_dte_to_exit\"",
    # Time/date defaults (not prices)
    "get(\"ts\"", "get(\"trading_day\"", "get(\"_updated\"",
    "get(\"session\"", "get(\"order_id\"",
    # String metadata (not prices)
    "get(\"action\"", "get(\"side\"", "get(\"reason\"", "get(\"strategy\"",
    "get(\"symbol\"", "get(\"status\"", "get(\"note\"", "get(\"direction\"",
    "get(\"label\"", "get(\"comment\"", "get(\"price_source\"",
    # Non-price numerical config
    "get(\"threshold\"", "get(\"timeout\"", "get(\"retries\"",
    "get(\"max_restarts\"", "get(\"age\"",
    # Dict/list defaults (not prices)
    "get(\"error\"", "get(\"result\"", "get(\"meta\"", "get(\"context\"",
    "get(\"data\"", "get(\"name\"", "get(\"type\"",
}


def _has_numeric_default(call_node: ast.Call) -> bool:
    """Check if a .get() call has a numeric literal as second argument."""
    if len(call_node.args) < 2:
        return False
    second = call_node.args[1]
    # int, float, or negative numbers (UnaryOp + Num)
    if isinstance(second, (ast.Constant, ast.Num)):
        if isinstance(second.value, (int, float)):
            return True
    if isinstance(second, ast.UnaryOp) and isinstance(second.op, ast.USub):
        if isinstance(second.operand, (ast.Constant, ast.Num)):
            if isinstance(second.operand.value, (int, float)):
                return True
    return False


def _get_first_arg_text(call_node: ast.Call) -> str:
    """Get the string representation of the first .get() argument."""
    if call_node.args:
        if isinstance(call_node.args[0], ast.Constant):
            return str(call_node.args[0].value)
        if isinstance(call_node.args[0], ast.Str):
            return call_node.args[0].s
    return ""


def _is_pytest_file(path: Path) -> bool:
    """Check if path is inside a test directory."""
    return "test_" in path.name or "/tests/" in str(path)


def test_no_numeric_fallback_in_get_calls():
    """
    Forbid `.get(key, <numeric_literal>)` in execution paths.

    Prices must come from live tick data or explicitly marked fallback sources,
    never from a silent default in a .get() call.
    """
    root = Path(__file__).parent.parent.parent
    failures = []

    # Collect all Python files in execution paths
    files_to_check = []
    for pattern in EXECUTION_FILE_PATTERNS:
        path = root / pattern
        if path.is_file():
            files_to_check.append(path)
        elif path.is_dir():
            files_to_check.extend(path.rglob("*.py"))

    # Deduplicate and filter test files
    seen = set()
    for path in sorted(files_to_check, key=lambda p: str(p)):
        if str(path) in seen:
            continue
        seen.add(str(path))

        if _is_pytest_file(path):
            continue

        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            failures.append(f"SYNTAX ERROR: {path.relative_to(root)}")
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "get":
                continue
            if not _has_numeric_default(node):
                continue

            first_key = _get_first_arg_text(node)
            # Only flag if this key is a price/execution value
            if first_key in ALLOWED_GET_KEYS_WITH_DEFAULTS:
                continue
            if any(first_key.startswith(k) for k in ALLOWED_GET_KEYS_WITH_DEFAULTS):
                continue
            if first_key not in EXECUTION_PRICE_KEYS:
                continue  # Non-price keys are fine (config defaults, metadata, etc.)

            lineno = node.lineno
            failures.append(
                f"{path.relative_to(root)}:{lineno}: "
                f".get() with numeric default '{first_key}' — "
                f"use explicit price source instead"
            )

    assert not failures, (
        "Numeric fallback in .get() detected in execution path.\n"
        "Rule: Prices must come from live tick data or explicitly marked "
        "fallback sources.\n"
        "Use .get(key) and handle None explicitly, or use a named constant.\n\n"
        + "\n".join(failures)
    )
