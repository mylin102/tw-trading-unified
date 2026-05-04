"""
Shared email notification infrastructure.

All trade notifications go through this module regardless of asset class.
Formatters are registered by name (options, futures, stock) and dispatched
by the caller.

Architecture:

    notifier.py              transport layer — SMTP, retry, dispatch
        schema               contract — TradeEvent, RegimeContext, PositionSnapshot
        formatter/*          domain presentation — reads monitor/position/PnL

    Rules:
      - notifier ONLY delivers. Never touches position, PnL, or product logic.
      - formatter ONLY formats. Reads domain objects, returns subject + body.
      - schema is the contract between layers.

Usage:

    from core.notification import notify_trade_event

    notify_trade_event(
        event=trade_event,
        formatter="options",
        monitor=monitor_instance,
    )

Production principle:
  Notification is not source of truth.
  Ledger persistence is source of truth.
  Notify only after the ledger write has been confirmed.
"""

import logging
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from core.notification.schemas import TradeEvent
from core.notification.formatters.options_formatter import OptionsFormatter
from core.notification.formatters.futures_formatter import FuturesFormatter

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Formatter registry
# ──────────────────────────────────────────────────────────────

_FORMATTERS = {
    "options": OptionsFormatter(),
    "futures": FuturesFormatter(),
}


def _get_formatter(name: str):
    formatter = _FORMATTERS.get(name)
    if formatter is None:
        raise KeyError(
            f"Unknown notification formatter '{name}'. "
            f"Available: {list(_FORMATTERS.keys())}"
        )
    return formatter


# ──────────────────────────────────────────────────────────────
# SMTP config (lazy-loaded)
# ──────────────────────────────────────────────────────────────

_smtp_config: Optional[dict] = None


def _load_smtp_config() -> dict:
    global _smtp_config
    if _smtp_config is not None:
        return _smtp_config

    env_path = Path(os.path.expanduser("~/.config/squeeze-backtest-email.env"))
    if env_path.exists():
        load_dotenv(str(env_path))

    cfg = {
        "server": os.getenv("SMTP_SERVER", "smtp.gmail.com"),
        "port": int(os.getenv("SMTP_PORT", 587)),
        "username": os.getenv("SMTP_USERNAME", ""),
        "password": os.getenv("SMTP_PASSWORD", ""),
        "recipient": os.getenv("SMTP_RECIPIENT", ""),
    }

    if not cfg["username"] or not cfg["password"] or not cfg["recipient"]:
        logger.warning(
            "SMTP not configured: missing SMTP_USERNAME, SMTP_PASSWORD, "
            "or SMTP_RECIPIENT in %s", env_path
        )
        cfg = None

    _smtp_config = cfg
    return _smtp_config


# ──────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────

def notify_trade_event(
    event: TradeEvent,
    formatter: str = "options",
    **context: Any,
) -> bool:
    """Format and send a trade event notification.

    Args:
        event: TradeEvent with trade_id, action, side, price, quantity.
        formatter: Formatter name ("options", "futures", etc.).
        **context: Passed to the formatter's build method. Typically
                   includes monitor, position, regime, portfolio.

    Returns:
        True if sent successfully, False otherwise.
    """
    formatter_obj = _get_formatter(formatter)

    try:
        payload = formatter_obj.build(event, **context)
        subject = formatter_obj.format_subject(payload)
        body = formatter_obj.format_body(payload)
    except Exception as e:
        logger.error(
            "NOTIFICATION_FORMATTER_FAILURE formatter=%s event=%s error=%s",
            formatter, event, e, exc_info=True,
        )
        return False

    return _send_email(subject, body)


def notify_raw(subject: str, body: str) -> bool:
    """Send a raw email without formatting (for legacy callers)."""
    return _send_email(subject, body)


# ──────────────────────────────────────────────────────────────
# SMTP send (internal)
# ──────────────────────────────────────────────────────────────

def _send_email(subject: str, body_text: str, body_html: Optional[str] = None) -> bool:
    cfg = _load_smtp_config()
    if cfg is None:
        logger.warning("Email not sent: SMTP not configured")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = cfg["username"]
        msg["To"] = cfg["recipient"]
        msg["Subject"] = subject

        msg.attach(MIMEText(body_text, "plain"))
        if body_html:
            msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(cfg["server"], cfg["port"]) as server:
            server.starttls()
            server.login(cfg["username"], cfg["password"])
            server.send_message(msg)

        logger.info("Email sent: subject=%s recipient=%s", subject, cfg["recipient"])
        return True
    except Exception as e:
        logger.error("Email send failed: subject=%s error=%s", subject, e, exc_info=True)
        return False
