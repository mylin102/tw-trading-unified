"""RED/GREEN: a ledger-derived FLAT alone may never erase a local position.

Live 2026-09-24 22:54:24: the POSITION_AUTHORITY gate reset the strategy to FLAT
(reason=POSITION_AUTHORITY_FLAT, writer mts_spread) while the broker still held
TMFJ6 Sell 1 / TMFK6 Buy 1.  The ledger authority said FLAT only because the
13:26 spread fill had been dropped downstream (negative-price normalizer), so
"ledger flat" was local evidence, not broker truth.  The 09-24 guard covered the
SPLIT_BRAIN path only; this guard belongs in the shared decision so that no
callsite can forget it.
"""
import inspect
import pathlib

from strategies.futures.mts_ledger_authority import (
    MtsAuthority,
    MtsAuthorityState,
    MtsGateAction,
    gate_decision_pre_signal,
)

_LEDGER_FLAT = MtsAuthorityState(MtsAuthority.FLAT)
_TID = "broker-reconciled-b44e20763a980f92"


def _decide(proof, strat_has_pos=True):
    return gate_decision_pre_signal(
        _LEDGER_FLAT,
        state_has_pos=False,
        strat_has_pos=strat_has_pos,
        strat_trade_id=_TID,
        broker_flat_proven=proof,
    )


def test_broker_not_flat_withholds_the_reset():
    """The live scenario: broker held both legs -> keep the local position."""
    assert _decide(False) is MtsGateAction.PASS


def test_missing_broker_proof_withholds_the_reset():
    """An unavailable/unknown broker query is not flat proof."""
    assert _decide(None) is MtsGateAction.PASS


def test_broker_flat_proof_still_resets():
    """A genuine divergence (broker proven flat) is still force-synced."""
    assert _decide(True) is MtsGateAction.RESET_STRATEGY


def test_pre_signal_decision_cannot_be_called_without_the_proof():
    """Structural: the proof is a REQUIRED keyword-only argument, so every
    callsite (present and future) must resolve broker truth explicitly."""
    param = inspect.signature(gate_decision_pre_signal).parameters["broker_flat_proven"]
    assert param.default is inspect.Parameter.empty
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_monitor_callsite_supplies_the_broker_proof():
    """The live callsite must pass the broker flat proof, not the ledger view."""
    src = pathlib.Path("strategies/futures/monitor.py").read_text(encoding="utf-8")
    assert "broker_flat_proven=self._broker_truth_is_flat(_broker_auth)" in src
