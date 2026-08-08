"""Runtime source adapter — skeletal v2 (research-only, design + RED first).

Converts the ACTUAL runtime schemas:
- fills: JSONL lines; fill_type ∈ {ENTRY, RELEASE, EXIT, COMBINED_EXIT};
  ENTRY sides are LONG/SHORT; contract is the NEAR/FAR LABEL
- spread events: JSONL; event/ts/trade_id/order_id (RELEASE_DECISION/
  SUBMISSION = legal anchors)
- BBO: JSONL; event_type=BBO_UPDATE/leg/contract_code/exchange_ts_ms/
  receive_ts_ms/source/bid/ask; contract_code is the ACTUAL code
  (TMFH6/TMFI6) which CHANGES after monthly settlement/roll — the
  contract mapping is resolved PER decision timestamp from authoritative
  records or a versioned mapping input, NEVER a global hardcode.

Every function raises NotImplementedError until the reviewed
implementation lands; RED contract tests fail independently.
"""

BBO_SOURCE_ALLOWLIST = ("shioaji_bidask",)
ANCHOR_EVENT_KINDS = ("RELEASE_DECISION", "SUBMISSION")
FILL_TYPES = ("ENTRY", "RELEASE", "EXIT", "COMBINED_EXIT")


def adapt_fill(raw, source_ctx):
    """fills JSONL record -> normalized fill. fill_type must be a known
    enum; ENTRY sides are LONG/SHORT with qty/side validation. contract is
    the NEAR/FAR LABEL (never compared to actual codes). Malformed ->
    NOT_AVAILABLE."""
    raise NotImplementedError("source_adapter.adapt_fill")


def adapt_spread_event(raw, source_ctx):
    """spread events JSONL record -> normalized event. Only
    RELEASE_DECISION/SUBMISSION are legal anchors."""
    raise NotImplementedError("source_adapter.adapt_spread_event")


def adapt_bbo(raw, source_ctx):
    """BBO telemetry JSONL record -> normalized quote with the ACTUAL
    contract code. source=shioaji_bidask ONLY — no last-price/OHLC
    conversion."""
    raise NotImplementedError("source_adapter.adapt_bbo")


def normalize_sources(input_paths):
    """Parse each JSONL source ONCE from bytes; preserve the EXACT per-line
    byte offset + record number; malformed/torn line or unsupported raw
    tick CSV -> ("REFUSED", reason). Returns (fills, events, quotes,
    manifest_sources)."""
    raise NotImplementedError("source_adapter.normalize_sources")


def join_anchor(fills, events):
    """Legal same-trade anchor joined to its trade's release fills."""
    raise NotImplementedError("source_adapter.join_anchor")


def join_positions(fills, anchor_trade):
    """Per-leg pre-decision positions from ENTRY LONG/SHORT fills (qty/
    side validated). Missing leg -> NOT_AVAILABLE."""
    raise NotImplementedError("source_adapter.join_positions")


def resolve_contract_mapping(records, mapping_input, decision_ts_ms):
    """Per-decision contract mapping {near: code, far: code, evidence,
    version, hash} from authoritative records or a versioned mapping input
    (validity windows). Codes change after monthly settlement — resolution
    is per-window; missing/ambiguous (e.g. at a roll boundary) ->
    NOT_AVAILABLE, never a guess."""
    raise NotImplementedError("source_adapter.resolve_contract_mapping")


def build_normalized_snapshot(input_paths, out_dir, mapping_input=None):
    """Normalized snapshot (builder-native) + manifest (with contract
    mapping evidence/version/hash) using no-replace atomic writes."""
    raise NotImplementedError("source_adapter.build_normalized_snapshot")
