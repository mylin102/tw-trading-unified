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
            for py_file in sorted(dir_path.glob("*.py")):
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
            spec = importlib.util.spec_from_file_location(py_file.stem, py_file)
            if spec is None or spec.loader is None:
                self._errors[py_file.stem] = "Could not load spec"
                return
            mod = importlib.util.module_from_spec(spec)
            sys.modules[py_file.stem] = mod
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
                self._plugins[name] = instance
                self._metadata[name] = {**instance.metadata, "name": name, "file": str(py_file)}
                logger.debug("Registered strategy: %s", name)
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
