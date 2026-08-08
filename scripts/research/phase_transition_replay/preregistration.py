"""Committed immutable pre-registration manifest (research-only, v6).

ALL runnable parameters — M_economic, fee assumptions, staleness bounds,
pair-skew bound, config version, classifier id — resolve from THIS
committed source. The runner accepts a selector (--prereg <id>) with NO
value defaults; the manifest records the prereg id, its sha256 AND the git
provenance (repo HEAD, file hashes, dirty status — verified by the runner).

MUST stay immutable: changing a value is a NEW preregistration id + new
hash (never an in-place edit).
"""

import hashlib
import json

PREREGISTRATION = {
    "prereg-v1": {
        "m_economic": 25.0,
        "fee_assumption_id": "fee-v1",
        "fee_assumptions": {
            "fee-v1": {
                "per_leg": 50.0,
                "slippage_ticks": 1,
                "description": "default research fee/slippage set",
            }
        },
        "staleness": {"max_age_s": 30},
        "max_pair_skew_ms": 1000,
        "timestamp_unit": "epoch_ms",
        "timestamp_validator_version": "v1",
        "config_version": "research-v1",
        "classifier": "frozen-precedence-2026-08-08",
    }
}

# canonical sha of the committed preregistration payload
PREREGISTRATION_SHA = hashlib.sha256(
    json.dumps(PREREGISTRATION, sort_keys=True, ensure_ascii=False)
    .encode("utf-8")
).hexdigest()


def preregistration(id_):
    """Return a deep copy of the preregistration params for id_.

    Unknown id -> KeyError (fail-closed — never a silent default).
    """
    import copy
    return copy.deepcopy(PREREGISTRATION[id_])


def prereg_ids():
    return sorted(PREREGISTRATION)


def prereg_sha(id_):
    """Per-id canonical sha (subtree of the committed manifest)."""
    subtree = {id_: PREREGISTRATION[id_]}
    return hashlib.sha256(
        json.dumps(subtree, sort_keys=True, ensure_ascii=False)
        .encode("utf-8")
    ).hexdigest()
