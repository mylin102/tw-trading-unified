"""
Strategy Evaluation Platform (SEP) - Decoupled Notification Outbox Module
Author: Gemini CLI
Date: 2026-07-23

Handles P0 Immediate Alerts, P1 Daily Warnings, and P2 Periodic Reports.
Decouples research execution from email delivery using data/notification_outbox/.
"""

import sys
import os
import json
import hashlib
import smtplib
from pathlib import Path
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, Any, List, Tuple
from enum import Enum

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.notification.notifier import _load_smtp_config


class Priority(str, Enum):
    P0_ALERT = "P0_ALERT"
    P1_WARNING = "P1_WARNING"
    P2_REPORT = "P2_REPORT"


class NotificationState(str, Enum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    RETRYABLE_FAILED = "RETRYABLE_FAILED"
    PERMANENT_FAILED = "PERMANENT_FAILED"


def queue_notification(
    priority: Priority,
    subject: str,
    body: str,
    outbox_dir: Path = None,
    report_execution_id: str = None
) -> Dict[str, Any]:
    """Enqueues a notification message into data/notification_outbox/pending/."""
    if outbox_dir is None:
        outbox_dir = REPO_ROOT / "data" / "notification_outbox"

    pending_dir = outbox_dir / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    content_hash = hashlib.sha256((subject + body).encode("utf-8")).hexdigest()[:12]
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    notif_id = f"NOTIF_{priority.value}_{timestamp_str}_{content_hash}"

    payload = {
        "notification_id": notif_id,
        "priority": priority.value,
        "subject": subject,
        "body": body,
        "report_execution_id": report_execution_id or f"EXEC-{timestamp_str}",
        "queued_at": datetime.now().astimezone().isoformat(),
        "attempt_count": 0,
        "max_attempts": 5,
        "status": NotificationState.PENDING.value,
        "last_error": None
    }

    out_file = pending_dir / f"{notif_id}.json"
    with open(out_file, "w") as f:
        json.dump(payload, f, indent=2)

    return payload


def send_email_smtp(subject: str, body: str) -> Tuple[bool, str]:
    """Delivers an email using SMTP credentials from ~/.config/squeeze-backtest-email.env."""
    cfg = _load_smtp_config()
    if not cfg:
        return False, "SMTP credentials missing in ~/.config/squeeze-backtest-email.env"

    try:
        msg = MIMEMultipart()
        msg["From"] = cfg["username"]
        msg["To"] = cfg["recipient"]
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP(cfg["server"], cfg["port"], timeout=15) as server:
            server.starttls()
            server.login(cfg["username"], cfg["password"])
            server.send_message(msg)

        return True, f"Sent to {cfg['recipient']}"
    except Exception as e:
        return False, str(e)


def dispatch_notification_outbox(outbox_dir: Path = None) -> Dict[str, Any]:
    """Processes pending items in notification outbox and attempts SMTP delivery."""
    if outbox_dir is None:
        outbox_dir = REPO_ROOT / "data" / "notification_outbox"

    pending_dir = outbox_dir / "pending"
    sent_dir = outbox_dir / "sent"
    failed_dir = outbox_dir / "failed"

    pending_dir.mkdir(parents=True, exist_ok=True)
    sent_dir.mkdir(parents=True, exist_ok=True)
    failed_dir.mkdir(parents=True, exist_ok=True)

    pending_files = list(pending_dir.glob("*.json"))
    results = {"processed": len(pending_files), "sent": 0, "failed": 0, "items": []}

    for p_file in pending_files:
        try:
            with open(p_file) as f:
                data = json.load(f)
        except Exception:
            continue

        data["attempt_count"] = data.get("attempt_count", 0) + 1
        data["status"] = NotificationState.SENDING.value

        success, msg = send_email_smtp(data["subject"], data["body"])

        if success:
            data["status"] = NotificationState.SENT.value
            data["sent_at"] = datetime.now().astimezone().isoformat()
            dst = sent_dir / p_file.name
            with open(dst, "w") as f:
                json.dump(data, f, indent=2)
            p_file.unlink()
            results["sent"] += 1
            results["items"].append({"id": data["notification_id"], "status": "SENT", "info": msg})
        else:
            data["last_error"] = msg
            if data["attempt_count"] >= data.get("max_attempts", 5):
                data["status"] = NotificationState.PERMANENT_FAILED.value
                dst = failed_dir / p_file.name
            else:
                data["status"] = NotificationState.RETRYABLE_FAILED.value
                dst = pending_dir / p_file.name

            with open(dst, "w") as f:
                json.dump(data, f, indent=2)

            results["failed"] += 1
            results["items"].append({"id": data["notification_id"], "status": data["status"], "error": msg})

    return results
