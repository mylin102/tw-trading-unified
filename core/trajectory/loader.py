# 2026-07-17 Gemini CLI: Implement Trajectory Loader with Schema Validation and Manifest Integrity Checking
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence, Tuple

import pandas as pd

from core.trajectory.contracts import (
    TrajectoryEvent,
    EventType,
    EventOrigin,
    EventAuthority,
    EventCausality,
    EventMutability,
    EventSource,
)
from core.trajectory.errors import (
    TrajectoryValidationError,
    DuplicateEventError,
    ManifestVerificationError,
    ReferenceIntegrityError,
)
from core.trajectory.validation import validate_event_dict

class TrajectoryLoader:
    """
    Loader responsible for reading trajectory events from JSONL or Parquet files,
    verifying schema constraints, enforcing reference integrity, and validating manifest hashes.
    """

    @staticmethod
    def verify_manifest(manifest_path: Path, base_dir: Path) -> None:
        """
        Verify the dataset manifest and content hash of source files.
        """
        if not manifest_path.exists():
            raise ManifestVerificationError(f"Manifest file not found: {manifest_path}")

        try:
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
        except Exception as e:
            raise ManifestVerificationError(f"Failed to parse manifest JSON: {e}")

        source_files = manifest.get("source_files", [])
        if not source_files:
            raise ManifestVerificationError("Manifest has no 'source_files' entry")

        for file_entry in source_files:
            rel_path = file_entry.get("path")
            expected_sha = file_entry.get("sha256")
            if not rel_path or not expected_sha:
                raise ManifestVerificationError("Invalid source file entry in manifest")

            target_file = base_dir / rel_path
            if not target_file.exists():
                # Try relative to the manifest directory itself
                target_file = manifest_path.parent / rel_path
                if not target_file.exists():
                    raise ManifestVerificationError(f"Dataset source file not found: {rel_path}")

            # Compute SHA-256
            sha256_hash = hashlib.sha256()
            try:
                with open(target_file, "rb") as f:
                    for byte_block in iter(lambda: f.read(65536), b""):
                        sha256_hash.update(byte_block)
            except Exception as e:
                raise ManifestVerificationError(f"Failed to read file {rel_path}: {e}")

            actual_sha = sha256_hash.hexdigest()
            if actual_sha != expected_sha:
                raise ManifestVerificationError(
                    f"Content hash mismatch for file '{rel_path}'. "
                    f"Expected: {expected_sha}, Actual: {actual_sha}"
                )

    @classmethod
    def load_from_jsonl(cls, file_path: Path) -> Tuple[TrajectoryEvent, ...]:
        """
        Load trajectory events from a JSON Lines file.
        """
        if not file_path.exists():
            raise FileNotFoundError(f"JSONL file not found: {file_path}")

        events = []
        event_ids = set()

        with open(file_path, "r") as f:
            for line_idx, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception as e:
                    raise TrajectoryValidationError(
                        f"Failed to parse JSON on line {line_idx} of {file_path.name}: {e}"
                    )

                # Validate dict schema and values
                validate_event_dict(data)

                # Duplicate ID check
                evt_id = data["event_id"]
                if evt_id in event_ids:
                    raise DuplicateEventError(f"Duplicate event_id detected: '{evt_id}'")
                event_ids.add(evt_id)

                # Create TrajectoryEvent DTO
                q_flags = tuple(data.get("quality_flags", []))
                event = TrajectoryEvent(
                    event_id=evt_id,
                    event_type=EventType(data["event_type"]),
                    event_time_ns=data["event_time_ns"],
                    receive_time_ns=data.get("receive_time_ns"),
                    source_sequence=data.get("source_sequence"),
                    source=EventSource(data["source"]),
                    session_id=data["session_id"],
                    trade_id=data.get("trade_id"),
                    origin=EventOrigin(data["origin"]),
                    authority=EventAuthority(data["authority"]),
                    causality=EventCausality(data["causality"]),
                    mutability=EventMutability(data["mutability"]),
                    payload_schema_version=data["payload_schema_version"],
                    payload=data["payload"],
                    quality_flags=q_flags,
                )
                events.append(event)

        # Reference integrity checking
        cls._verify_reference_integrity(events)

        return tuple(events)

    @classmethod
    def load_from_parquet(cls, file_path: Path) -> Tuple[TrajectoryEvent, ...]:
        """
        Load trajectory events from a Parquet file.
        """
        if not file_path.exists():
            raise FileNotFoundError(f"Parquet file not found: {file_path}")

        try:
            df = pd.read_parquet(file_path)
        except Exception as e:
            raise TrajectoryValidationError(f"Failed to parse Parquet file: {e}")

        events = []
        event_ids = set()

        for idx, row in df.iterrows():
            # Convert row to dict
            data = row.to_dict()

            # Normalize data types from pandas
            # payload can be a dict or a JSON string depending on storage
            payload = data.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    pass
            data["payload"] = payload

            # quality_flags can be an array, list, or string
            q_flags = data.get("quality_flags")
            if q_flags is None:
                q_flags = []
            elif isinstance(q_flags, str):
                try:
                    q_flags = json.loads(q_flags)
                except Exception:
                    q_flags = [q_flags]
            elif hasattr(q_flags, "tolist"):
                q_flags = q_flags.tolist()
            elif not isinstance(q_flags, (list, tuple)):
                try:
                    q_flags = list(q_flags)
                except Exception:
                    q_flags = [q_flags]
            data["quality_flags"] = q_flags

            # Normalize numpy ints/floats
            for key in ["event_time_ns", "receive_time_ns", "source_sequence"]:
                if key in data and pd.notna(data[key]):
                    data[key] = int(data[key])
                else:
                    data[key] = None

            if pd.isna(data.get("trade_id")):
                data["trade_id"] = None

            # Validate dict schema and values
            validate_event_dict(data)

            # Duplicate ID check
            evt_id = data["event_id"]
            if evt_id in event_ids:
                raise DuplicateEventError(f"Duplicate event_id detected: '{evt_id}'")
            event_ids.add(evt_id)

            # Create TrajectoryEvent DTO
            event = TrajectoryEvent(
                event_id=evt_id,
                event_type=EventType(data["event_type"]),
                event_time_ns=data["event_time_ns"],
                receive_time_ns=data["receive_time_ns"],
                source_sequence=data["source_sequence"],
                source=EventSource(data["source"]),
                session_id=data["session_id"],
                trade_id=data["trade_id"],
                origin=EventOrigin(data["origin"]),
                authority=EventAuthority(data["authority"]),
                causality=EventCausality(data["causality"]),
                mutability=EventMutability(data["mutability"]),
                payload_schema_version=data["payload_schema_version"],
                payload=data["payload"],
                quality_flags=tuple(q_flags),
            )
            events.append(event)

        # Reference integrity checking
        cls._verify_reference_integrity(events)

        return tuple(events)

    @staticmethod
    def _verify_reference_integrity(events: Sequence[TrajectoryEvent]) -> None:
        """
        Enforce session and trade reference integrity constraints across loaded events.
        """
        # Session ID must be non-empty for all events
        for event in events:
            if not event.session_id or not event.session_id.strip():
                raise ReferenceIntegrityError(
                    f"Event '{event.event_id}' has an empty or missing session_id"
                )

        # Gather all trade IDs referenced by endogenous or derived events
        referenced_trades = set()
        for event in events:
            if event.trade_id:
                referenced_trades.add(event.trade_id)

        # Trade ID setup validation:
        # Every trade ID referenced in endogenous/derived events must have a matching LIFECYCLE_TRANSITION setup event
        # (or similar trade facts mapping)
        initialized_trades = set()
        for event in events:
            if event.event_type == EventType.LIFECYCLE_TRANSITION:
                t_id = event.trade_id
                if t_id:
                    initialized_trades.add(t_id)

        # Check if there are dangling trade references
        dangling = referenced_trades - initialized_trades
        if dangling:
            # We raise ReferenceIntegrityError if a trade is referenced without initialization
            raise ReferenceIntegrityError(
                f"Referenced trade IDs {list(dangling)} have no corresponding lifecycle transition initialization"
            )
