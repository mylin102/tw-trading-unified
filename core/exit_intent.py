"""Durable COMBINED_EXIT / exit intent log (P1-B design v3.1, Phase 1).

Append-only JSONL + fsync'd transitions, per-leg state machine, durable
lock with owner-verified reclaim, child sub-intents for repair, emergency
supersede records, capacity fail-closed, terminal compaction with
retention.

Phase 1 scope: this module only. Monitor/tmf_spread/order_manager wiring
is a LATER integration phase (B19/B23 stay red until then).
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable, Dict, List, Optional

MAX_ACTIVE_INTENTS = 20
RETENTION_DAYS = 30
INTENT_LOG_NAME = "mts_exit_intent.jsonl"
INTENT_ARCHIVE_NAME = "mts_exit_intent.archive.jsonl"
LOCK_NAME = "intent.lock"

LEG_STATUSES = (
    "NOT_SUBMITTED", "SUBMIT_ATTEMPTED", "SUBMITTED", "FILLED",
    "REJECTED", "CANCELLED", "NOT_FOUND_CONFIRMED", "UNKNOWN",
)
TERMINAL_LEG = ("FILLED", "REJECTED", "CANCELLED", "NOT_FOUND_CONFIRMED")


class IntentError(Exception):
    pass


class DuplicateSubmitError(IntentError):
    pass


class StaleVersionError(IntentError):
    pass


class IntentCapacityError(IntentError):
    pass


class LockBusyError(IntentError):
    pass


class IntentNotTerminalError(IntentError):
    pass


class SupersededIntentError(IntentError):
    pass


def client_order_id(trade_id: str, leg: str, nonce: Optional[str] = None) -> str:
    """Deterministic pre-I/O client id (design §1.1)."""
    import hashlib
    seed = f"{trade_id}:{leg}:{nonce or '0'}"
    return "CE-" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def _fsync_file(fd) -> None:
    os.fsync(fd)


def _atomic_append(path: str, record: dict) -> None:
    """O_APPEND single write + fsync; fsync parent dir on creation."""
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fd = fh.fileno()
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        _fsync_file(fd)


def _read_rows(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


class IntentLog:
    """Durable per-trade exit intent log (single-writer, lock-protected)."""

    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        self.log_path = os.path.join(log_dir, INTENT_LOG_NAME)
        self.archive_path = os.path.join(log_dir, INTENT_ARCHIVE_NAME)
        self.lock_path = os.path.join(log_dir, LOCK_NAME)

    # ── read ──────────────────────────────────────────────────────────
    def raw_lines(self) -> List[str]:
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path, encoding="utf-8") as fh:
            return [l.rstrip("\n") for l in fh if l.strip()]

    def _rows(self) -> List[dict]:
        return _read_rows(self.log_path)

    def _archive_rows(self) -> List[dict]:
        return _read_rows(self.archive_path)

    def get(self, intent_id: str) -> dict:
        row = None
        best = -1
        for r in self._rows() + self._archive_rows():
            if r.get("intent_id") == intent_id and r.get("version", 0) > best:
                row = r
                best = r.get("version", 0)
        if row is None:
            raise KeyError(f"no intent {intent_id}")
        return row

    def list_active(self) -> List[str]:
        last: Dict[str, dict] = {}
        for r in self._rows():
            last[r["intent_id"]] = r
        return [iid for iid, r in last.items() if r.get("terminal") is None]

    # ── write (all durable, fsync'd) ──────────────────────────────────
    def _new_intent(self, trade_id: str, reason: str, parent: Optional[str] = None,
                    leg: Optional[str] = None, nonce: Optional[str] = None) -> dict:
        if len(self.list_active()) >= MAX_ACTIVE_INTENTS:
            raise IntentCapacityError(f"active intents >= {MAX_ACTIVE_INTENTS}")
        iid = f"CE-{trade_id}-{nonce or (parent or '0')}"
        if parent:
            iid = f"RC-{parent}-{nonce}"
        legs = {
            "NEAR": {"status": "NOT_SUBMITTED", "client_order_id": None, "broker_order_id": None},
            "FAR": {"status": "NOT_SUBMITTED", "client_order_id": None, "broker_order_id": None},
        }
        if parent and leg:
            legs = {leg: {"status": "NOT_SUBMITTED", "client_order_id": None,
                          "broker_order_id": None}}
        # pre-generate deterministic client ids BEFORE any I/O (design §1.1)
        for l in legs:
            legs[l]["client_order_id"] = client_order_id(trade_id, l, nonce=nonce)
        return {
            "intent_id": iid, "trade_id": trade_id, "reason": reason,
            "version": 1, "created_at": time.time(), "parent": parent,
            "legs": legs, "terminal": None, "retention_expires_at": None,
            "supersedes": None,
        }

    def create(self, trade_id: str, reason: str = "COMBINED_EXIT") -> str:
        rec = self._new_intent(trade_id, reason)
        _atomic_append(self.log_path, rec)
        return rec["intent_id"]

    def transition(self, intent_id: str, leg: str, status: str,
                   client_order_id: Optional[str] = None,
                   broker_order_id: Optional[str] = None,
                   expect_version: Optional[int] = None) -> None:
        cur = self.get(intent_id)
        if expect_version is not None and cur.get("version") != expect_version:
            raise StaleVersionError(
                f"expected version {expect_version}, got {cur.get('version')}")
        if cur.get("terminal") == "SUPERSEDED_BY_EMERGENCY":
            raise SupersededIntentError(intent_id)
        legs = {k: dict(v) for k, v in cur["legs"].items()}
        if leg not in legs:
            legs[leg] = {"status": "NOT_SUBMITTED", "client_order_id": None,
                         "broker_order_id": None}
        if client_order_id is not None:
            legs[leg]["client_order_id"] = client_order_id
        if broker_order_id is not None:
            legs[leg]["broker_order_id"] = broker_order_id
        legs[leg]["status"] = status
        rec = dict(cur)
        rec["version"] = cur.get("version", 1) + 1
        rec["legs"] = legs
        _atomic_append(self.log_path, rec)

    def mark_terminal(self, intent_id: str, status: str) -> None:
        cur = self.get(intent_id)
        rec = dict(cur)
        rec["version"] = cur.get("version", 1) + 1
        rec["terminal"] = status
        _atomic_append(self.log_path, rec)

    # ── submission / repair / emergency ───────────────────────────────
    def submit_leg(self, intent_id: str, leg: str, order_mgr,
                   submit_fn: Optional[Callable] = None) -> dict:
        cur = self.get(intent_id)
        st = cur["legs"][leg]["status"]
        if st not in ("NOT_SUBMITTED", "SUBMIT_ATTEMPTED"):
            raise DuplicateSubmitError(f"{intent_id} {leg} already {st}")
        if st == "NOT_SUBMITTED":
            cid = cur["legs"][leg]["client_order_id"]
            self.transition(intent_id, leg, "SUBMIT_ATTEMPTED", client_order_id=cid)
        cur = self.get(intent_id)
        cid = cur["legs"][leg]["client_order_id"]
        fn = submit_fn or getattr(order_mgr, "submit")
        try:
            r = fn(cid, leg)
        except Exception:
            # ambiguous (broker may have accepted): durable UNKNOWN, never resubmit
            self.transition(intent_id, leg, "UNKNOWN")
            raise
        oid = (r or {}).get("order_id") if isinstance(r, dict) else None
        self.transition(intent_id, leg, "SUBMITTED", broker_order_id=oid)
        return {"intent_id": intent_id, "order_id": oid}

    def repair_complete(self, intent_id: str, leg: str, reason: str) -> dict:
        cur = self.get(intent_id)
        if cur.get("terminal") == "SUPERSEDED_BY_EMERGENCY":
            raise SupersededIntentError(intent_id)
        nonce = f"{int(time.time() * 1000)}-{leg}"
        child = self._new_intent(cur["trade_id"], f"REPAIR:{reason}",
                                 parent=intent_id, leg=leg, nonce=nonce)
        # durable SUBMIT_ATTEMPTED BEFORE any repair I/O (design §1.5)
        child["legs"][leg]["status"] = "SUBMIT_ATTEMPTED"
        _atomic_append(self.log_path, child)
        return {"intent_id": child["intent_id"], "parent": intent_id,
                "nonce": nonce, "legs": child["legs"]}

    def emergency_supersede(self, intent_id: str, order_mgr=None) -> dict:
        # durable supersede record BEFORE any emergency orders (design §1.8)
        _atomic_append(self.log_path, {"event": "EMERGENCY_SUPERSEDES",
                                       "intent_id": intent_id,
                                       "supersedes": intent_id,
                                       "ts": time.time()})
        self.mark_terminal(intent_id, "SUPERSEDED_BY_EMERGENCY")
        return {"supersedes": intent_id}

    def reconciliation_view(self, intent_id: str) -> dict:
        intent = self.get(intent_id)
        emg = None
        for r in self._rows():
            if r.get("event") == "EMERGENCY_SUPERSEDES" and r.get("intent_id") == intent_id:
                emg = r
        return {"intent": intent, "emergency": emg}

    # ── recovery (internally serialized) ──────────────────────────────
    def recover(self, intent_id: str, query_fn: Callable,
                order_mgr=None) -> dict:
        with self._lock_ctx():
            cur = self.get(intent_id)
            blocked = False
            all_terminal = True
            all_not_submitted = True
            for leg, st in cur["legs"].items():
                status = st["status"]
                if status in TERMINAL_LEG:
                    continue
                all_terminal = False
                if status == "NOT_SUBMITTED":
                    continue
                all_not_submitted = False
                qid = st.get("broker_order_id") or st.get("client_order_id") or ""
                q = query_fn(qid) if qid else {"status": "UNAVAILABLE"}
                qs = q.get("status") if isinstance(q, dict) else "UNAVAILABLE"
                if qs == "NOT_FOUND":
                    self.transition(intent_id, leg, "NOT_FOUND_CONFIRMED")
                elif qs in TERMINAL_LEG:
                    self.transition(intent_id, leg, qs)
                else:
                    blocked = True  # UNAVAILABLE/ambiguous: never infer, never resubmit
            cur = self.get(intent_id)
            # recompute AFTER transitions: statuses may have resolved in-loop
            all_terminal = all(st["status"] in TERMINAL_LEG
                               for st in cur["legs"].values())
            all_not_submitted = all(st["status"] == "NOT_SUBMITTED"
                                    for st in cur["legs"].values())
            if all_not_submitted:
                self.mark_terminal(intent_id, "CANCELED_SAFE")
                cur = self.get(intent_id)
            elif all_terminal:
                self.mark_terminal(intent_id, "COMPLETED")
                cur = self.get(intent_id)
            return {"legs": cur["legs"], "terminal": cur.get("terminal"),
                    "blocked": blocked}

    # ── compaction / retention ────────────────────────────────────────
    def archive(self, intent_id: str) -> None:
        cur = self.get(intent_id)
        if cur.get("terminal") is None:
            raise IntentNotTerminalError(intent_id)
        rec = dict(cur)
        rec["version"] = cur.get("version", 1) + 1
        rec["retention_expires_at"] = time.time() + RETENTION_DAYS * 86400
        _atomic_append(self.archive_path, rec)

    def archive_index(self) -> List[str]:
        return [r["intent_id"] for r in self._archive_rows()]

    # ── pre-gate ──────────────────────────────────────────────────────
    def has_inflight_exit_intent(self, trade_id: str) -> bool:
        for iid in self.list_active():
            if self.get(iid).get("trade_id") == trade_id:
                return True
        return False

    def entry_allowed(self, trade_id: str) -> bool:
        return not self.has_inflight_exit_intent(trade_id)

    def exit_trigger_allowed(self, trade_id: str) -> bool:
        return not self.has_inflight_exit_intent(trade_id)

    def session_transition_allowed(self, trade_id: str) -> bool:
        return not self.has_inflight_exit_intent(trade_id)

    def recovery_path_allowed(self, trade_id: str) -> bool:
        return True

    def emergency_path_allowed(self, trade_id: str) -> bool:
        return True

    # ── durable lock (owner-verified reclaim; age never authorizes) ───
    def _lock_ctx(self):
        return _LockCtx(self)

    def try_acquire(self, meta: dict, owner_check_fn: Optional[Callable] = None,
                    age_alert_threshold_s: Optional[float] = None) -> bool:
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            owner = self._read_owner()
            reclaim = False
            if owner is not None and owner_check_fn is not None:
                verdict = owner_check_fn(owner.get("pid"), owner.get("start_token")) or {}
                alive = bool(verdict.get("alive", False))
                token = verdict.get("start_token")
                if alive and token is not None and token != owner.get("start_token"):
                    reclaim = True  # PID reused by a new incarnation
                elif not alive:
                    reclaim = True  # owner verified dead
            if not reclaim:
                raise LockBusyError(
                    f"lock held by pid={owner.get('pid') if owner else '?'}")
            try:
                os.unlink(self.lock_path)
            except OSError:
                pass
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(meta, ensure_ascii=False))
        return True

    def lock(self, owner_name: str):
        return self._lock_ctx()

    def lock_owner(self) -> dict:
        return self._read_owner() or {}

    def _read_owner(self) -> Optional[dict]:
        if not os.path.exists(self.lock_path):
            return None
        try:
            with open(self.lock_path, encoding="utf-8") as fh:
                return json.loads(fh.read().strip() or "{}") or None
        except ValueError:
            return None

    def _force_lock_owner(self, meta: dict) -> None:
        with open(self.lock_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(meta, ensure_ascii=False) + "\n")


class _LockCtx:
    """Context manager: exclusive lock; LockBusyError if held by another."""

    def __init__(self, log: IntentLog):
        self.log = log

    def __enter__(self):
        self.log.try_acquire({"pid": os.getpid(),
                              "start_token": f"tok-{os.getpid()}",
                              "host": "local"})
        return self

    def __exit__(self, *exc):
        try:
            if os.path.exists(self.log.lock_path):
                os.unlink(self.log.lock_path)
        except OSError:
            pass
        return False


def dispatch_combined_exit(log: IntentLog, trade_id: str, order_mgr,
                           submit_hook: Optional[Callable] = None) -> dict:
    """Design §1.3 sequence: durable intent → per-leg SUBMIT_ATTEMPTED
    (fsync) → hook (call-time durable snapshot) → real submit."""
    iid = log.create(trade_id, "COMBINED_EXIT")
    for leg in ("NEAR", "FAR"):
        cid = log.get(iid)["legs"][leg]["client_order_id"]
        log.transition(iid, leg, "SUBMIT_ATTEMPTED", client_order_id=cid)
        if submit_hook is not None:
            submit_hook(leg, cid, iid)
        r = order_mgr.submit(cid, leg)
        oid = r.get("order_id") if isinstance(r, dict) else None
        log.transition(iid, leg, "SUBMITTED", broker_order_id=oid)
    return {"intent_id": iid}
