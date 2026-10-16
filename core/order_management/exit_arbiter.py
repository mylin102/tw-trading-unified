# 2026-08-03 Gemini CLI: Independent Candidate Producers + Atomic Exit Arbiter for MTS Single-Leg Exit Architecture
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class CandidatePriority(Enum):
    EMERGENCY = 100
    POLICY_J_GIVEBACK = 80
    SINGLE_LEG_TRAIL = 60
    RENKO_SHADOW = 40


@dataclass
class ExitClaimRequest:
    """Atomic exit claim request representing a candidate exit trigger."""
    trade_id: str
    single_leg_episode_id: str
    lifecycle_phase: str
    state_revision: int
    candidate_event_id: str
    owner: str  # e.g. "POLICY_J", "SINGLE_LEG_TRAIL", "EMERGENCY"
    exit_reason: str
    source_receive_sequence: int
    trigger_timestamp: str
    priority: CandidatePriority = CandidatePriority.SINGLE_LEG_TRAIL
    open_quantity: int = 1
    executable_upl_twd: float = 0.0
    total_net_pnl_twd: float = 0.0
    is_shadow: bool = False  # If True (e.g. Renko), cannot claim real execution


@dataclass
class ExitClaimResult:
    """Result of an exit claim arbitration."""
    success: bool
    claim_id: str
    winner_request: Optional[ExitClaimRequest]
    state_revision: int
    arbitration_reason: str
    same_source_tick: bool = False
    all_triggered_candidates: List[Dict[str, Any]] = field(default_factory=list)
    suppressed_candidates: List[Dict[str, Any]] = field(default_factory=list)


