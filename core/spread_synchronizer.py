"""P2: Spread Synchronizer — canonical near/far pairing with freshness/skew
gates. Latest-snapshot pairing (NOT one-to-one), shadow-only, no live mutation.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class SpreadSample:
    schema_version: int
    episode_id: str
    trade_id: Optional[str]
    session_id: str
    near_contract: str
    far_contract: str
    near_sequence: int
    far_sequence: int
    near_timestamp: float
    far_timestamp: float
    pairing_skew_ms: float
    spread_value: float
    collector_sequence: int
    process_instance_id: str
    validity: str = "VALID"
    rejection_reason: Optional[str] = None


class SpreadSynchronizer:
    """Latest-snapshot pairing with strict gates.

    A sample is emitted only when:
    - same contract pair (near_code/far_code match)
    - same session_id
    - both legs fresh (age <= max_leg_age_ms from now)
    - pair skew <= max_pairing_skew_ms
    - source sequences monotonic per leg (out-of-order rejected)
    - one tick consumed at most once (dedupe by (leg, seq))
    """

    def __init__(self, near_code="TMFH6", far_code="TMFI6",
                 max_leg_age_ms=3000.0, max_pairing_skew_ms=2000.0,
                 session_id="", process_instance_id="P2"):
        self.near_code = near_code
        self.far_code = far_code
        self.max_leg_age_ms = float(max_leg_age_ms)
        self.max_pairing_skew_ms = float(max_pairing_skew_ms)
        self.session_id = session_id
        self.process_instance_id = process_instance_id
        self._near = None       # latest accepted near quote
        self._far = None        # latest accepted far quote
        self._near_last_seq = -1
        self._far_last_seq = -1
        self._consumed = set()  # (leg, seq) — one tick consumed at most once
        self.samples: List[SpreadSample] = []
        self.rejections: List[str] = []
        self.last_rejection: Optional[str] = None
        self.last_rejection_code: Optional[str] = None
        self._seq = 0

    def _accept_leg(self, leg, quote):
        """Common leg validation. Returns error string or None."""
        code = quote.get("code", "")
        expected = self.near_code if leg == "near" else self.far_code
        if code != expected:
            return "CONTRACT_PAIR_MISMATCH"
        sess = quote.get("session_id", "")
        if sess and self.session_id and sess != self.session_id:
            return "SESSION_MISMATCH"
        seq = int(quote.get("seq", -1))
        last = self._near_last_seq if leg == "near" else self._far_last_seq
        if seq <= last:
            return "OUT_OF_ORDER_SEQUENCE"
        key = (leg, seq)
        if key in self._consumed:
            return "DUPLICATE_TICK"
        # freshness vs pairing partner
        partner = self._far if leg == "near" else self._near
        if partner is not None:
            skew = abs(float(quote.get("ts_ms", 0)) - float(partner.get("ts_ms", 0)))
            if skew > self.max_pairing_skew_ms:
                return "PAIR_SKEW_EXCEEDED"
            age = abs(float(quote.get("ts_ms", 0)) - float(partner.get("ts_ms", 0)))
            if age > self.max_leg_age_ms:
                return "STALE_LEG"
        return None

    def on_near(self, quote: Dict) -> Optional[SpreadSample]:
        err = self._accept_leg("near", quote)
        if err:
            self.rejections.append(err)
            self.last_rejection = err
            self.last_rejection_code = quote.get("code", "")
            return None
        self._consumed.add(("near", int(quote["seq"])))
        self._near_last_seq = int(quote["seq"])
        self._near = quote
        return self._try_pair()

    def on_far(self, quote: Dict) -> Optional[SpreadSample]:
        err = self._accept_leg("far", quote)
        if err:
            self.rejections.append(err)
            self.last_rejection = err
            self.last_rejection_code = quote.get("code", "")
            return None
        self._consumed.add(("far", int(quote["seq"])))
        self._far_last_seq = int(quote["seq"])
        self._far = quote
        return self._try_pair()

    def _try_pair(self) -> Optional[SpreadSample]:
        if self._near is None or self._far is None:
            return None
        skew = abs(float(self._near["ts_ms"]) - float(self._far["ts_ms"]))
        if skew > self.max_pairing_skew_ms:
            return None
        self._seq += 1
        sample = SpreadSample(
            schema_version=1,
            episode_id=self.session_id or "EPISODE_0",
            trade_id=None,
            session_id=self.session_id,
            near_contract=self._near["code"],
            far_contract=self._far["code"],
            near_sequence=int(self._near["seq"]),
            far_sequence=int(self._far["seq"]),
            near_timestamp=float(self._near["ts_ms"]),
            far_timestamp=float(self._far["ts_ms"]),
            pairing_skew_ms=skew,
            spread_value=float(self._near["price"]) - float(self._far["price"]),
            collector_sequence=self._seq,
            process_instance_id=self.process_instance_id,
        )
        self.samples.append(sample)
        return sample
