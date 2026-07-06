"""
Session Config Manager — handles day/night config split and atomic write-back.

Usage:
    from core.session_config import SessionConfig
    cfg = SessionConfig.load("day")     # Loads futures_day.yaml
    cfg = SessionConfig.load("night")   # Loads futures_night.yaml
    cfg.set("risk_mgmt.stop_loss_pts", 70)
    cfg.save()  # Atomic write to futures_day.yaml
"""
from __future__ import annotations

import copy
import shutil
import tempfile
import yaml
from pathlib import Path
from datetime import datetime
from typing import Any

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

_SESSION_FILES = {
    "day": CONFIG_DIR / "futures_day.yaml",
    "night": CONFIG_DIR / "futures_night.yaml",
}
_FALLBACK = CONFIG_DIR / "futures.yaml"


def _get_nested(d: dict, key: str) -> Any:
    """Get nested dict value by dot-separated key."""
    parts = key.split(".")
    for p in parts:
        if isinstance(d, dict):
            d = d.get(p, None)
        else:
            return None
    return d


def _set_nested(d: dict, key: str, value: Any) -> None:
    """Set nested dict value by dot-separated key."""
    parts = key.split(".")
    for p in parts[:-1]:
        if p not in d or not isinstance(d[p], dict):
            d[p] = {}
        d = d[p]
    d[parts[-1]] = value


class SessionConfig:
    """Manages a single session's config with atomic save."""

    def __init__(self, session: str, data: dict):
        self.session = session
        self._data = data
        self._file = _SESSION_FILES.get(session, _FALLBACK)

    @classmethod
    def load(cls, session: str) -> "SessionConfig":
        """Load config for session. Falls back to futures.yaml if session file missing."""
        f = _SESSION_FILES.get(session, _FALLBACK)
        if f.exists():
            with open(f) as fh:
                data = yaml.safe_load(fh) or {}
        elif _FALLBACK.exists():
            with open(_FALLBACK) as fh:
                data = yaml.safe_load(fh) or {}
        else:
            data = {}
        return cls(session, data)

    def get(self, key: str, default: Any = None) -> Any:
        """Get config value by dot-separated key."""
        val = _get_nested(self._data, key)
        return val if val is not None else default

    def set(self, key: str, value: Any) -> None:
        """Set config value by dot-separated key."""
        _set_nested(self._data, key, value)

    @property
    def data(self) -> dict:
        """Read-only snapshot."""
        return copy.deepcopy(self._data)

    def save(self, backup: bool = True) -> Path:
        """
        Atomic save: write to temp file, then rename.
        Creates backup if backup=True.
        Returns the path written.
        """
        self._file.parent.mkdir(parents=True, exist_ok=True)

        if backup and self._file.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = self._file.with_suffix(f".yaml.bak.{ts}")
            shutil.copy2(self._file, backup_path)

        # Atomic write: temp file + rename
        fd, tmp_path = tempfile.mkstemp(dir=str(self._file.parent), suffix=".yaml.tmp")
        try:
            with open(fd, "w") as f:
                yaml.dump(self._data, f, default_flow_style=False, sort_keys=False)
            self._file.unlink(missing_ok=True)
            Path(tmp_path).rename(self._file)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise

        return self._file

    def reload(self) -> None:
        """Reload from disk."""
        new = self.load(self.session)
        self._data = new._data
