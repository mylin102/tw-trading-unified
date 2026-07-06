"""
Contract: No magic price fallback in execution paths.

Any default price in execution, exit, fill, spread, or manual trade path
is forbidden unless explicitly test-only and guarded by paper/dry-run mode.

Rule: Manual flag is intent only, not a trusted price source.
Monitor must resolve prices from live tick data.
"""
from pathlib import Path


# ── Forbidden magic numbers that appeared in historical bugs ──
FORBIDDEN_VALUES = {
    "41800",  # Dashboard hardcoded fallback (May 2026)
    "41900",  # Dashboard hardcoded fallback (May 2026)
}

# ── Files that must not contain magic price fallbacks ──
SCANNED_FILES = [
    "ui/dashboard.py",
    "strategies/futures/monitor.py",
    "strategies/plugins/futures/active/tmf_spread.py",
]

# ── Allowed contexts where fallback values may legitimately appear ──
ALLOWED_CONTEXTS = [
    "test_",           # Test files (test-only)
    "# Test",          # Inline test comment
    "# CONFIG",        # Config initializers
    "synthetic_near_price",  # Config keys, not execution fallback
    "margin_per_lot",        # Margin amount, not price
    "initial_balance",       # Balance, not price
    "stop_loss",             # Stop level, not entry price
]


def test_no_magic_price_fallbacks_in_execution_paths():
    """Forbid hardcoded price values in execution paths unless in test-only context."""
    root = Path(__file__).parent.parent.parent
    failures = []

    for rel_path in SCANNED_FILES:
        abs_path = root / rel_path
        if not abs_path.exists():
            failures.append(f"MISSING: {rel_path}")
            continue

        text = abs_path.read_text()
        lines = text.split("\n")
        for lineno, line in enumerate(lines, 1):
            for value in FORBIDDEN_VALUES:
                if value not in line:
                    continue
                # Check if this line is in an allowed context
                if any(ctx in line for ctx in ALLOWED_CONTEXTS):
                    continue
                failures.append(
                    f"{rel_path}:{lineno}: magic fallback price {value} "
                    f"found in non-test context:\n  {line.strip()}"
                )

    assert not failures, (
        "Magic price fallback detected in execution path.\n"
        "Rule: Manual flag is intent only, not a trusted price source.\n"
        "Monitor must resolve prices from live tick data.\n\n"
        + "\n".join(failures)
    )
