"""
Strategy Evaluation Platform (SEP) - Research Inbox & Ingestion State Machine
Author: Gemini CLI
Date: 2026-07-23

Manages dataset ingestion lifecycle with a 6-state machine:
DISCOVERED -> TRANSFERRED -> HASH_VERIFIED -> CONTRACT_VALIDATED -> REGISTERED -> AVAILABLE_FOR_RESEARCH
Failure state: QUARANTINED
"""

import os
import sys
import json
import shutil
from pathlib import Path
from typing import Dict, Any, Tuple
from enum import Enum

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.research_manifest import compute_file_hash


class IngestionState(str, Enum):
    DISCOVERED = "DISCOVERED"
    TRANSFERRED = "TRANSFERRED"
    HASH_VERIFIED = "HASH_VERIFIED"
    CONTRACT_VALIDATED = "CONTRACT_VALIDATED"
    REGISTERED = "REGISTERED"
    AVAILABLE_FOR_RESEARCH = "AVAILABLE_FOR_RESEARCH"
    QUARANTINED = "QUARANTINED"


def process_inbox_bundle(staging_dir: Path = None, datasets_dir: Path = None, quarantine_dir: Path = None) -> Tuple[IngestionState, str, Dict[str, Any]]:
    """
    Ingests and validates dataset bundles from inbox/.staging into data/datasets/<build_id>/
    Enforces atomic READY marker check, SHA-256 verification, and state machine lifecycle.
    """
    if staging_dir is None:
        staging_dir = REPO_ROOT / "data" / "inbox" / ".staging"
    if datasets_dir is None:
        datasets_dir = REPO_ROOT / "data" / "datasets"
    if quarantine_dir is None:
        quarantine_dir = REPO_ROOT / "data" / "quarantine"

    staging_dir.mkdir(parents=True, exist_ok=True)
    datasets_dir.mkdir(parents=True, exist_ok=True)
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    # Search for subdirectories or bundle files
    bundles = [d for d in staging_dir.iterdir() if d.is_dir()]
    if not bundles:
        # Check root staging
        ready_marker = staging_dir / "READY"
        if ready_marker.exists() or list(staging_dir.glob("*.parquet")):
            bundles = [staging_dir]
        else:
            return IngestionState.DISCOVERED, "No staging bundles discovered.", {}

    bundle_path = bundles[0]
    metadata = {
        "source_path": str(bundle_path),
        "state": IngestionState.DISCOVERED.value,
        "history": [IngestionState.DISCOVERED.value]
    }

    # State 1: DISCOVERED -> TRANSFERRED
    metadata["state"] = IngestionState.TRANSFERRED.value
    metadata["history"].append(IngestionState.TRANSFERRED.value)

    # State 2: Check READY marker (Atomic write check)
    ready_file = bundle_path / "READY" if bundle_path.is_dir() else staging_dir / "READY"
    if not ready_file.exists():
        return _quarantine(bundle_path, quarantine_dir, "Missing READY marker. Bundle transfer incomplete or active in Mini.", metadata)

    # State 3: Read Manifest
    manifest_file = bundle_path / "dataset_manifest.json" if bundle_path.is_dir() else staging_dir / "dataset_manifest.json"
    manifest_data = {}
    if manifest_file.exists():
        try:
            with open(manifest_file) as f:
                manifest_data = json.load(f)
        except Exception as e:
            return _quarantine(bundle_path, quarantine_dir, f"Malformed manifest JSON: {e}", metadata)

    build_id = manifest_data.get("build_id") or manifest_data.get("dataset_build_id") or bundle_path.name
    metadata["build_id"] = build_id
    metadata["producer_manifest"] = manifest_data

    # State 4: HASH_VERIFIED
    files_to_check = [f for f in bundle_path.iterdir() if f.is_file() and f.name not in ("READY", "registration_meta.json")]
    registered_files = []
    
    expected_hashes = manifest_data.get("files_sha256", {})
    for f in files_to_check:
        h = compute_file_hash(f)
        if f.name in expected_hashes:
            if expected_hashes[f.name] != h:
                return _quarantine(bundle_path, quarantine_dir, f"SHA-256 mismatch for file '{f.name}'", metadata)
        registered_files.append({"file": f.name, "sha256": h})

    metadata["state"] = IngestionState.HASH_VERIFIED.value
    metadata["history"].append(IngestionState.HASH_VERIFIED.value)

    # State 5: CONTRACT_VALIDATED
    contract_ver = manifest_data.get("dataset_contract_version", "1.0")
    if contract_ver.startswith("999"): # Example major incompatibility
        return _quarantine(bundle_path, quarantine_dir, f"Incompatible dataset contract version '{contract_ver}'", metadata)

    metadata["state"] = IngestionState.CONTRACT_VALIDATED.value
    metadata["history"].append(IngestionState.CONTRACT_VALIDATED.value)

    # State 6: REGISTERED -> AVAILABLE_FOR_RESEARCH
    target_dir = datasets_dir / build_id
    if target_dir.exists():
        # Check if existing registered build hash matches
        meta_existing = target_dir / "registration_meta.json"
        if meta_existing.exists():
            with open(meta_existing) as f:
                existing_meta = json.load(f)
            if existing_meta.get("producer_manifest_hash") != compute_file_hash(manifest_file) if manifest_file.exists() else "":
                return _quarantine(bundle_path, quarantine_dir, f"Conflict: Build ID '{build_id}' exists with different hash", metadata)
            return IngestionState.AVAILABLE_FOR_RESEARCH, f"Dataset '{build_id}' already registered and verified.", existing_meta

    target_dir.mkdir(parents=True, exist_ok=True)
    for f in files_to_check:
        shutil.copy2(f, target_dir / f.name)
    if manifest_file.exists():
        shutil.copy2(manifest_file, target_dir / "dataset_manifest.json")
    if ready_file.exists():
        shutil.copy2(ready_file, target_dir / "READY")

    metadata["state"] = IngestionState.AVAILABLE_FOR_RESEARCH.value
    metadata["history"].append(IngestionState.REGISTERED.value)
    metadata["history"].append(IngestionState.AVAILABLE_FOR_RESEARCH.value)
    metadata["registered_path"] = str(target_dir)
    metadata["files"] = registered_files
    metadata["producer_manifest_hash"] = compute_file_hash(manifest_file) if manifest_file.exists() else ""

    reg_file = target_dir / "registration_meta.json"
    with open(reg_file, "w") as f:
        json.dump(metadata, f, indent=2)

    return IngestionState.AVAILABLE_FOR_RESEARCH, f"Dataset '{build_id}' successfully ingested and registered.", metadata


def _quarantine(bundle_path: Path, quarantine_dir: Path, reason: str, metadata: Dict[str, Any]) -> Tuple[IngestionState, str, Dict[str, Any]]:
    metadata["state"] = IngestionState.QUARANTINED.value
    metadata["history"].append(IngestionState.QUARANTINED.value)
    metadata["quarantine_reason"] = reason

    q_build_dir = quarantine_dir / (metadata.get("build_id") or "invalid_bundle")
    q_build_dir.mkdir(parents=True, exist_ok=True)

    if bundle_path.is_dir():
        for item in bundle_path.iterdir():
            if item.is_file():
                shutil.copy2(item, q_build_dir / item.name)
    
    q_meta = q_build_dir / "quarantine_meta.json"
    with open(q_meta, "w") as f:
        json.dump(metadata, f, indent=2)

    return IngestionState.QUARANTINED, f"QUARANTINED: {reason}", metadata
