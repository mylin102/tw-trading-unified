"""Runtime source adapter — skeletal (research-only, design + RED first).

Converts the ACTUAL runtime schemas (fills fill_type/timestamp/leg/
contract/side/trade_id; spread events event/ts/trade_id/order_id; BBO
telemetry event_type=BBO_UPDATE/leg/contract_code/exchange_ts_ms/
receive_ts_ms/source/bid/ask) into builder-native normalized records.

Every function raises NotImplementedError until the reviewed
implementation lands; RED contract tests fail independently.
"""

BBO_SOURCE_ALLOWLIST = ("shioaji_bidask",)
ANCHOR_EVENT_KINDS = ("RELEASE_DECISION", "SUBMISSION")
FILL_TYPES = ("fill",)  # release fill types accepted by the join


def adapt_fill(raw, source_ctx):
    """fills runtime record -> normalized fill:
    {trade_id, leg, contract, side, ts_text, parsed_ts, unit,
     source, record_no, byte_offset, byte_hash}. Malformed -> NOT_AVAILABLE."""
    raise NotImplementedError("source_adapter.adapt_fill")


def adapt_spread_event(raw, source_ctx):
    """spread events runtime record -> normalized event
    {trade_id, order_id, kind, ts_text, parsed_ts, unit, provenance}.
    Only RELEASE_DECISION/SUBMISSION are legal anchors; anything else ->
    NOT_AVAILABLE."""
    raise NotImplementedError("source_adapter.adapt_spread_event")


def adapt_bbo(raw, source_ctx):
    """BBO telemetry -> normalized quote
    {leg, contract, exchange_ts_ms, receive_ts_ms, source, bid, ask,
     provenance}. source=shioaji_bidask ONLY — no last-price/OHLC
    conversion."""
    raise NotImplementedError("source_adapter.adapt_bbo")


def normalize_sources(input_paths):
    """Read each source ONCE + hash exact bytes; adapt every record.
    Returns (fills, events, quotes, manifest_sources) — malformed/torn
    input or unsupported raw tick CSV -> ("REFUSED", reason)."""
    raise NotImplementedError("source_adapter.normalize_sources")


def join_anchor(fills, events):
    """Legal same-trade anchor: a RELEASE_DECISION/SUBMISSION event joined
    to its trade's release fills. Ambiguous/missing -> ("NOT_AVAILABLE",
    reason) — never synthesized."""
    raise NotImplementedError("source_adapter.join_anchor")


def join_positions(fills, anchor_trade):
    """Per-leg pre-decision position mapping from the trade's release
    fills: {"near": side, "far": side}. Missing leg -> NOT_AVAILABLE."""
    raise NotImplementedError("source_adapter.join_positions")


def build_normalized_snapshot(input_paths, out_dir):
    """Normalized snapshot (builder-native) + manifest with no-replace
    atomic writes. Existing out-dir/race -> ("REFUSED", reason) — zero
    partial output."""
    raise NotImplementedError("source_adapter.build_normalized_snapshot")
