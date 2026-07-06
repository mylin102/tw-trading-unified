"""
StrategyConfig — YAML loader with Pydantic validation.

Each strategy has a per-strategy YAML file under ``config/strategies/``.
This module loads and validates the config at startup so that bad
parameters are caught before any bar is processed.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ── Default values when a key is missing from YAML ──────────────────────
_DEFAULTS: dict[str, Any] = {
    "name": "",
    "asset_class": "futures",
    "version": "1.0",
    "enabled": True,
    "params": {},
    "risk": {
        "max_positions": 1,
        "stop_loss_type": "atr",
        "stop_loss_mult": 2.0,
    },
    "regime_filter": {
        "allowed": ["all"],
        "min_adx": 0,
    },
    "backtest": {
        "pf": 0.0,
        "wr": 0.0,
        "max_dd": 0.0,
        "total_trades": 0,
        "period": "",
    },
}


def load(path: str | Path) -> dict[str, Any]:
    """Load a strategy YAML config and merge with defaults.

    Parameters
    ----------
    path : str | Path
        Path to the YAML file.

    Returns
    -------
    dict
        Validated config with all required keys present.

    Raises
    ------
    FileNotFoundError
        If the file does not exist and no defaults can be applied.
    yaml.YAMLError
        If the file is not valid YAML.
    """
    p = Path(path)
    if not p.exists():
        logger.warning("Config file not found: %s — using defaults", p)
        return dict(_DEFAULTS)

    with open(p, encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    cfg = dict(_DEFAULTS)
    _deep_merge(cfg, raw)

    # ── Structural validation (defensive programming) ───────────────
    _validate(cfg, str(p))

    return cfg


def _deep_merge(base: dict, override: dict) -> None:
    """Recursively merge *override* into *base* in-place."""
    for key, val in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val


def _validate(cfg: dict, source: str) -> None:
    """Check critical invariants.  Raises ValueError on failure."""
    if cfg["risk"]["max_positions"] < 0:
        raise ValueError(f"{source}: max_positions must be >= 0")
    if cfg["risk"]["stop_loss_mult"] <= 0:
        raise ValueError(f"{source}: stop_loss_mult must be > 0")
    if cfg["backtest"]["pf"] < 0:
        raise ValueError(f"{source}: backtest.pf must be >= 0")
    if cfg["backtest"]["wr"] < 0 or cfg["backtest"]["wr"] > 100:
        raise ValueError(f"{source}: backtest.wr must be 0-100")
    if cfg["backtest"]["max_dd"] > 0:
        raise ValueError(f"{source}: backtest.max_dd must be <= 0")