class ExitArbiter:
    """Thread-safe, atomic exit arbiter for SINGLE_LEG phase exit candidates."""

    def __init__(self):
        self._lock = threading.Lock()
        self._active_claims: Dict[str, ExitClaimRequest] = {}  # episode_id -> winning ExitClaimRequest
        self._state_revisions: Dict[str, int] = {}             # episode_id -> current revision
        self._telemetry_ledger: List[Dict[str, Any]] = []

    def get_state_revision(self, episode_id: str) -> int:
        with self._lock:
            return self._state_revisions.get(episode_id, 1)

    def increment_revision(self, episode_id: str) -> int:
        with self._lock:
            rev = self._state_revisions.get(episode_id, 1) + 1
            self._state_revisions[episode_id] = rev
            return rev

    def try_claim(self, requests: List[ExitClaimRequest]) -> ExitClaimResult:
        """Arbitrate candidate requests from a source tick and atomically claim exit ownership."""
        with self._lock:
            if not requests:
                return ExitClaimResult(
                    success=False,
                    claim_id="",
                    winner_request=None,
                    state_revision=0,
                    arbitration_reason="NO_REQUESTS",
                )

            episode_id = requests[0].single_leg_episode_id
            seq = requests[0].source_receive_sequence
            cur_rev = self._state_revisions.get(episode_id, 1)

            candidate_dicts = []
            for req in requests:
                candidate_dicts.append({
                    "candidate_event_id": req.candidate_event_id,
                    "owner": req.owner,
                    "exit_reason": req.exit_reason,
                    "priority": req.priority.value,
                    "is_shadow": req.is_shadow,
                    "trigger_timestamp": req.trigger_timestamp,
                    "total_net_pnl_twd": req.total_net_pnl_twd,
                })

            # Check if episode is already claimed
            if episode_id in self._active_claims:
                existing_winner = self._active_claims[episode_id]
                telemetry = {
                    "event": "ARBITRATION_SUPPRESSED",
                    "trade_id": requests[0].trade_id,
                    "single_leg_episode_id": episode_id,
                    "source_receive_sequence": seq,
                    "same_source_tick_flag": True,
                    "all_triggered_candidates": candidate_dicts,
                    "winner": existing_winner.candidate_event_id,
                    "suppressed_candidates": candidate_dicts,
                    "arbitration_reason": f"EPISODE_ALREADY_CLAIMED_BY_{existing_winner.owner}",
                }
                self._telemetry_ledger.append(telemetry)
                return ExitClaimResult(
                    success=False,
                    claim_id="",
                    winner_request=existing_winner,
                    state_revision=cur_rev,
                    arbitration_reason=f"ALREADY_CLAIMED_BY_{existing_winner.owner}",
                    same_source_tick=True,
                    all_triggered_candidates=candidate_dicts,
                    suppressed_candidates=candidate_dicts,
                )

            # Filter out shadow candidates (cannot enter real claim)
            eligible = [r for r in requests if not r.is_shadow]
            if not eligible:
                telemetry = {
                    "event": "ARBITRATION_SHADOW_ONLY",
                    "trade_id": requests[0].trade_id,
                    "single_leg_episode_id": episode_id,
                    "source_receive_sequence": seq,
                    "same_source_tick_flag": True,
                    "all_triggered_candidates": candidate_dicts,
                    "winner": None,
                    "suppressed_candidates": candidate_dicts,
                    "arbitration_reason": "ALL_CANDIDATES_ARE_SHADOW",
                }
                self._telemetry_ledger.append(telemetry)
                return ExitClaimResult(
                    success=False,
                    claim_id="",
                    winner_request=None,
                    state_revision=cur_rev,
                    arbitration_reason="ALL_CANDIDATES_ARE_SHADOW",
                    same_source_tick=len(requests) > 1,
                    all_triggered_candidates=candidate_dicts,
                    suppressed_candidates=candidate_dicts,
                )

            # Same-tick arbitration: sort by priority DESC (higher enum value first), then total_net_pnl_twd
            sorted_eligible = sorted(
                eligible, key=lambda r: (r.priority.value, r.total_net_pnl_twd), reverse=True
            )
            winner = sorted_eligible[0]
            suppressed = [
                c for c in candidate_dicts if c["candidate_event_id"] != winner.candidate_event_id
            ]

            # Register winner claim & increment state revision
            claim_id = f"CLAIM-{winner.single_leg_episode_id}-{uuid.uuid4().hex[:8]}"
            self._active_claims[episode_id] = winner
            new_rev = cur_rev + 1
            self._state_revisions[episode_id] = new_rev

            telemetry = {
                "event": "ARBITRATION_WINNER_CLAIMED",
                "claim_id": claim_id,
                "trade_id": winner.trade_id,
                "single_leg_episode_id": episode_id,
                "lifecycle_phase": winner.lifecycle_phase,
                "state_revision": new_rev,
                "source_receive_sequence": seq,
                "same_source_tick_flag": len(requests) > 1,
                "winner": {
                    "candidate_event_id": winner.candidate_event_id,
                    "owner": winner.owner,
                    "exit_reason": winner.exit_reason,
                    "priority": winner.priority.name,
                },
                "all_triggered_candidates": candidate_dicts,
                "suppressed_candidates": suppressed,
                "arbitration_reason": f"DETERMINISTIC_PRIORITY_{winner.priority.name}_WINS",
            }
            self._telemetry_ledger.append(telemetry)

            return ExitClaimResult(
                success=True,
                claim_id=claim_id,
                winner_request=winner,
                state_revision=new_rev,
                arbitration_reason=f"DETERMINISTIC_PRIORITY_{winner.priority.name}_WINS",
                same_source_tick=len(requests) > 1,
                all_triggered_candidates=candidate_dicts,
                suppressed_candidates=suppressed,
            )

    def release_claim(self, episode_id: str, reason: str = "SUBMIT_FAILURE") -> None:
        """Release claim on submit failure or timeout, allowing retry/escalation path."""
        with self._lock:
            if episode_id in self._active_claims:
                claimed = self._active_claims.pop(episode_id)
                logger.warning(
                    "[EXIT_ARBITER_CLAIM_RELEASED] episode_id=%s owner=%s reason=%s",
                    episode_id, claimed.owner, reason
                )

    def complete_claim(self, episode_id: str) -> None:
        """Complete claim upon FLAT transition."""
        with self._lock:
            self._active_claims.pop(episode_id, None)

    def get_telemetry_ledger(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._telemetry_ledger)
