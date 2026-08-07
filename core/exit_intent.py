"""Durable COMBINED_EXIT / exit intent log (P1-B design v3.1, Phase 1 corr.).

Codex corrective round: all public mutations serialize internally
(reentrant mutex + durable O_EXCL file lock with owner-verified reclaim),
legal state-transition validation, submit_leg sends ONLY from
NOT_SUBMITTED (never re-sends an attempted leg without authoritative
query), real process-start token, durable atomic append (O_APPEND single
write + fsync + parent-dir fsync on creation), collision-resistant ids,
and recovery terminal policy that never silently completes a failed exit.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
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

# legal per-leg transitions (never FILLED→attempted, etc.)
_ALLOWED = {
    "NOT_SUBMITTED": {"SUBMIT_ATTEMPTED"},
    "SUBMIT_ATTEMPTED": {"SUBMITTED", "UNKNOWN", "REJECTED", "CANCELLED",
                         "FILLED", "NOT_FOUND_CONFIRMED"},
    "SUBMITTED": {"FILLED", "REJECTED", "CANCELLED", "UNKNOWN",
                  "NOT_FOUND_CONFIRMED"},
    "UNKNOWN": {"FILLED", "REJECTED", "CANCELLED", "NOT_FOUND_CONFIRMED"},
    "FILLED": set(),
    "REJECTED": set(),
    "CANCELLED": set(),
    "NOT_FOUND_CONFIRMED": set(),
}

# real per-process start token (module import time + pid; PID reuse gets a
# different import-time seed) — owner-verified reclaim uses it
_PROCESS_TOKEN = f"{os.getpid()}-{int(time.time() * 1_000_000)}"

# per-lock-path holder registry for same-thread reentrancy
_LOCK_HELD: Dict[str, dict] = {}


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


class IllegalTransitionError(IntentError):
    pass


class DuplicateRepairError(IntentError):
    pass


class CorruptLogError(IntentError):
    pass


def client_order_id(trade_id: str, leg: str, nonce: Optional[str] = None) -> str:
    """Deterministic pre-I/O client id (per-leg idempotency key)."""
    import hashlib
    seed = f"{trade_id}:{leg}:{nonce or '0'}"
    return "CE-" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def _os_start_token(pid: int):
    """Verifiable per-PID OS start token (codex: never guess a foreign
    owner's identity). Returns ("alive", token) | ("dead", None) |
    ("unknown", None)."""
    import subprocess
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return ("alive", f"{pid}:{r.stdout.strip()}")
        if r.returncode == 1 and not r.stdout.strip():
            return ("dead", None)  # verified absent
        return ("unknown", None)   # unverifiable → fail-closed
    except Exception:
        return ("unknown", None)


def _default_owner_check(pid, token) -> dict:
    """Owner-verified check against the OS: returns
    {"state": alive|dead|unknown, "start_token": current}. Callers reclaim
    ONLY on dead or alive-with-token-mismatch (PID reuse); unknown and
    alive-with-same-token are NEVER reclaimed."""
    if not isinstance(pid, int):
        return {"state": "unknown", "start_token": None}
    state, cur_token = _os_start_token(pid)
    return {"state": state, "start_token": cur_token}


def _atomic_append(path: str, record: dict) -> None:
    """Durable primitive: O_APPEND single write + fsync; fsync parent dir
    on file creation (codex #4)."""
    parent = os.path.dirname(path)
    created = not os.path.exists(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    if created and parent:
        dfd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def _pid_alive(pid: int) -> bool:
    return _os_start_token(pid)[0] == "alive"


def _read_rows(path: str) -> List[dict]:
    """Read + parse; raises CorruptLogError on ANY malformed record
    (fail-closed: a crash tail must never be silently swallowed)."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError as exc:
                raise CorruptLogError(
                    f"malformed record at {path}:{lineno}") from exc
    return out


class IntentLog:
    """Durable per-trade exit intent log (single-writer, internally locked)."""

    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)  # BEFORE any lock acquisition
        self.log_path = os.path.join(log_dir, INTENT_LOG_NAME)
        self.archive_path = os.path.join(log_dir, INTENT_ARCHIVE_NAME)
        self.lock_path = os.path.join(log_dir, LOCK_NAME)
        self._mutex = threading.RLock()  # in-process reentrant guard

    # ── read (lock-free) ───────────────────────────────────────────────
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
        # PARTIAL/FAILED_NO_FILL are NOT fully resolved: repair pending ⇒
        # still in-flight (blocks entry/exit controllers)
        return [iid for iid, r in last.items()
                if r.get("terminal") in (None, "PARTIAL", "FAILED_NO_FILL")]

    # ── durable lock (design §1.7: owner-verified; age never authorizes) ─
    def _lock_meta(self, intent_id: Optional[str] = None) -> dict:
        state, token = _os_start_token(os.getpid())
        v = 0
        if intent_id is not None:
            try:
                v = self.get(intent_id)["version"]
            except KeyError:
                v = 0
        return {"pid": os.getpid(),
                "start_token": token or _PROCESS_TOKEN,
                "host": socket.gethostname(), "acquired_at": time.time(),
                "intent_version": v}

    def _write_lock(self, fd, meta: dict) -> None:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(meta, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        # fsync the parent dir so the lock file creation is durable
        dfd = os.open(self.log_dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def _release_lock(self) -> None:
        """Only unlink if the lock still belongs to THIS owner (fence)."""
        own = self._read_owner()
        if own is None:
            return
        if own.get("pid") == os.getpid() and \
                own.get("start_token") == self._lock_meta()["start_token"] and \
                own.get("host") == socket.gethostname():
            try:
                os.unlink(self.lock_path)
            except OSError:
                pass

    @contextmanager
    def _file_lock(self, intent_id: Optional[str] = None):
        """Cross-process exclusive lock with owner-verified reclaim.

        Reentrant for the SAME thread (mirrors the RLock); different threads
        or processes contend via the O_EXCL file. Host mismatch on the
        recorded owner ⇒ fail-closed (a remote healthy owner with the same
        numeric PID must never be reclaimed)."""
        entry = _LOCK_HELD.get(self.lock_path)
        if entry is not None and entry["thread"] == threading.get_ident():
            entry["depth"] += 1
            try:
                yield
            finally:
                entry["depth"] -= 1
            return
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                owner = self._read_owner()
                reclaim = False
                if owner is None:
                    # empty/partial lock file (writer mid-flight) or corrupt:
                    # NEVER reclaim — fail-closed (codex: unknown ⇒ LOCK_BUSY)
                    raise LockBusyError("lock file unreadable (writer in flight?)")
                if owner.get("host") != socket.gethostname():
                    # remote host with the same numeric PID must NEVER be
                    # judged by local ps (codex B43)
                    raise LockBusyError(
                        f"lock held on remote host {owner.get('host')}")
                verdict = _default_owner_check(owner.get("pid"),
                                               owner.get("start_token"))
                state = verdict.get("state")
                cur_token = verdict.get("start_token")
                if state == "dead":
                    reclaim = True
                elif state == "alive" and cur_token is not None and \
                        cur_token != owner.get("start_token"):
                    reclaim = True  # PID reused by a new incarnation
                # alive+same-token and unknown ⇒ NEVER reclaim (fail-closed)
                if not reclaim:
                    raise LockBusyError(
                        f"lock held by pid={owner.get('pid') if owner else '?'}")
                # ownership fence: only unlink if the owner is unchanged
                if self._read_owner() == owner:
                    try:
                        os.unlink(self.lock_path)
                    except OSError:
                        pass
                continue  # retry acquire
            self._write_lock(fd, self._lock_meta(intent_id))
            _LOCK_HELD[self.lock_path] = {
                "thread": threading.get_ident(), "depth": 1,
                "intent_versions": {intent_id: self._lock_meta(intent_id)["intent_version"]}
                if intent_id is not None else {}}
            try:
                yield
            finally:
                _LOCK_HELD.pop(self.lock_path, None)
                self._release_lock()
            return

    def _locked(self, fn: Callable, *a, intent_id: Optional[str] = None, **k):
        with self._mutex:
            with self._file_lock(intent_id=intent_id):
                # generation fence enforced per-durable-write in
                # _append_locked (single primitive); no separate entry check
                return fn(*a, **k)

    def try_acquire(self, meta: dict, owner_check_fn: Optional[Callable] = None,
                    age_alert_threshold_s: Optional[float] = None) -> bool:
        """TEST/OPERATOR-only lock API — shares the same fsync + host-check
        + owner-verified semantics as _file_lock. owner_check_fn(pid, token)
        returns {"alive": bool, "start_token": <current>|None}."""
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                owner = self._read_owner()
                reclaim = False
                if owner is not None:
                    if owner.get("host") != socket.gethostname():
                        raise LockBusyError(
                            f"lock held on remote host {owner.get('host')}")
                    if owner_check_fn is not None:
                        verdict = owner_check_fn(owner.get("pid"),
                                                 owner.get("start_token")) or {}
                        alive = bool(verdict.get("alive", False))
                        token = verdict.get("start_token")
                        if alive and token is not None and token != owner.get("start_token"):
                            reclaim = True  # PID reused
                        elif not alive:
                            reclaim = True
                # owner unreadable or no owner_check → fail-closed, no reclaim
                if not reclaim:
                    raise LockBusyError(
                        f"lock held by pid={owner.get('pid') if owner else '?'}")
                if self._read_owner() == owner:
                    try:
                        os.unlink(self.lock_path)
                    except OSError:
                        pass
                continue
            rec = dict(meta)
            rec.setdefault("host", socket.gethostname())
            self._write_lock(fd, rec)
            return True

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

    # ── private (locked) helpers ───────────────────────────────────────
    def _create_locked(self, trade_id: str, reason: str,
                       parent: Optional[str] = None,
                       leg: Optional[str] = None) -> dict:
        if len(self.list_active()) >= MAX_ACTIVE_INTENTS:
            raise IntentCapacityError(f"active intents >= {MAX_ACTIVE_INTENTS}")
        uid = uuid.uuid4().hex[:12]
        iid = f"RC-{parent}-{uid}" if parent else f"CE-{trade_id}-{uid}"
        legs = {
            "NEAR": {"status": "NOT_SUBMITTED", "client_order_id": None,
                     "broker_order_id": None},
            "FAR": {"status": "NOT_SUBMITTED", "client_order_id": None,
                    "broker_order_id": None},
        }
        if parent and leg:
            legs = {leg: {"status": "NOT_SUBMITTED", "client_order_id": None,
                          "broker_order_id": None}}
        for l in legs:
            legs[l]["client_order_id"] = client_order_id(trade_id, l, nonce=uid)
        return {
            "intent_id": iid, "trade_id": trade_id, "reason": reason,
            "version": 1, "created_at": time.time(), "parent": parent,
            "legs": legs, "terminal": None, "retention_expires_at": None,
            "supersedes": None,
        }

    def _append_locked(self, fence_intent_id: str, builder: Callable,
                       path: Optional[str] = None) -> dict:
        """Single durable-append primitive with the generation fence
        (codex B44-B47): before EVERY durable write the fence intent's
        version must match what THIS lock holder last saw — external drift ⇒
        StaleVersionError BEFORE any I/O. Our own transitions advance the
        fence, so legitimate same-holder evolution is never blocked.

        fence_intent_id is the intent whose version gates the write; the
        appended record may be that intent (transition/terminal) or a
        parent-bound child/event (repair/emergency) that references it."""
        entry = _LOCK_HELD.get(self.lock_path)
        expected = None
        if entry is not None:
            expected = entry.setdefault("intent_versions", {}).get(fence_intent_id)
        cur = self.get(fence_intent_id)
        if expected is not None and cur["version"] != expected:
            raise StaleVersionError(
                f"intent {fence_intent_id} version drifted externally "
                f"(holder saw {expected}, now {cur['version']})")
        rec = builder(cur)
        _atomic_append(path or self.log_path, rec)
        if entry is not None:
            if rec.get("intent_id") == fence_intent_id:
                entry.setdefault("intent_versions", {})[fence_intent_id] = \
                    rec.get("version", cur["version"])
            else:
                # parent-bound child/event: parent version unchanged
                entry.setdefault("intent_versions", {})[fence_intent_id] = \
                    cur["version"]
        return rec

    def _transition_locked(self, intent_id: str, leg: str, status: str,
                           client_order_id: Optional[str] = None,
                           broker_order_id: Optional[str] = None,
                           expect_version: Optional[int] = None) -> None:
        cur = self.get(intent_id)
        if expect_version is not None and cur.get("version") != expect_version:
            raise StaleVersionError(
                f"expected version {expect_version}, got {cur.get('version')}")
        if cur.get("terminal") == "SUPERSEDED_BY_EMERGENCY":
            raise SupersededIntentError(intent_id)
        prev = cur["legs"][leg]["status"]
        if status not in _ALLOWED.get(prev, set()):
            raise IllegalTransitionError(f"{prev} -> {status} illegal")

        def builder(c):
            legs = {k: dict(v) for k, v in c["legs"].items()}
            if client_order_id is not None:
                legs[leg]["client_order_id"] = client_order_id
            if broker_order_id is not None:
                legs[leg]["broker_order_id"] = broker_order_id
            legs[leg]["status"] = status
            rec = dict(c)
            rec["version"] = c.get("version", 1) + 1
            rec["legs"] = legs
            return rec
        self._append_locked(intent_id, builder)

    # ── public mutations (all internally locked) ──────────────────────
    def create(self, trade_id: str, reason: str = "COMBINED_EXIT") -> str:
        def _do():
            rec = self._create_locked(trade_id, reason)
            _atomic_append(self.log_path, rec)
            return rec["intent_id"]
        return self._locked(_do)

    def transition(self, intent_id: str, leg: str, status: str,
                   client_order_id: Optional[str] = None,
                   broker_order_id: Optional[str] = None,
                   expect_version: Optional[int] = None) -> None:
        self._locked(self._transition_locked, intent_id, leg, status,
                     client_order_id, broker_order_id, expect_version,
                     intent_id=intent_id)

    def mark_terminal(self, intent_id: str, status: str) -> None:
        def _do():
            def builder(c):
                rec = dict(c)
                rec["version"] = c.get("version", 1) + 1
                rec["terminal"] = status
                return rec
            self._append_locked(intent_id, builder)
        self._locked(_do, intent_id=intent_id)

    def submit_leg(self, intent_id: str, leg: str, order_mgr,
                   submit_fn: Optional[Callable] = None,
                   submit_hook: Optional[Callable] = None) -> dict:
        """Canonical submit: ONLY from NOT_SUBMITTED. Durable SUBMIT_ATTEMPTED
        first (inside the same lock), then I/O, then SUBMITTED/UNKNOWN.
        An already-attempted leg is REJECTED — recovery must query."""
        def _do():
            cur = self.get(intent_id)
            st = cur["legs"][leg]["status"]
            if st != "NOT_SUBMITTED":
                raise DuplicateSubmitError(f"{intent_id} {leg} already {st} "
                                           "(recovery query required)")
            cid = cur["legs"][leg]["client_order_id"]
            self._transition_locked(intent_id, leg, "SUBMIT_ATTEMPTED",
                                    client_order_id=cid)
            if submit_hook is not None:
                submit_hook(leg, cid, intent_id)
            fn = submit_fn or getattr(order_mgr, "submit")
            try:
                r = fn(cid, leg)
            except Exception:
                # ambiguous (broker may have accepted): durable UNKNOWN
                self._transition_locked(intent_id, leg, "UNKNOWN")
                raise
            oid = (r or {}).get("order_id") if isinstance(r, dict) else None
            self._transition_locked(intent_id, leg, "SUBMITTED",
                                    broker_order_id=oid)
            return {"intent_id": intent_id, "order_id": oid}
        return self._locked(_do, intent_id=intent_id)

    def repair_complete(self, intent_id: str, leg: str, reason: str) -> dict:
        def _do():
            cur = self.get(intent_id)
            if cur.get("terminal") == "SUPERSEDED_BY_EMERGENCY":
                raise SupersededIntentError(intent_id)
            # per-(parent, leg) ACTIVE child idempotency (codex #3): a second
            # repair for the same leg must NOT create another submittable order
            for iid in self.list_active():
                r = self.get(iid)
                if r.get("parent") == intent_id and leg in r.get("legs", {}):
                    raise DuplicateRepairError(
                        f"active repair child {iid} exists for {intent_id} {leg}")
            child = self._append_locked(intent_id, lambda c: self._create_locked(
                c["trade_id"], f"REPAIR:{reason}", parent=intent_id, leg=leg))
            return {"intent_id": child["intent_id"], "parent": intent_id,
                    "nonce": child["intent_id"].split("-")[-1],
                    "legs": child["legs"]}
        return self._locked(_do, intent_id=intent_id)

    def emergency_supersede(self, intent_id: str, order_mgr=None) -> dict:
        def _do():
            # audit event + terminal record BOTH fence-aware (B47): the
            # parent version is validated immediately before each append
            self._append_locked(intent_id, lambda c: {
                "event": "EMERGENCY_SUPERSEDES", "intent_id": intent_id,
                "supersedes": intent_id, "ts": time.time(),
                "version": c["version"]})
            self._append_locked(intent_id, lambda c: {
                **dict(c), "version": c["version"] + 1,
                "terminal": "SUPERSEDED_BY_EMERGENCY"})
            return {"supersedes": intent_id}
        return self._locked(_do, intent_id=intent_id)

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
        def _do():
            cur = self.get(intent_id)
            blocked = False
            for leg, st in cur["legs"].items():
                status = st["status"]
                if status in TERMINAL_LEG or status == "NOT_SUBMITTED":
                    continue
                qid = st.get("broker_order_id") or st.get("client_order_id") or ""
                q = query_fn(qid) if qid else {"status": "UNAVAILABLE"}
                qs = q.get("status") if isinstance(q, dict) else "UNAVAILABLE"
                if qs in ("NOT_FOUND", "FILLED", "REJECTED", "CANCELLED"):
                    target = "NOT_FOUND_CONFIRMED" if qs == "NOT_FOUND" else qs
                    self._transition_locked(intent_id, leg, target)
                else:
                    blocked = True  # UNAVAILABLE/ambiguous: never infer
            cur = self.get(intent_id)
            statuses = [st["status"] for st in cur["legs"].values()]
            terminal = cur.get("terminal")
            if all(s == "NOT_SUBMITTED" for s in statuses):
                self._transition_terminal_locked(intent_id, "CANCELED_SAFE")
                terminal = "CANCELED_SAFE"
            elif all(s == "FILLED" for s in statuses):
                self._transition_terminal_locked(intent_id, "COMPLETED")
                terminal = "COMPLETED"
            elif all(s in TERMINAL_LEG for s in statuses) and \
                    all(s != "FILLED" for s in statuses):
                # attempted exit with NO fill: repair needed, never COMPLETED
                self._transition_terminal_locked(intent_id, "FAILED_NO_FILL")
                terminal = "FAILED_NO_FILL"
            elif any(s == "FILLED" for s in statuses) and \
                    all(s in TERMINAL_LEG for s in statuses):
                self._transition_terminal_locked(intent_id, "PARTIAL")
                terminal = "PARTIAL"
            cur = self.get(intent_id)
            return {"legs": cur["legs"], "terminal": terminal, "blocked": blocked}
        return self._locked(_do, intent_id=intent_id)

    def _transition_terminal_locked(self, intent_id: str, status: str) -> None:
        def builder(c):
            rec = dict(c)
            rec["version"] = c.get("version", 1) + 1
            rec["terminal"] = status
            return rec
        self._append_locked(intent_id, builder)

    # ── compaction / retention ────────────────────────────────────────
    def archive(self, intent_id: str) -> None:
        def _do():
            cur = self.get(intent_id)
            if cur.get("terminal") is None:
                raise IntentNotTerminalError(intent_id)
            # same generation fence before the archive write
            entry = _LOCK_HELD.get(self.lock_path)
            expected = None
            if entry is not None:
                expected = entry.setdefault("intent_versions", {}).get(intent_id)
            if expected is not None and cur["version"] != expected:
                raise StaleVersionError(
                    f"intent {intent_id} version drifted externally "
                    f"(holder saw {expected}, now {cur['version']})")
            rec = dict(cur)
            rec["version"] = cur.get("version", 1) + 1
            rec["retention_expires_at"] = time.time() + RETENTION_DAYS * 86400
            _atomic_append(self.archive_path, rec)
            if entry is not None:
                entry.setdefault("intent_versions", {})[intent_id] = rec["version"]
        self._locked(_do, intent_id=intent_id)

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


def dispatch_combined_exit(log: IntentLog, trade_id: str, order_mgr,
                           submit_hook: Optional[Callable] = None) -> dict:
    """Design §1.3: create intent (ids persisted) then CANONICAL submit_leg
    per leg (durable SUBMIT_ATTEMPTED before I/O, call-time hook, UNKNOWN on
    ambiguous). Each mutation internally locked; no direct order_mgr.submit."""
    iid = log.create(trade_id, "COMBINED_EXIT")
    for leg in ("NEAR", "FAR"):
        log.submit_leg(iid, leg, order_mgr, submit_hook=submit_hook)
    return {"intent_id": iid}
