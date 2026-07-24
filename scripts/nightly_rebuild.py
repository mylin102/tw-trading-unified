#!/usr/bin/env python3
"""
nightly_rebuild.py — Production nightly rebuild of trade dataset.

Schedule: 06:00 Asia/Taipei (via cron)
Lock:     fcntl.flock (kernel auto-release on crash)
Staging:  built with publish=False, then rename → atomic symlink swap
Safety:   never touches 'current' on failure; source mutation detection

Exit codes:
  0 — success (published new generation)
  1 — skipped (locked by another process)
  2 — failure (validation/parse/write error, current unchanged)
  3 — source mutation detected during build
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# macOS Silicon optimization
if sys.platform == "darwin":
    os.system(f"taskpolicy -b -p {os.getpid()}")

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.trade_dataset import (
    BASE_DIR,
    EVENTS_LOG,
    FILLS_LOG,
    _file_sha256,
    _make_build_id,
    rebuild as _build_core,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

LOCK_FILE = BASE_DIR / ".nightly_rebuild.lock"
LOG_DIR = BASE_DIR / "rebuild_logs"
BUILD_LOG = LOG_DIR / "build_history.jsonl"

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("nightly_rebuild")


def _source_fingerprint(path: Path) -> dict:
    if not path.exists():
        return {"path": str(path), "exists": False}
    return {
        "path": str(path),
        "exists": True,
        "size": path.stat().st_size,
        "mtime": path.stat().st_mtime,
        "sha256": _file_sha256(path),
    }


def _detect_mutation(before: dict, after: dict) -> bool:
    return before.get("sha256") != after.get("sha256")


def nightly_rebuild() -> int:
    """Run the nightly rebuild with full safety guards. Returns exit code."""
    started_at = datetime.now(timezone.utc)
    build_id = _make_build_id()

    # Log header
    logger.info("=" * 60)
    logger.info("Nightly Rebuild %s", build_id)
    logger.info("  UTC:   %s", started_at.isoformat())
    logger.info("  Taipei: %s", started_at.astimezone().isoformat())
    logger.info("=" * 60)

    # ------------------------------------------------------------------
    # 1. Acquire lock
    # ------------------------------------------------------------------
    try:
        lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.warning("Another rebuild in progress — skipping (REBUILD_SKIPPED_LOCKED)")
        return 1
    except Exception as e:
        logger.error("Lock acquisition failed: %s", e)
        return 2

    try:
        return _run_with_lock(build_id, lock_fd)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        except Exception:
            pass


def _run_with_lock(build_id: str, lock_fd: int) -> int:
    started_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # 2. Fingerprint sources (before)
    # ------------------------------------------------------------------
    fills_before = _source_fingerprint(FILLS_LOG)
    events_before = _source_fingerprint(EVENTS_LOG)
    logger.info("Fills:   %s (%d bytes)", fills_before.get("sha256","?")[:12], fills_before.get("size",0))
    logger.info("Events:  %s (%d bytes)", events_before.get("sha256","?")[:12], events_before.get("size",0))

    # ------------------------------------------------------------------
    # 3. Build into staging generation (publish=False)
    #    The core rebuild writes to output_dir/generations/<build_id>/
    #    but does NOT touch the 'current' symlink.
    # ------------------------------------------------------------------
    # Use the build_id as-is; it will live in data/generations/<build_id>/
    try:
        manifest = _build_core(
            fills_path=FILLS_LOG,
            events_path=EVENTS_LOG,
            output_dir=BASE_DIR,
            build_id=build_id,
            publish=False,    # ← staging: don't touch current symlink
        )
    except Exception as e:
        logger.error("Rebuild failed: %s", e)
        _cleanup(build_id)
        return 2

    gen_dir = BASE_DIR / "generations" / build_id
    if not gen_dir.exists():
        logger.error("Staging generation not found: %s", gen_dir)
        return 2

    # ------------------------------------------------------------------
    # 4. Source mutation detection (after)
    # ------------------------------------------------------------------
    fills_after = _source_fingerprint(FILLS_LOG)
    events_after = _source_fingerprint(EVENTS_LOG)

    fills_changed = _detect_mutation(fills_before, fills_after)
    events_changed = _detect_mutation(events_before, events_after)

    if fills_changed or events_changed:
        logger.error(
            "SOURCE_CHANGED_DURING_BUILD: fills=%s events=%s",
            "changed" if fills_changed else "ok",
            "changed" if events_changed else "ok",
        )
        _cleanup(build_id)
        return 3

    # ------------------------------------------------------------------
    # 5. Atomic symlink swap
    # ------------------------------------------------------------------
    current_link = BASE_DIR / "current"
    tmp_link = BASE_DIR / ".current_tmp"
    try:
        if tmp_link.is_symlink() or tmp_link.exists():
            tmp_link.unlink()
        rel = os.path.relpath(gen_dir, BASE_DIR)
        tmp_link.symlink_to(rel)
        os.replace(tmp_link, current_link)
        logger.info("Published: current -> %s", build_id)
    except Exception as e:
        logger.error("Symlink publish failed: %s", e)
        if tmp_link.is_symlink() or tmp_link.exists():
            tmp_link.unlink()
        _cleanup(build_id)
        return 2

    # ------------------------------------------------------------------
    # 6. Log success
    # ------------------------------------------------------------------
    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    log_entry = {
        "build_id": build_id,
        "started_at_utc": started_at.isoformat(),
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": round(elapsed, 2),
        "exit_code": 0,
        "fills_sha256": fills_after.get("sha256"),
        "events_sha256": events_after.get("sha256"),
        "fills_size": fills_after.get("size"),
        "events_size": events_after.get("size"),
    }
    with open(BUILD_LOG, "a") as f:
        f.write(json.dumps(log_entry) + "\n")

    # Read manifest for summary
    with open(gen_dir / "manifest.json") as f:
        m = json.load(f)
    logger.info("Published: %d facts, %d snapshots, %d decisions, %d outcomes",
                m["row_counts"].get("trade_facts", 0),
                m["row_counts"].get("trade_snapshots", 0),
                m["row_counts"].get("trade_decisions", 0),
                m["row_counts"].get("trade_outcomes", 0))
    logger.info("Content hash: %s", m.get("dataset_content_hash", "?")[:16])
    logger.info("Nightly rebuild complete (%.1fs)", elapsed)
    return 0


def _cleanup(build_id: str):
    """Remove failed generation directory."""
    gen_dir = BASE_DIR / "generations" / build_id
    if gen_dir.exists():
        shutil.rmtree(gen_dir, ignore_errors=True)
        logger.info("Cleaned up failed generation: %s", build_id)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    exit_code = nightly_rebuild()
    sys.exit(exit_code)
