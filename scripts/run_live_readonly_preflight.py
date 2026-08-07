#!/usr/bin/env python3
"""Execute one explicitly requested, no-order live broker preflight."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _api_factory():
    # Import here so tests and Dashboard rendering never import the broker SDK.
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=True)
    if os.environ.get("LIVE_PREFLIGHT_NO_ORDERS") != "1":
        raise RuntimeError("LIVE_PREFLIGHT_NO_ORDERS is required")
    import shioaji as sj
    from core.broker.shioaji_compat import safe_login

    api_key = os.environ.get("SHIOAJI_API_KEY")
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY")
    if not api_key or not secret_key:
        raise RuntimeError("SHIOAJI_CREDENTIALS_MISSING")
    api = sj.Shioaji()
    safe_login(api, api_key, secret_key, contracts_timeout=10000)
    return api


def _assert_paper_config() -> None:
    """This runner is evidence collection before a live config transition."""
    config = yaml.safe_load((ROOT / "config" / "futures.yaml").read_text()) or {}
    if bool(config.get("live_trading", False)):
        raise RuntimeError("PREFLIGHT_REQUIRES_PAPER_CONFIG")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--product", default="TMF")
    args = parser.parse_args()

    # This runner is for a pre-transition check only.  A config flip is not a
    # substitute for the full transition workflow.
    _assert_paper_config()
    from core.live_broker_preflight import run_once
    response = run_once(_api_factory, request_id=args.request_id, product=args.product)
    return 0 if response["preflight"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
