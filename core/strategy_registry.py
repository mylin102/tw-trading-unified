"""
StrategyRegistry — auto-discovery and hot-swap management.

Scans ``strategies/plugins/{futures,options}/`` for Python modules,
imports each one, and registers the first ``StrategyBase`` subclass it
finds.  Import errors are caught and logged so that a broken plugin
does not crash the entire system.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Type

from core.strategy_base import StrategyBase
from core.strategy_config import load as load_config

logger = logging.getLogger(__name__)

# Default plugin directory relative to project root
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "strategies" / "plugins"


class StrategyRegistry:
    """Discovers, loads, and serves strategy plugins."""

    def __init__(self, plugin_root: Path | str | None = None):
        self._plugins: dict[str, StrategyBase] = {}
        self._metadata: dict[str, dict[str, Any]] = {}
        self._errors: dict[str, str] = {}
        self._config_dir: Path | None = None
        self._plugin_root = Path(plugin_root) if plugin_root else _PLUGIN_ROOT

    # ── Discovery ────────────────────────────────────────────────────────

    def discover(self, config_dir: Path | str | None = None) -> None:
        """Scan plugin directories and register all valid strategies."""
        self._config_dir = Path(config_dir) if config_dir else None
        self._plugins.clear()
        self._metadata.clear()
        self._errors.clear()

        for asset_dir in ("futures", "options"):
            dir_path = self._plugin_root / asset_dir
            if not dir_path.is_dir():
                continue
            # GSD: Search recursively (rglob) to support subdirectories (active/experimental/deprecated)
            for py_file in sorted(dir_path.rglob("*.py")):
                if py_file.name.startswith("_"):
                    continue
                self._try_register_file(py_file, asset_dir)

        logger.info(
            "StrategyRegistry: discovered %d plugin(s), %d error(s)",
            len(self._plugins),
            len(self._errors),
        )

    def _try_register_file(self, py_file: Path, asset_class: str) -> None:
        """Import a plugin file directly (no package path needed) and register it."""
        try:
            # GSD: Use unique module name to avoid collision across subdirectories
            # format: plugin.futures.active.squeeze_fire_scout
            rel_path = py_file.relative_to(self._plugin_root)
            module_name = "plugin." + ".".join(rel_path.with_suffix("").parts)
            
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                self._errors[py_file.stem] = "Could not load spec"
                return
            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)
        except Exception as exc:
            self._errors[py_file.stem] = str(exc)
            logger.warning("Plugin import failed — %s: %s", py_file.stem, exc)
            return

        # Find the first StrategyBase subclass
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, StrategyBase)
                and attr is not StrategyBase
            ):
                instance = attr()
                name = instance.name
                if name in self._plugins:
                    logger.warning("Duplicate strategy name '%s' found in %s (ignoring)", name, py_file)
                    return
                self._plugins[name] = instance
                self._metadata[name] = {**instance.metadata, "name": name, "file": str(py_file)}
                logger.info("Registered strategy: %s from %s", name, rel_path)
                return

        logger.warning("No StrategyBase subclass found in %s", py_file)

    # ── Accessors ────────────────────────────────────────────────────────

    def get(self, name: str) -> StrategyBase | None:
        """Return a strategy instance by name, or ``None``."""
        return self._plugins.get(name)

    def list_all(self) -> list[dict[str, Any]]:
        """Return metadata for every discovered strategy."""
        result = []
        for name, meta in self._metadata.items():
            entry = dict(meta)
            entry["available"] = True
            result.append(entry)
        for name, err in self._errors.items():
            result.append({
                "name": name,
                "available": False,
                "error": err,
            })
        return result

    def load_config(self, name: str) -> dict[str, Any]:
        """Load the per-strategy YAML config for *name*.

        Falls back to defaults if the file is missing.
        """
        if self._config_dir is None:
            return {}
        cfg_path = self._config_dir / f"{name}.yaml"
        return load_config(cfg_path)

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def errors(self) -> dict[str, str]:
        """Mapping of plugin name → error message for failed imports."""
        return dict(self._errors)


# ── Phase 1: Day/Night Performance Lookup ───────────────────────────────
# Source: backtest results from exports/vbt_counter_sweep.csv and session-specific analysis.
# Populated from backtests; future: auto-updated from live performance.

STRATEGY_PERF: dict[str, dict[str, float]] = {
    "counter_vwap":        {"day_pf": 2.1, "night_pf": 1.4},
    "spring_upthrust":     {"day_pf": 1.6, "night_pf": 1.3},
    "kbar_feature":        {"day_pf": 2.5, "night_pf": 1.8},      # 新增，基於初步回測
    "calendar_condor_v2":  {"day_pf": 7.39, "night_pf": 0.0},     # 新增，夜盤不交易
    "vol_squeeze":         {"day_pf": 1.5, "night_pf": 1.2},
    "psar":                {"day_pf": 1.4, "night_pf": 0.9},
    "weak_bear_trend":     {"day_pf": 1.2, "night_pf": 1.0},  # 新增，WEAK regime 空頭策略
    "weak_bull_trend":     {"day_pf": 1.0, "night_pf": 1.0},  # 新增，WEAK regime 防守型多頭 (初始 PF=1.0，等待 live 驗證)
    "squeeze_fire_scout":  {"day_pf": 1.0, "night_pf": 1.0},  # 新增，SQUEEZE regime scout
}

# Regime → preferred strategy order (same order for day/night, PF filter applied at runtime)
REGIME_STRATEGY_ORDER: dict[str, list[str]] = {
    "trending":  ["counter_vwap", "vol_squeeze", "psar"],
    "ranging":   ["spring_upthrust", "counter_vwap", "vol_squeeze"],
    "volatile":  ["vol_squeeze", "counter_vwap", "spring_upthrust"],
    "low_vol":   ["vol_squeeze", "counter_vwap", "spring_upthrust"],
    "shock":     [],  # No strategies in shock regime
}

# WEAK regime bias-aware routing (used by futures_strategy_router)
WEAK_BIAS_STRATEGY_MAP: dict[str, str] = {
    "SHORT":   "weak_bear_trend",
    "BULLISH": "weak_bull_trend",
}

DEFAULT_MIN_PF = 1.0  # Strategies below this PF are excluded from rankings


def select_best_strategy(session_type: str, regime: str = "trending", min_pf: float | None = None) -> str:
    """
    Select the best strategy for a given session type and market regime.

    Args:
        session_type: "day" or "night"
        regime: market regime (trending, ranging, volatile, low_vol, shock)
        min_pf: minimum PF threshold (default: DEFAULT_MIN_PF)

    Returns:
        Best strategy name, or "counter_vwap" as fallback.
    """
    pf_key = f"{session_type}_pf"
    threshold = min_pf if min_pf is not None else DEFAULT_MIN_PF

    # Try regime-ordered list first
    order = REGIME_STRATEGY_ORDER.get(regime, REGIME_STRATEGY_ORDER.get("trending", []))

    candidates = []
    for strat in order:
        perf = STRATEGY_PERF.get(strat, {})
        pf = perf.get(pf_key, 0)
        if pf >= threshold:
            candidates.append((strat, pf))

    # Sort by PF descending
    candidates.sort(key=lambda x: x[1], reverse=True)

    if candidates:
        return candidates[0][0]

    # Fallback: scan all strategies
    all_candidates = [
        (name, perf.get(pf_key, 0))
        for name, perf in STRATEGY_PERF.items()
        if perf.get(pf_key, 0) >= threshold
    ]
    all_candidates.sort(key=lambda x: x[1], reverse=True)

    return all_candidates[0][0] if all_candidates else "counter_vwap"


def get_strategy_ranking(session_type: str, min_pf: float | None = None) -> list[tuple[str, float]]:
    """
    Return ranked list of strategies for a session type, sorted by PF.

    Returns:
        List of (strategy_name, pf) tuples, filtered by min_pf threshold.
    """
    pf_key = f"{session_type}_pf"
    threshold = min_pf if min_pf is not None else DEFAULT_MIN_PF

    ranking = [
        (name, perf.get(pf_key, 0))
        for name, perf in STRATEGY_PERF.items()
        if perf.get(pf_key, 0) >= threshold
    ]
    ranking.sort(key=lambda x: x[1], reverse=True)
    return ranking
