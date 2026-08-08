"""Replay entrypoint — research-only.

The engine is implemented module-by-module; main() is a no-op guard that
does NOT run historical artifacts (explicitly out of scope until the
engine review passes).
"""


def main(argv=None):
    """Entrypoint guard: refuses to execute a replay without explicit
    authorization (returns 0 without touching artifacts)."""
    return 0
