"""Builder module — skeletal. All contracts raise NotImplementedError."""


def build_snapshot(input_paths, out_dir):
    raise NotImplementedError("event_snapshot.builder.build_snapshot")


def read_source_once(path):
    raise NotImplementedError("event_snapshot.builder.read_source_once")


def legal_anchor(records):
    raise NotImplementedError("event_snapshot.builder.legal_anchor")


def attach_provenance(record):
    raise NotImplementedError("event_snapshot.builder.attach_provenance")


def order_events(events):
    raise NotImplementedError("event_snapshot.builder.order_events")


def attach_quotes(events, quote_records):
    raise NotImplementedError("event_snapshot.builder.attach_quotes")


def emit_manifest(out_dir, events, sources):
    raise NotImplementedError("event_snapshot.builder.emit_manifest")
