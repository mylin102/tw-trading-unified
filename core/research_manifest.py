"""
Strategy Evaluation Platform (SEP) - Research Manifest Module
Author: Gemini CLI
Date: 2026-07-23

Provides immutable provenance and reproducibility metadata for all research artifacts,
replay experiments, and statistical inference reports.
"""

import sys
import os
import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, Any


def compute_file_hash(filepath: Path) -> str:
    """Computes SHA-256 hash of a dataset or code file."""
    if not filepath.exists():
        return "file_not_found"
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def get_git_info(repo_root: Path) -> Dict[str, Any]:
    """Retrieves current Git commit SHA and dirty status."""
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True
        ).strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=repo_root, text=True
        ).strip()
        is_dirty = len(status) > 0
        return {
            "git_commit": commit,
            "git_dirty": is_dirty,
            "git_dirty_file_count": len(status.splitlines()) if is_dirty else 0
        }
    except Exception as e:
        return {
            "git_commit": "unknown",
            "git_dirty": True,
            "error": str(e)
        }


def read_deployment_target(repo_root: Path) -> Dict[str, Any]:
    """Reads .deployment-target identity file if present."""
    target_file = repo_root / ".deployment-target"
    if target_file.exists():
        try:
            with open(target_file) as f:
                return json.load(f)
        except Exception:
            pass
    return {"deployment_id": "unknown"}


def generate_research_manifest(
    research_id: str,
    dataset_path: Path,
    policy_version: str = "v1.0",
    execution_semantics_version: str = "ADR-011",
    statistical_method_version: str = "Bootstrap-Wilcoxon-v1",
    random_seed: int = 42,
    bootstrap_seed: int = 42,
    doe_seed: int = 42,
    extra_metadata: Dict[str, Any] = None
) -> Dict[str, Any]:
    """Generates an immutable Research Manifest JSON for reproducibility governance."""
    repo_root = Path(__file__).resolve().parent.parent
    git_info = get_git_info(repo_root)
    deployment_info = read_deployment_target(repo_root)
    dataset_hash = compute_file_hash(dataset_path)

    manifest = {
        "schema_version": "SEP-Governance-1.0",
        "research_id": research_id,
        "generated_at": datetime.now().astimezone().isoformat(),
        "deployment_identity": {
            "deployment_id": deployment_info.get("deployment_id", "unknown"),
            "instance_id": deployment_info.get("instance_id", "unknown"),
            "host_role": deployment_info.get("host_role", "unknown")
        },
        "version_provenance": {
            "git_commit": git_info["git_commit"],
            "git_dirty": git_info["git_dirty"],
            "dataset_version": dataset_path.name,
            "dataset_sha256": dataset_hash,
            "replay_engine_version": "R-004.5-SEP",
            "policy_version": policy_version,
            "execution_semantics_version": execution_semantics_version,
            "statistical_method_version": statistical_method_version
        },
        "reproducibility_seeds": {
            "random_seed": random_seed,
            "bootstrap_seed": bootstrap_seed,
            "doe_seed": doe_seed
        },
        "extra_metadata": extra_metadata or {}
    }
    return manifest
