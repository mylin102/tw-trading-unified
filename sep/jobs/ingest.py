"""
SEP Ingestion Job (Hourly Continuous Ingestion)
Author: Gemini CLI
Date: 2026-07-23
"""

import sys
import os
from pathlib import Path
from datetime import datetime

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.deployment_role_gate import assert_research_allowed
from core.research_inbox import process_inbox_bundle, IngestionState
from sep.notification import queue_notification, Priority


def run_ingest_job() -> dict:
    """Executes the hourly continuous dataset ingestion pipeline on Air4."""
    # 1. Deployment Role Fail-Closed Check
    dep_id = assert_research_allowed(REPO_ROOT)

    execution_id = f"SEP-INGEST-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{dep_id}"
    
    # 2. Process Inbox Bundle State Machine
    state, msg, meta = process_inbox_bundle(
        staging_dir=REPO_ROOT / "data" / "inbox" / ".staging",
        datasets_dir=REPO_ROOT / "data" / "datasets",
        quarantine_dir=REPO_ROOT / "data" / "quarantine"
    )

    result = {
        "execution_id": execution_id,
        "timestamp": datetime.now().astimezone().isoformat(),
        "state": state.value if hasattr(state, "value") else str(state),
        "message": msg,
        "metadata": meta
    }

    # 3. Queue P0 Alert if quarantined or failed
    if state == IngestionState.QUARANTINED:
        queue_notification(
            priority=Priority.P0_ALERT,
            subject=f"[SEP][P0 Alert] Dataset Bundle Quarantined - {meta.get('build_id', 'unknown')}",
            body=f"Execution ID: {execution_id}\nReason: {msg}\nMetadata: {meta}",
            report_execution_id=execution_id
        )

    return result
