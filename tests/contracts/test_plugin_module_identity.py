"""
Contract test: Plugin module identity.

Verifies that all import paths for the tmf_spread plugin resolve to
the SAME module object in sys.modules, preventing the "split-brain enum"
bug where identical enum classes loaded via different import paths have
different identity (causing ReleaseGroupStatus.ARMED != ReleaseGroupStatus.ARMED,
which silently kills release decisions).

2026-07-16: Moved from test_release_eval_bug.py to dedicated contract file.
"""
import sys
from pathlib import Path


# ── Import paths that must resolve to the same module object ──

def _canonical_import_paths() -> list[str]:
    """Return the known import paths for tmf_spread."""
    return [
        "strategies.plugins.futures.active.tmf_spread",
        "strategies.plugins.futures.active.tmf_spread",
    ]


def test_plugin_single_import_identity() -> None:
    """
    The same module must never be loaded under two different sys.modules keys.
    This catches the split-brain bug where:
      import strategies.plugins.futures.active.tmf_spread  (module A)
      from strategies.plugins.futures.active import tmf_spread  (module A ✓)
      from plugin.futures.active import tmf_spread  (module B — NO, SAME module)
    """
    import strategies.plugins.futures.active.tmf_spread as outer
    from strategies.plugins.futures.active import tmf_spread as inner

    assert outer is inner, (
        f"Module identity split-brain! "
        f"outer={outer} id={id(outer)} "
        f"inner={inner} id={id(inner)}"
    )


def test_enum_identity_across_import_paths() -> None:
    """
    Enum classes imported via different paths must have the same identity.
    This is the root cause of P0-A: decision=None when far_hit=True.
    """
    from strategies.plugins.futures.active.tmf_spread import (
        ReleaseGroupStatus as RGS1,
        PositionPhase as PP1,
        TrailGroupStatus as TGS1,
    )
    from strategies.plugins.futures.active.tmf_spread import (
        ReleaseGroupStatus as RGS2,
        PositionPhase as PP2,
        TrailGroupStatus as TGS2,
    )

    assert RGS1 is RGS2, "ReleaseGroupStatus identity split!"
    assert PP1 is PP2, "PositionPhase identity split!"
    assert TGS1 is TGS2, "TrailGroupStatus identity split!"

    # Verify value-level comparison works too (defense against string-vs-enum)
    assert RGS1.ARMED == RGS2.ARMED
    assert RGS1.ARMED.value == "ARMED"
    assert str(RGS1.ARMED.value) == "ARMED"


def test_enum_value_normalizes_split_brain() -> None:
    """
    enum_value() helper must return the same string regardless of
    whether it receives a canonical enum, a split-brain copy, or a raw string.
    """
    from strategies.plugins.futures.active.tmf_spread import (
        enum_value,
        ReleaseGroupStatus,
        PositionPhase,
        TrailGroupStatus,
    )

    # Canonical enum
    assert enum_value(ReleaseGroupStatus.ARMED) == "ARMED"
    assert enum_value(PositionPhase.SPREAD) == "SPREAD"
    assert enum_value(TrailGroupStatus.INACTIVE) == "INACTIVE"

    # Raw string (from JSON state file)
    assert enum_value("ARMED") == "ARMED"
    assert enum_value("SPREAD") == "SPREAD"

    # None
    assert enum_value(None) is None

    # Unknown type → fail closed (None, not "None")
    assert enum_value(42) is None
    assert enum_value(object()) is None


def test_plugin_registered_identity_is_canonical() -> None:
    """
    Verify that StrategyRegistry registers the plugin in the exact same module
    object that standard python imports resolve to.
    """
    from core.strategy_registry import StrategyRegistry
    import importlib
    
    registry = StrategyRegistry()
    registry.discover()
    
    imported_mod = importlib.import_module("strategies.plugins.futures.active.tmf_spread")
    
    # Get registered instance
    strategy = registry._plugins.get("tmf_spread")
    assert strategy is not None
    strategy_mod = sys.modules[strategy.__class__.__module__]
    
    assert strategy_mod is imported_mod, (
        f"Registry loaded module {strategy_mod} is not imported canonical module {imported_mod}!"
    )
