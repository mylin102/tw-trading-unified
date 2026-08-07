"""Run manifest (design §7) — immutable, committed at a non-ignored path.

scripts/research/exit_attribution/reports/<run_id>/manifest.json
Raw CSVs may stay gitignored; the manifest is the reproducibility key.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Dict


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def git_head(repo_root: str, exclude_paths=None) -> dict:
    try:
        out = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        sha = out.stdout.strip()
        st = subprocess.run(
            ["git", "-C", repo_root, "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        if exclude_paths:
            for _p in exclude_paths:
                st = "\n".join(l for l in st.splitlines() if _p not in l)
        dirty = st.strip() != ""
        return {"commit": sha, "dirty": dirty}
    except Exception as exc:  # pragma: no cover - environment guard
        return {"commit": None, "dirty": None, "error": str(exc)}


def build_manifest(
    run_id: str,
    input_paths: Dict[str, str],
    schema_version: str,
    repo_root: str,
    fee_source_path: str = "",
    fee_effective_date: str = "",
    config: Dict = None,
    seed: str = "",
    dirty_exclude: tuple = (),
) -> dict:
    manifest = {
        "run_id": run_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "schema_version": schema_version,
        "input_sha256": {name: sha256_file(p) for name, p in input_paths.items()},
        "git": git_head(repo_root, exclude_paths=dirty_exclude),
        "fee_source": {
            "path": fee_source_path,
            "sha256": sha256_file(fee_source_path) if fee_source_path and os.path.exists(fee_source_path) else None,
            "effective_date": fee_effective_date,
        },
        "config": config or {},
        "seed": seed,
    }
    return manifest


def write_manifest(manifest: dict, report_dir: str) -> str:
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, "manifest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    return path


def verify_manifest(manifest: dict, input_paths: Dict[str, str]) -> dict:
    """Verify recorded input hashes against current files (T19)."""
    mismatches = {}
    for name, path in input_paths.items():
        recorded = manifest.get("input_sha256", {}).get(name)
        if recorded is None:
            mismatches[name] = "missing_in_manifest"
        else:
            current = sha256_file(path)
            if current != recorded:
                mismatches[name] = {"recorded": recorded, "current": current}
    return mismatches
