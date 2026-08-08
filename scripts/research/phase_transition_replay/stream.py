"""Immutable globally-ordered market-event stream — skeletal."""


def ordered_stream(events, clock_contract):
    raise NotImplementedError("stream.ordered_stream: source_event_seq/exchange_ts/recv_ts/replay_seq/stream hash/ordering key")
