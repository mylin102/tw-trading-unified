#!/usr/bin/env python3
"""Live Route Certification — core module (Phase 1, authorized 2026-08-08).

Replaces the weak startup preflight (login flag / account hasattr / contract
object) with a single short-lived, process-bound broker certificate. Only the
certificate path may set LIVE_READY.

Design: .planning/live_route_certification_design.md (v7).
Capability map: .planning/shioaji_capability_map.md (verified Shioaji 1.7.0
surface; builtins.Shioaji cannot be weak-referenced and does not allow
setattr → the SessionRegistry is a module-level STRONG-registration map).

NOT wired to monitor.py — that is a separate, separately-reviewed phase.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
import secrets
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.live_broker_preflight import (
    PreflightBlocked,
    _account_hash,
    collect_read_only_preflight,
    resolve_near_far_contracts,
)
from core.mode_transition import (
    ExecutionContext,
    ModeTransitionState,
    with_effective_mode,
)

MARGIN_SOURCE_VERSION = 1
MARGIN_BUFFER = 0.1          # 1 near + 1 far micro: per_pair × (1 + buffer)
TTL_SECS = 60
SKEW_SECS = 30
KNOWN_PRODUCTS = ("TMF", "MTX")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _account_identity(account: Any) -> tuple:
    """Stable futures-account identity (capability map §4)."""
    return (getattr(account, "person_id", None),
            getattr(account, "broker_id", None),
            getattr(account, "account_id", None))


# ── SessionRegistry (round-8: strong-registration map, no weakref/setattr) ─

@dataclass
class _SessionEntry:
    api: object               # STRONG ref — occupies the id while registered
    generation: str           # secrets opaque generation
    logged_in_at: float


class SessionRegistry:
    """Process-local strong-registration map.

    builtins.Shioaji cannot be weak-referenced (capability map §6) and does
    not allow setattr, so this registry is module-level and holds the api
    object STRONGLY. id(api) is only an index; every read verifies
    ``entry.api is api`` (identity check — stale entries / id reuse yield
    no generation).
    """

    _entries: dict = {}          # class-level default (test contract); each
    # instance shadows it with its own dict in __init__.

    def __init__(self) -> None:
        self._entries: dict[int, _SessionEntry] = {}
        self._last_generation: Optional[str] = None
        self._last_generation_api: Any = None   # identity-aware last owner

    def register(self, api: object) -> str:
        generation = secrets.token_hex(16)
        self._entries[id(api)] = _SessionEntry(
            api=api, generation=generation, logged_in_at=time.time())
        self._last_generation = generation
        self._last_generation_api = api
        return generation

    def unregister(self, api: object) -> None:
        # identity-aware removal (round-9 #6): only clear the current
        # generation when the EXACT registration that owns it is removed —
        # removing another api must not invalidate the live session.
        entry = self._entries.pop(id(api), None)
        if entry is not None and entry.api is api \
                and self._last_generation_api is api:
            self._last_generation = None
            self._last_generation_api = None

    def generation(self, api: object) -> Optional[str]:
        entry = self._entries.get(id(api))
        if entry is None or entry.api is not api:
            return None
        return entry.generation

    def current_generation(self) -> Optional[str]:
        """The process's current session generation (most recent successful
        login). The MTS runtime has a single api object, so this is the
        authoritative current session for certificate validation."""
        return self._last_generation


session_registry = SessionRegistry()


def register_session(api: object) -> str:
    """Hook called AFTER a successful safe_login — registers exactly once.

    safe_login invalidates (unregister) BEFORE the attempt, so a failed
    relogin leaves no valid generation (see design v7 §6.2).
    """
    return session_registry.register(api)


def unregister_session(api: object) -> None:
    """Public logout hook — invalidates the registration (round-9 #1).
    Invoked by core/shioaji_session.logout() around the broker logout."""
    session_registry.unregister(api)


# ── authenticated-session adapter (capability map §4) ──────────────────────

def is_authenticated_session(api: object) -> bool:
    """Strict, verified Shioaji evidence — no optimistic fallback:
    (A) futopt_account is a valid account object
    (B) list_accounts() live query returns non-empty
    (C) the registry holds a generation for THIS api (login hook ran)
    Any missing piece / exception → False (fail-closed)."""
    try:
        if getattr(api, "futopt_account", None) is None:
            return False
        accounts = list(api.list_accounts())
        if not accounts:
            return False
        # round-9 #2: the futures account must actually be REPRESENTED in
        # list_accounts — a non-empty list of a DIFFERENT account is not an
        # authenticated futures session.
        fut_ident = _account_identity(getattr(api, "futopt_account"))
        if not any(_account_identity(a) == fut_ident for a in accounts):
            return False
    except Exception:
        return False
    return session_registry.generation(api) is not None


# ── margin source (round-9 #3/#4: sealed, trusted-config only) ─────────────

@dataclass(frozen=True)
class SealedMarginSource:
    """Immutable, sealed per-pair margin source — the ONLY input type
    certify_route accepts. Built exclusively by load_trusted_margin_source /
    parse_margin_config which READ the effective config bytes and compute
    the SHA256 internally (caller-supplied metadata is forgeable and is
    rejected by the type check)."""
    source: str                       # "CONFIG_FLOOR"
    version: int
    config_path: str
    config_sha256: str
    config_commit: str                # deployed release SHA
    per_pair_margin: float
    product: str


MARGIN_FLOOR_KEY = ("mts", "live_required_margin_per_pair")


def _parse_floor(doc: Any) -> float:
    node = doc
    for key in MARGIN_FLOOR_KEY:
        if not isinstance(node, dict):
            raise ValueError(f"config missing {'.'.join(MARGIN_FLOOR_KEY)}")
        node = node.get(key)
        if node is None:
            raise ValueError(f"config missing {'.'.join(MARGIN_FLOOR_KEY)}")
    if isinstance(node, bool):
        raise ValueError("per_pair margin must be numeric")
    try:
        value = float(node)
    except (TypeError, ValueError):
        raise ValueError(f"per_pair margin not numeric: {node!r}")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"per_pair margin must be finite positive: {node!r}")
    return value


def parse_margin_config(raw: bytes, *, config_path: str, release_sha: str,
                        product: str = "TMF") -> SealedMarginSource:
    """Seal the margin source from the ACTUAL config bytes. The SHA256 is
    computed here; caller-supplied hashes are never trusted (round-9 #3)."""
    product = str(product).upper()
    if product not in KNOWN_PRODUCTS:
        raise ValueError(f"unknown product {product!r}")
    if not release_sha:
        raise ValueError("release_sha (deployed commit) required")
    import yaml
    try:
        doc = yaml.safe_load(raw)
    except Exception as exc:
        raise ValueError(f"unparseable config: {exc}") from exc
    floor = _parse_floor(doc)
    return SealedMarginSource(
        source="CONFIG_FLOOR",
        version=MARGIN_SOURCE_VERSION,
        config_path=str(config_path),
        config_sha256=hashlib.sha256(raw).hexdigest(),
        config_commit=str(release_sha),
        per_pair_margin=floor,
        product=product,
    )


def load_trusted_margin_source(config_path, *, release_sha: str,
                               product: str = "TMF") -> SealedMarginSource:
    """Read the effective config FILE, verify and seal the margin source.
    Missing/unreadable file → ValueError (fail-closed)."""
    path = Path(config_path)
    if not path.is_file():
        raise ValueError(f"config not found: {path}")
    return parse_margin_config(path.read_bytes(), config_path=str(path),
                               release_sha=release_sha, product=product)


def _required_margin(source: SealedMarginSource,
                     buffer: float = MARGIN_BUFFER) -> float:
    return round(float(source.per_pair_margin) * (1 + buffer), 2)


# ── certificate + issuer ───────────────────────────────────────────────────

@dataclass(frozen=True)
class LiveBrokerCertificate:
    version: int = 1
    nonce: str = ""                    # in-memory issuance handle (issuer-set)
    process_start_id: str = ""
    captured_at: str = ""              # ISO-8601 tz-aware
    account_hash: str = ""
    near_code: str = ""
    far_code: str = ""
    position_snapshot_ts: str = ""
    order_snapshot_ts: str = ""
    margin_available: float = 0.0
    required_margin: float = 0.0
    margin_source: str = ""            # "CONFIG_FLOOR"
    margin_source_version: int = MARGIN_SOURCE_VERSION
    config_path: str = ""
    config_sha256: str = ""
    config_commit: str = ""
    product: str = ""
    session_generation: str = ""
    query_results: tuple = ()
    bidask_subscribed: tuple = ()
    bidask_unsubscribed: tuple = ()
    warnings: tuple = ()


class CertificateAlreadyConsumed(Exception):
    """A certificate whose nonce was already redeemed was used again."""


class CertificateIssuer:
    """In-memory unforgeable issuance registry (per-process).

    The nonce exists only here. Persisted snapshots are audit-only and can
    never recreate issuance state (fresh issuers start with no state).
    """

    def __init__(self) -> None:
        # Lazy state: a fresh issuer has an empty __dict__ (restart has no
        # restored authorization).
        pass

    def _maps(self) -> tuple[dict, set]:
        if not hasattr(self, "_issued"):
            self._issued: dict[str, LiveBrokerCertificate] = {}
            self._consumed: set[str] = set()
        return self._issued, self._consumed

    def issue(self, cert: LiveBrokerCertificate) -> LiveBrokerCertificate:
        issued_map, _ = self._maps()
        nonce = secrets.token_hex(16)
        issued = replace(cert, nonce=nonce)
        issued_map[nonce] = issued
        return issued

    def peek(self, nonce: str) -> Optional[LiveBrokerCertificate]:
        issued_map, _ = self._maps()
        return issued_map.get(nonce)

    def redeem(self, nonce: str) -> Optional[LiveBrokerCertificate]:
        issued_map, consumed = self._maps()
        cert = issued_map.pop(nonce, None)
        if cert is not None:
            consumed.add(nonce)          # single-use: consumed exactly once
        return cert

    def was_consumed(self, nonce: str) -> bool:
        _, consumed = self._maps()
        return nonce in consumed

    def invalidate_all(self) -> None:
        issued_map, consumed = self._maps()
        issued_map.clear()
        consumed.clear()


# ── certification (collects from the session itself — no external payload) ─

def certify_route(api: object, *, process_start_id: str,
                  issuer: CertificateIssuer, config_path,
                  release_sha: str, product: str = "TMF",
                  margin_buffer: float = MARGIN_BUFFER,
                  ) -> tuple[Optional[LiveBrokerCertificate], list[str]]:
    """Collect from the CURRENT authenticated execution session and issue a
    certificate. No external dict/JSON payload is accepted (B2); the margin
    source is loaded and sealed HERE from the effective config file
    (round-9 #3/#4) — a caller-supplied source cannot be injected."""
    margin_source = load_trusted_margin_source(config_path,
                                               release_sha=release_sha,
                                               product=product)
    failures: list[str] = []

    if not is_authenticated_session(api):
        return None, ["AUTH_SESSION_UNAVAILABLE"]

    try:
        pre = collect_read_only_preflight(api, product)
    except PreflightBlocked as exc:
        return None, [str(exc)]
    except Exception as exc:
        return None, [f"PREFLIGHT_FAILED: {type(exc).__name__}: {exc}"]

    account_hash = pre.get("account_id_hash") or ""
    if not account_hash:
        failures.append("ACCOUNT_UNREADABLE")

    required = _required_margin(margin_source, margin_buffer)
    available = (pre.get("margin") or {}).get("available_margin")
    if available is None or not isinstance(available, (int, float)) \
            or not math.isfinite(float(available)) or float(available) < required:
        failures.append("MARGIN_INSUFFICIENT")

    if pre.get("positions"):
        failures.append("BROKER_NOT_FLAT")
    if pre.get("open_orders"):
        failures.append("OPEN_ORDERS_PRESENT")

    contracts = pre.get("contracts") or {}
    near_code = (contracts.get("near") or {}).get("code")
    far_code = (contracts.get("far") or {}).get("code")
    if not near_code or not far_code or near_code == far_code:
        failures.append("CONTRACTS_NOT_DISTINCT")

    # snapshot presence semantics (L5): the exact set {near, far} — empty /
    # missing / duplicate / extra codes all fail (market-closed is not a
    # documented exception yet)
    snapshot_codes = [c for c in (pre.get("snapshot_codes") or [])]
    if near_code and far_code and set(snapshot_codes) != {near_code, far_code}:
        failures.append("SNAPSHOT_CODES_INCONSISTENT")

    # round-9 #5: all([]) is true — require EXACTLY two successful, unique
    # quote checks whose codes are precisely {near_code, far_code}.
    quote_checks = [c for c in (pre.get("quote_subscription") or [])
                    if c.get("passed")]
    quote_codes = {c.get("code") for c in quote_checks}
    if near_code and far_code and (
            len(quote_checks) != 2 or len(quote_codes) != 2
            or quote_codes != {near_code, far_code}):
        failures.append("QUOTE_SUBSCRIPTION_FAILED")

    if failures:
        return None, failures

    generation = session_registry.generation(api) or ""
    cert = LiveBrokerCertificate(
        process_start_id=process_start_id,
        captured_at=_now_iso(),
        account_hash=account_hash,
        near_code=near_code,
        far_code=far_code,
        position_snapshot_ts=pre.get("position_snapshot_time") or _now_iso(),
        order_snapshot_ts=pre.get("order_snapshot_time") or _now_iso(),
        margin_available=float(available),
        required_margin=required,
        margin_source=margin_source.source,
        margin_source_version=margin_source.version,
        config_path=margin_source.config_path,
        config_sha256=margin_source.config_sha256,
        config_commit=margin_source.config_commit,
        product=margin_source.product,
        session_generation=generation,
        query_results=tuple(pre.get("query_failures") or []) + ("AUTH_OK",),
        bidask_subscribed=tuple(
            "NEAR" if c.get("code") == near_code else "FAR"
            for c in quote_checks),
        bidask_unsubscribed=tuple(
            "NEAR" if c.get("code") == near_code else "FAR"
            for c in quote_checks),
        warnings=tuple(pre.get("warnings") or []),
    )
    return issuer.issue(cert), []


# ── validation ─────────────────────────────────────────────────────────────

def validate_live_broker_certificate(
        cert: LiveBrokerCertificate, *, issuer: CertificateIssuer,
        process_start_id: str, account_hash: str, near_code: str,
        far_code: str, now_ts: Optional[str] = None,
        margin_source: Optional[SealedMarginSource] = None,
        session_generation: Optional[str] = None,
        ttl_secs: int = TTL_SECS, skew_secs: int = SKEW_SECS,
        ) -> tuple[bool, list[str]]:
    reasons: list[str] = []

    if issuer.peek(cert.nonce) is None:
        reasons.append("NONCE_UNKNOWN")

    try:
        captured = datetime.fromisoformat(cert.captured_at)
        now = datetime.fromisoformat(now_ts) if now_ts \
            else datetime.now(timezone.utc)
        if now < captured - timedelta(seconds=skew_secs):
            reasons.append("SKEW")
        elif now - captured > timedelta(seconds=ttl_secs):
            reasons.append("STALE")
    except (TypeError, ValueError):
        reasons.append("CAPTURED_AT_UNPARSEABLE")

    if cert.process_start_id != process_start_id:
        reasons.append("PROCESS_MISMATCH")
    if cert.account_hash != account_hash:
        reasons.append("ACCOUNT")
    if cert.near_code != near_code or cert.far_code != far_code:
        reasons.append("CONTRACT_MISMATCH")

    expected_generation = session_generation \
        if session_generation is not None \
        else session_registry.current_generation()
    if cert.session_generation and (
            expected_generation is None
            or cert.session_generation != expected_generation):
        reasons.append("SESSION_GENERATION_MISMATCH")

    if margin_source is not None:
        expected_required = _required_margin(margin_source)
        if cert.margin_source != margin_source.source \
                or cert.margin_source_version != margin_source.version \
                or cert.config_path != margin_source.config_path \
                or cert.config_sha256 != margin_source.config_sha256 \
                or cert.config_commit != margin_source.config_commit \
                or cert.product != margin_source.product \
                or cert.required_margin != expected_required:
            reasons.append("SOURCE_MISMATCH")

    return (not reasons, reasons)


# ── runtime certification context (P0-4: trusted factory only) ─────────────

@dataclass(frozen=True)
class RuntimeCertificationContext:
    """Internal immutable context. There is NO public dict/JSON constructor —
    build_runtime_certification_context is the only factory (forged mappings
    are rejected by the transition type check)."""
    process_start_id: str
    account_hash: str
    near_code: str
    far_code: str
    margin_source: dict
    session_generation: str
    now_ts: str


def build_runtime_certification_context(api: object, config: dict,
                                        process_state: Optional[dict] = None,
                                        ) -> RuntimeCertificationContext:
    """Trusted factory: current api + loaded config + process state.
    ``config`` is the effective config FILE PATH; the margin source is
    sealed by load_trusted_margin_source (round-9 #3)."""
    account = getattr(api, "futopt_account", None)
    if account is None:
        raise PreflightBlocked("FUTOPT_ACCOUNT_UNAVAILABLE")
    near, far = resolve_near_far_contracts(api, "TMF")
    ps = process_state or {}
    if isinstance(ps, dict) and isinstance(ps.get("process_state"), dict):
        ps = ps["process_state"]          # facts = {config, process_state}
    return RuntimeCertificationContext(
        process_start_id=str(ps.get("process_start_id", "")),
        account_hash=_account_hash(account),
        near_code=getattr(near, "code", "") or "",
        far_code=getattr(far, "code", "") or "",
        margin_source=load_trusted_margin_source(
            config, release_sha=str(ps.get("release_sha", "") or "")),
        session_generation=session_registry.generation(api) or "",
        now_ts=_now_iso(),
    )


# ── transition (B1/B5/L7: atomic validation + single-use redeem) ───────────

def transition_with_certificate(
        ctx: ExecutionContext, cert: LiveBrokerCertificate,
        issuer: CertificateIssuer, *, runtime: RuntimeCertificationContext,
        ) -> ExecutionContext:
    """The ONLY path to LIVE_READY.

    Validates every fact against the in-process runtime context BEFORE and
    atomically with the single-use nonce redeem. Any failure returns an
    explicit LIVE_QUARANTINED context with audit reasons (never raises for
    fact failures; never authorizes). A consumed certificate raises
    CertificateAlreadyConsumed. A forgeable mapping is rejected (TypeError).
    """
    if not isinstance(runtime, RuntimeCertificationContext):
        raise TypeError(
            "runtime must be a RuntimeCertificationContext built by "
            "build_runtime_certification_context — external dict/JSON is "
            "not accepted as authorization input")

    if issuer.peek(cert.nonce) is None and issuer.was_consumed(cert.nonce):
        raise CertificateAlreadyConsumed(
            f"certificate nonce {cert.nonce} already redeemed")

    # round-9 #7 canonicality: the supplied certificate must be BYTE-EQUAL
    # to the issuer's canonical issued certificate — the nonce alone only
    # proves existence. Any tampering (captured_at/margin/account/contract/
    # config) with the same nonce+issuer is rejected before fact checks.
    canonical = issuer.peek(cert.nonce)
    if canonical is not None and canonical != cert:
        return with_effective_mode(
            ctx, ModeTransitionState.LIVE_QUARANTINED.value,
            live_order_allowed=False,
            audit_reasons=("CERT_TAMPERED",))

    ok, reasons = validate_live_broker_certificate(
        cert, issuer=issuer,
        now_ts=runtime.now_ts,
        process_start_id=runtime.process_start_id,
        account_hash=runtime.account_hash,
        near_code=runtime.near_code,
        far_code=runtime.far_code,
        margin_source=runtime.margin_source,
        session_generation=runtime.session_generation,
    )
    if cert.session_generation != runtime.session_generation:
        reasons.append("SESSION_GENERATION_MISMATCH")

    if reasons:
        return with_effective_mode(
            ctx, ModeTransitionState.LIVE_QUARANTINED.value,
            live_order_allowed=False, audit_reasons=tuple(reasons))

    if issuer.peek(cert.nonce) is None:
        return with_effective_mode(
            ctx, ModeTransitionState.LIVE_QUARANTINED.value,
            live_order_allowed=False, audit_reasons=("NONCE_UNKNOWN",))

    issuer.redeem(cert.nonce)              # consume exactly once
    return with_effective_mode(
        ctx, ModeTransitionState.LIVE_READY.value,
        live_order_allowed=True, audit_reasons=())
