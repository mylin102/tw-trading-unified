"""Production invariants (I3 FAILPATH + exit-code contract).

2026-11-11 Hermes Agent. Two invariants are locked here:

I3-FAILPATH (scripts/ci/check_no_swallowed_except.py)
    A handler that wraps an order/exit/session operation must record
    evidence or re-raise. Silent `pass` / `continue` around `submit(...)`
    is how a real order failure becomes an invisible one.

EXIT-CODE CONTRACT (main._shutdown_exit_code)
    2026-11-07 and 2026-11-10: contract resolution failed, the process
    exited 0, and PM2's `stop_exit_codes: [0]` treated that as a
    deliberate operator stop -> no restart -> dead for 83 minutes, twice.
    Only an operator stop or a signal may map to exit 0.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))

import check_no_swallowed_except as gate  # noqa: E402


# ── I3 FAILPATH: the gate must bite on the shape it exists to catch ──

def _scan(tmp_path: Path):
    return gate.scan_tree(tmp_path)


def test_gate_flags_silent_handler_around_submit(tmp_path):
    (tmp_path / "bad.py").write_text(
        "def f(order):\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "    except Exception:\n"
        "        pass\n"
    )
    found = _scan(tmp_path)
    assert len(found) == 1
    assert found[0]["line"] == 4
    assert "submit" in found[0]["wrapped_call"]


def test_gate_flags_silent_continue_around_release(tmp_path):
    (tmp_path / "bad.py").write_text(
        "def f(items):\n"
        "    for i in items:\n"
        "        try:\n"
        "            strat.release(i)\n"
        "        except Exception:\n"
        "            continue\n"
    )
    assert len(_scan(tmp_path)) == 1


def test_gate_flags_bare_except_around_execute_trade(tmp_path):
    (tmp_path / "bad.py").write_text(
        "def f():\n"
        "    try:\n"
        "        self._execute_trade('EXIT', 1.0)\n"
        "    except:\n"
        "        pass\n"
    )
    assert len(_scan(tmp_path)) == 1


def test_gate_requires_annotation_even_when_logging(tmp_path):
    """Logging alone is not the standard: the handler must declare whether
    the failure is survivable (agent_engineering_governance I3)."""
    (tmp_path / "bad.py").write_text(
        "def f(order):\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "    except Exception:\n"
        "        logger.warning('submit failed', exc_info=True)\n"
    )
    found = _scan(tmp_path)
    assert len(found) == 1
    assert found[0]["severity"] == "WARN"
    assert found[0]["problem"].startswith("no FAILPATH annotation")


def test_gate_allows_annotated_recoverable_handler(tmp_path):
    (tmp_path / "ok.py").write_text(
        "def f(order):\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "    except Exception as exc:\n"
        "        # FAILPATH: RECOVERABLE reason=ORDER_STATE_RACE\n"
        "        logger.warning('submit failed: %s', exc)\n"
    )
    assert _scan(tmp_path) == []


def test_gate_flags_annotated_recoverable_without_evidence(tmp_path):
    (tmp_path / "bad.py").write_text(
        "def f(order):\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "    except Exception:\n"
        "        # FAILPATH: RECOVERABLE reason=ORDER_STATE_RACE\n"
        "        pass\n"
    )
    found = _scan(tmp_path)
    assert len(found) == 1
    assert "no log/event evidence" in found[0]["problem"]


def test_gate_flags_fatal_annotation_without_raise(tmp_path):
    """Declaring FATAL while silently continuing is the exact contradiction
    the annotation exists to expose."""
    (tmp_path / "bad.py").write_text(
        "def f(order):\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "    except Exception:\n"
        "        # FAILPATH: FATAL reason=POSITION_STATE_UNREADABLE\n"
        "        logger.error('submit failed')\n"
    )
    found = _scan(tmp_path)
    assert len(found) == 1
    assert "does not raise" in found[0]["problem"]


def test_gate_allows_fatal_annotation_with_raise(tmp_path):
    (tmp_path / "ok.py").write_text(
        "def f(order):\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "    except Exception:\n"
        "        # FAILPATH: FATAL reason=POSITION_STATE_UNREADABLE\n"
        "        raise\n"
    )
    assert _scan(tmp_path) == []


def test_gate_flags_unregistered_reason(tmp_path):
    (tmp_path / "bad.py").write_text(
        "def f(order):\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "    except Exception:\n"
        "        # FAILPATH: RECOVERABLE reason=MADE_UP_REASON\n"
        "        logger.warning('x')\n"
    )
    found = _scan(tmp_path)
    assert len(found) == 1
    assert "unregistered reason" in found[0]["problem"]


def test_gate_allows_reraise(tmp_path):
    (tmp_path / "ok.py").write_text(
        "def f(order):\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "    except Exception:\n"
        "        raise\n"
    )
    assert _scan(tmp_path) == []


def test_gate_ignores_silent_handler_around_non_order_call(tmp_path):
    """Scoping rule: a silent handler around unrelated IO is noise, not
    an order hazard -- it must NOT be flagged (keeps signal high)."""
    (tmp_path / "fine.py").write_text(
        "def f(obj):\n"
        "    try:\n"
        "        json.dumps(obj)\n"
        "    except Exception:\n"
        "        pass\n"
    )
    assert _scan(tmp_path) == []


# ── EXIT-CODE CONTRACT ──

def test_gate_allows_unknown_with_refusal_return(tmp_path):
    (tmp_path / "ok.py").write_text(
        "def f(order):\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "    except Exception:\n"
        "        # FAILPATH: UNKNOWN reason=SUBMIT_STATUS_UNKNOWN\n"
        "        return None\n"
    )
    assert _scan(tmp_path) == []


def test_gate_allows_unknown_with_falsy_sentinel(tmp_path):
    """`ok = False` then `return ok` is the refusal shape used by the
    exit-intent submit paths."""
    (tmp_path / "ok.py").write_text(
        "def f(order):\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "    except Exception:\n"
        "        # FAILPATH: UNKNOWN reason=EXIT_SUBMIT_FAILED\n"
        "        ok = False\n"
        "    return ok\n"
    )
    assert _scan(tmp_path) == []


def test_gate_allows_unknown_with_reject_call(tmp_path):
    (tmp_path / "ok.py").write_text(
        "def f(order):\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "    except Exception:\n"
        "        # FAILPATH: UNKNOWN reason=ADAPTER_SUBMIT_REJECTED\n"
        "        self.reject(order.order_id, reason='X')\n"
        "        return False\n"
    )
    assert _scan(tmp_path) == []


def test_gate_flags_unknown_that_only_logs(tmp_path):
    """Declaring UNKNOWN while merely logging is the contradiction the
    classification exists to expose."""
    (tmp_path / "bad.py").write_text(
        "def f(order):\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "    except Exception:\n"
        "        # FAILPATH: UNKNOWN reason=SUBMIT_STATUS_UNKNOWN\n"
        "        logger.warning('unknown submit state')\n"
    )
    found = _scan(tmp_path)
    assert len(found) == 1
    assert "neither raises nor refuses" in found[0]["problem"]


def test_gate_allows_sentinel_default_refusal(tmp_path):
    """A tick-local decision sentinel left at its None default IS a refusal:
    the tick performs no action. Verified structurally, not asserted."""
    (tmp_path / "ok.py").write_text(
        "def tick(self, order):\n"
        "    decision = None\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "        decision = self.adapter.evaluate()\n"
        "    except Exception:\n"
        "        # FAILPATH: UNKNOWN reason=LIFECYCLE_EVAL_FAILED refusal=sentinel-default(decision)\n"
        "        logger.exception('eval failed')\n"
        "    if decision is not None:\n"
        "        return decision.action\n"
    )
    assert _scan(tmp_path) == []


def test_gate_flags_sentinel_default_when_handler_assigns_it(tmp_path):
    """Claiming 'left at default' while overwriting it is a false claim."""
    (tmp_path / "bad.py").write_text(
        "def tick(self, order):\n"
        "    decision = None\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "        decision = self.adapter.evaluate()\n"
        "    except Exception:\n"
        "        # FAILPATH: UNKNOWN reason=LIFECYCLE_EVAL_FAILED refusal=sentinel-default(decision)\n"
        "        decision = self.adapter.evaluate()\n"
        "        logger.exception('retry')\n"
    )
    found = _scan(tmp_path)
    assert len(found) == 1
    assert "handler assigns it" in found[0]["problem"]


def test_gate_flags_sentinel_default_without_none_init(tmp_path):
    """If the sentinel is not initialised before the try it could hold a
    value from an earlier tick, so 'no action' is not guaranteed."""
    (tmp_path / "bad.py").write_text(
        "def tick(self, order):\n"
        "    decision = self.last_decision\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "        decision = self.adapter.evaluate()\n"
        "    except Exception:\n"
        "        # FAILPATH: UNKNOWN reason=LIFECYCLE_EVAL_FAILED refusal=sentinel-default(decision)\n"
        "        logger.exception('eval failed')\n"
    )
    found = _scan(tmp_path)
    assert len(found) == 1
    assert "not initialised to None" in found[0]["problem"]


def test_gate_flags_sentinel_default_without_log(tmp_path):
    (tmp_path / "bad.py").write_text(
        "def tick(self, order):\n"
        "    decision = None\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "        decision = self.adapter.evaluate()\n"
        "    except Exception:\n"
        "        # FAILPATH: UNKNOWN reason=LIFECYCLE_EVAL_FAILED refusal=sentinel-default(decision)\n"
        "        pass\n"
    )
    found = _scan(tmp_path)
    assert len(found) == 1
    assert "not logged" in found[0]["problem"]


def test_exit_code_contract():
    from main import _shutdown_exit_code

    # Only an operator stop or a POSIX signal is a clean exit. The signal
    # convention in main.py:2333 is "SIGNAL_<NAME>" (set_shutdown_cause),
    # verified against the live source rather than assumed.
    assert _shutdown_exit_code("SIGNAL_SIGTERM") == 0
    assert _shutdown_exit_code("SIGNAL_SIGINT") == 0
    assert _shutdown_exit_code("SIGNAL_SIGHUP") == 0
    assert _shutdown_exit_code("OPERATOR_STOP") == 0

    # A bare signal name is NOT the contract's spelling -- it must fall
    # through to the non-zero default rather than silently be treated as
    # an operator stop.
    assert _shutdown_exit_code("SIGTERM") != 0

    # Everything else must be visible to the supervisor as a failure.
    for fatal in ("CRITICAL_CRASH", "MAIN_LOOP_RETURNED", "MONITOR_STOPPED",
                  "RECOVERABLE_SESSION_LOSS", "STARTUP_INVARIANT_VIOLATED",
                  "CONTRACT_RESOLUTION_FAILED", "SESSION_LOGOUT"):
        assert _shutdown_exit_code(fatal) != 0, (
            f"{fatal} must not exit 0 -- PM2 stop_exit_codes:[0] would "
            f"treat it as a deliberate operator stop and never restart")


def test_recoverable_session_loss_uses_distinct_code():
    """Distinct non-zero code so operators can tell 'retry may help' from
    'this process must not run'."""
    from main import _shutdown_exit_code
    assert _shutdown_exit_code("RECOVERABLE_SESSION_LOSS") == 75
    assert _shutdown_exit_code("CRITICAL_CRASH") == 1


# ── I1 EVENT-SCHEMA ──

def _schema_scan(tmp_path):
    import importlib
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "ci"))
    mod = importlib.import_module("check_event_schema")
    return mod.check(tmp_path)


def test_event_schema_flags_drift(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "emitter.py").write_text(
        "def a(self):\n"
        "    self._append_mts_event('ORDER_SUBMITTED', trade_id=1, contract='X')\n"
        "def b(self):\n"
        "    self._append_mts_event('ORDER_SUBMITTED', trade_id=1)\n"
    )
    found = _schema_scan(tmp_path)
    kinds = {v["kind"] for v in found}
    assert "SCHEMA_DRIFT" in kinds
    drift = [v for v in found if v["kind"] == "SCHEMA_DRIFT"][0]
    assert drift["event"] == "ORDER_SUBMITTED"


def test_event_schema_accepts_one_shape(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "emitter.py").write_text(
        "def a(self):\n"
        "    self._append_mts_event('ORDER_SUBMITTED', trade_id=1, contract='X')\n"
        "def b(self):\n"
        "    self._append_mts_event('ORDER_SUBMITTED', contract='Y', trade_id=2)\n"
    )
    assert _schema_scan(tmp_path) == []


def test_event_schema_flattens_static_dict_splat(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "emitter.py").write_text(
        "def a(self):\n"
        "    self._append_mts_event('ORDER_SUBMITTED', **{'trade_id': 1, 'qty': 2})\n"
        "def b(self):\n"
        "    self._append_mts_event('ORDER_SUBMITTED', trade_id=3, qty=4)\n"
    )
    assert _schema_scan(tmp_path) == []


def test_event_schema_flags_non_literal_name(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "emitter.py").write_text(
        "def a(self, name):\n"
        "    self._append_mts_event(name, trade_id=1)\n"
    )
    found = _schema_scan(tmp_path)
    assert any(v["kind"] == "NON_LITERAL_EVENT_NAME" for v in found)


def test_event_schema_flags_unverifiable_splat(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "emitter.py").write_text(
        "def a(self, payload):\n"
        "    self._append_mts_event('ORDER_SUBMITTED', **payload)\n"
    )
    found = _schema_scan(tmp_path)
    assert any(v["kind"] == "UNVERIFIABLE_FIELDS" for v in found)


def test_event_schema_reads_name_from_second_arg_for_ilog(tmp_path):
    """_ilog.append_event(intent_id, name, **fields): the name is argument 1,
    not 0. Treating arg 0 as the name produced false NON_LITERAL reports."""
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "emitter.py").write_text(
        "def a(self, log, iid):\n"
        "    log.append_event(iid, 'POLICY_J_SINGLE_LEG_TRIGGERED', trade_id=1)\n"
        "def b(self, log, iid):\n"
        "    log.append_event(iid, 'POLICY_J_SINGLE_LEG_TRIGGERED', trade_id=2)\n"
    )
    assert _schema_scan(tmp_path) == []


def test_event_schema_ignores_object_based_append_event(tmp_path):
    """lifecycle_provenance._append_event(event_obj) takes an object, not a
    name; it is not a name-based API and must not be schema-checked."""
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "emitter.py").write_text(
        "def a(self, event):\n"
        "    self._append_event(event)\n"
    )
    assert _schema_scan(tmp_path) == []


# ── RESERVED ENVELOPE KEYS ──

def test_normalize_never_returns_envelope_keys():
    """ORDER_SUBMITTED passes `event=` as a payload field. If normalize()
    echoed it back, event.update() would overwrite the event name with the
    payload value -- observed in production on 2026-11-11 as two records with
    "event": null right after the schema lock shipped."""
    from core.event_schema import normalize
    out = normalize("ORDER_SUBMITTED", {"contract": "TMF", "event": "X", "ts": "Y"})
    assert "event" not in out
    assert "ts" not in out


def test_normalize_fills_declared_fields_and_drops_undeclared():
    from core.event_schema import EVENT_FIELDS, normalize
    declared = [f for f in sorted(EVENT_FIELDS["ORDER_SUBMITTED"])
                if f not in ("event", "ts")]
    out = normalize("ORDER_SUBMITTED", {"contract": "TMF", "not_a_field": 1})
    assert "not_a_field" not in out
    for field in declared:
        assert field in out, f"{field} missing from the normalised record"
