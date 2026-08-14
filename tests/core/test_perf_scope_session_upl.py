"""Option B v2 (minimal slice per review): the scope's live fallback must
use the CURRENT Shioaji session's list_positions() per-leg pnl directly —
NOT the stale canonical JSON artifact.  Query failure / missing legs /
no api -> N/A.  Paper unchanged.  Provenance BROKER_CANONICAL_RUNTIME.
"""
from types import SimpleNamespace

from core.performance_provenance import scope_mts_performance


class _FakeApi:
    def __init__(self, positions=None, raise_exc=False):
        self.futopt_account = SimpleNamespace(account_id="123456")
        self._positions = positions or []
        self._raise = raise_exc

    def list_positions(self, account=None):
        if self._raise:
            raise RuntimeError("query failed")
        return self._positions


def _leg(code, direction, qty, avg_price, pnl):
    return SimpleNamespace(code=code, direction=direction, quantity=qty,
                           avg_price=avg_price, pnl=pnl)


def _full_positions():
    return [_leg("TMFH6", "S", 1, 45962.0, 650.0),
            _leg("TMFI6", "B", 1, 46088.0, -680.0)]


def _live_truth(session_id="sess-123"):
    return {"is_live_runtime": True, "is_paper_runtime": False,
            "session_id": session_id, "config_hash": "cfg-1"}


def _legacy_evidence():
    return {"evidence_mode": "legacy", "record_count": 392,
            "run_ids": [], "config_hashes": [], "sessions": [],
            "sources": [], "reason": "legacy ledger without provenance"}


def test_scope_ok_with_session_list_positions():
    api = _FakeApi(positions=_full_positions())
    scope = scope_mts_performance(_live_truth(), _legacy_evidence(), api=api)
    assert scope["ok"] is True
    assert scope["provenance"] == "BROKER_CANONICAL_RUNTIME"
    assert scope["mode"] == "live"


def test_scope_na_on_query_failure():
    api = _FakeApi(raise_exc=True)
    scope = scope_mts_performance(_live_truth(), _legacy_evidence(), api=api)
    assert scope["ok"] is False


def test_scope_na_missing_leg():
    api = _FakeApi(positions=[_leg("TMFH6", "S", 1, 45962.0, 650.0)])
    scope = scope_mts_performance(_live_truth(), _legacy_evidence(), api=api)
    assert scope["ok"] is False


def test_scope_na_zero_qty_leg():
    api = _FakeApi(positions=[_leg("TMFH6", "S", 0, 45962.0, 0.0),
                              _leg("TMFI6", "B", 1, 46088.0, -680.0)])
    scope = scope_mts_performance(_live_truth(), _legacy_evidence(), api=api)
    assert scope["ok"] is False


def test_scope_na_without_api():
    scope = scope_mts_performance(_live_truth(), _legacy_evidence(), api=None)
    assert scope["ok"] is False  # no stale canonical JSON fallback


def test_paper_unchanged_with_api():
    paper = {"is_live_runtime": False, "is_paper_runtime": True,
             "session_id": "sess-123", "config_hash": "cfg-1"}
    api = _FakeApi(positions=_full_positions())
    scope = scope_mts_performance(paper, _legacy_evidence(), api=api)
    assert scope["ok"] is True  # paper + legacy compat untouched
    assert scope.get("provenance") != "BROKER_CANONICAL_RUNTIME"
