"""2026-10-12 Codex: mutable PAPER configuration is not release source code.

An installed runtime profile is shared by MTS and dashboard, even when their
code releases differ. LIVE and non-default futures profiles remain sealed in
their original source trees. Missing PAPER profile means legacy baseline;
an invalid installed profile is an error, never a silent fallback.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import tempfile

import yaml


def paper_profile_path() -> Path | None:
    root = os.environ.get("TRADING_RUNTIME_DIR")
    return Path(root).resolve() / "config/futures.paper.yaml" if root else None


def validate_paper_profile(body: bytes, *, ticker: str | None = None) -> dict:
    try:
        cfg = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        raise ValueError("PAPER_PROFILE_INVALID_YAML") from exc
    if (not isinstance(cfg, dict) or cfg.get("live_trading") is not False
            or not isinstance(cfg.get("ticker"), str) or not cfg["ticker"]
            or (ticker is not None and cfg["ticker"] != ticker)):
        raise ValueError("PAPER_PROFILE_MODE_OR_PRODUCT_MISMATCH")
    try:
        width = cfg.get("mts", {}).get("params", {}).get("max_spread_width")
    except AttributeError as exc:
        raise ValueError("PAPER_PROFILE_INVALID_PARAMS") from exc
    if width is not None and (isinstance(width, bool)
                              or not isinstance(width, (int, float))
                              or not math.isfinite(width) or width <= 0):
        raise ValueError("PAPER_PROFILE_INVALID_WIDTH")
    return cfg


def resolve_futures_profile(path: str | Path) -> Path:
    path = Path(path)
    shared = paper_profile_path()
    # Do not redirect sealed LIVE, alternate products, or arbitrary test files.
    if path.name != "futures.yaml" or shared is None or not shared.exists():
        return path
    if shared.is_symlink() or shared.resolve() != shared:
        raise ValueError("PAPER_PROFILE_PATH_NOT_RUNTIME_OWNED")
    baseline = yaml.safe_load(path.read_bytes()) if path.exists() else {}
    if baseline is not None and not isinstance(baseline, dict):
        raise ValueError("PAPER_PROFILE_INVALID_BASELINE")
    validate_paper_profile(shared.read_bytes(), ticker=(baseline or {}).get("ticker"))
    return shared


def write_paper_profile(path: Path, body: bytes, *, ticker: str,
                        runtime_dir: Path | None = None) -> None:
    """Validate before atomically replacing a runtime-owned profile; no PM2/Git."""
    path = Path(path)
    expected = (Path(runtime_dir).resolve() / "config/futures.paper.yaml"
                if runtime_dir is not None else paper_profile_path())
    if expected is None or path.resolve() != expected or path.is_symlink():
        raise ValueError("PAPER_PROFILE_PATH_NOT_RUNTIME_OWNED")
    validate_paper_profile(body, ticker=ticker)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".futures.paper.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
