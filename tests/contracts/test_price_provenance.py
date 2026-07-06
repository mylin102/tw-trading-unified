"""
Contract: Price provenance — every price used in execution must have a
declared source, and live mode must reject non-live prices.

This ensures the price_source traceability is enforced at runtime in tests
and statically in contract scanning.
"""
from pathlib import Path


# ── All valid price sources in the system ──
VALID_PRICE_SOURCES = {
    "LIVE_TICK",
    "PAPER_SIM",
    "HISTORICAL_BAR",
    "FLAG_FALLBACK",
    "SYNTHETIC_CONFIG",
    "BACKFILL_BAR",
    "INDICATOR_CSV",
    "MTS_STATE",
    "MISSING",
    "UNSET",
}

# ── Price sources only allowed in paper/dry-run mode ──
PAPER_ONLY_SOURCES = {
    "FLAG_FALLBACK",
    "SYNTHETIC_CONFIG",
}

# ── Files that define or assign price_source ──
PRICE_SOURCE_FILES = [
    "strategies/futures/monitor.py",
    "strategies/plugins/futures/active/tmf_spread.py",
]


def test_price_source_is_from_valid_set():
    """
    All price_source assignments must use one of the VALID_PRICE_SOURCES.
    This prevents ad-hoc sources like "AUTO" or "DEFAULT".
    """
    root = Path(__file__).parent.parent.parent
    failures = []

    for rel_path in PRICE_SOURCE_FILES:
        abs_path = root / rel_path
        if not abs_path.exists():
            failures.append(f"MISSING: {rel_path}")
            continue

        text = abs_path.read_text()
        lines = text.split("\n")
        for lineno, line in enumerate(lines, 1):
            # Match: _price_source = "SOME_VALUE" or price_source = "SOME_VALUE"
            stripped = line.strip()
            if "price_source" not in stripped:
                continue
            if "=" not in stripped:
                continue
            # Extract the string value after =
            parts = stripped.split("=", 1)
            if len(parts) < 2:
                continue
            rhs = parts[1].strip().strip('"').strip("'")
            # Skip non-string assignments (variables, f-strings)
            if rhs.startswith("f") or rhs.startswith("self.") or rhs.startswith("_"):
                continue
            if rhs in VALID_PRICE_SOURCES:
                continue
            # Check if it's part of a VALID_PRICE_SOURCES set literal
            if rhs.startswith("{") or rhs == "VALID_PRICE_SOURCES":
                continue

            failures.append(
                f"{rel_path}:{lineno}: invalid price_source '{rhs}' — "
                f"must be one of {sorted(VALID_PRICE_SOURCES)}"
            )

    assert not failures, (
        "Invalid price_source detected. "
        "All price sources must be from the canonical set.\n\n"
        + "\n".join(failures)
    )


def test_paper_only_sources_not_used_in_live_paths():
    """
    Paper-only sources (FLAG_FALLBACK, SYNTHETIC_CONFIG) must be guarded
    by a paper/dry-run check before assignment.
    """
    root = Path(__file__).parent.parent.parent
    failures = []

    for rel_path in PRICE_SOURCE_FILES:
        abs_path = root / rel_path
        if not abs_path.exists():
            continue

        text = abs_path.read_text()
        lines = text.split("\n")
        for lineno, line in enumerate(lines, 1):
            for source in PAPER_ONLY_SOURCES:
                if source not in line:
                    continue
                # Check that this line is preceded by a paper/dry-run guard
                # Look back up to 10 lines for the guard
                start = max(0, lineno - 12)
                context_lines = lines[start:lineno]
                context = "\n".join(context_lines)
                guard_patterns = [
                    "not self.live_trading",
                    "self.dry_run",
                    "paper",
                    "PAPER",
                ]
                if not any(g in context for g in guard_patterns):
                    failures.append(
                        f"{rel_path}:{lineno}: {source} used without "
                        f"paper/dry-run guard"
                    )

    assert not failures, (
        "Paper-only price sources used in potentially live path. "
        "Sources like FLAG_FALLBACK and SYNTHETIC_CONFIG must be guarded "
        "by a paper/dry-run check.\n\n"
        + "\n".join(failures)
    )
