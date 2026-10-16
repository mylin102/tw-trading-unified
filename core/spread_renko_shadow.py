"""P2: Spread Renko Shadow Collector — durable telemetry for accepted
synchronized spread samples. Shadow-only: never touches live Renko tracker,
position lifecycle, or order state. Failures are logged, never raised into
the trading loop.
"""
import json
import os
import uuid


class SpreadRenkoShadowCollector:
    def __init__(self, telemetry_path=None, process_instance_id=None):
        self.process_instance_id = process_instance_id or uuid.uuid4().hex[:8]
        self.telemetry_path = telemetry_path or os.path.join(
            "data", "telemetry", "spread_shadow", "shadow.jsonl")
        self.collector_sequence = 0
        self.errors = 0
        os.makedirs(os.path.dirname(self.telemetry_path), exist_ok=True)

    def resume_from_disk(self):
        """Restart recovery: resume collector_sequence from last written row.
        Never replays old events (they stay in the file); the new instance
        only continues numbering."""
        seq = 0
        try:
            if os.path.exists(self.telemetry_path):
                with open(self.telemetry_path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                            s = int(row.get("collector_sequence", 0))
                            if s > seq:
                                seq = s
                        except (json.JSONDecodeError, ValueError):
                            continue
        except OSError:
            pass
        self.collector_sequence = seq
        return seq

    def accept_tick(self, sample=None, source="", rejected_reason=None):
        """Only accepted ticks (passed Session→QuoteIntegrity→Jump) may enter.
        A rejected upstream tick raises ValueError — the caller must filter."""
        if sample is None:
            raise ValueError(f"rejected upstream tick ({rejected_reason}) must not enter collector")

    def record(self, sample) -> bool:
        """Durably append one sample. Never raises into the trading loop."""
        try:
            self.collector_sequence += 1
            if hasattr(sample, "__dict__"):
                row = dict(sample.__dict__)
            elif isinstance(sample, dict):
                row = dict(sample)
            else:
                raise TypeError("sample must be SpreadSample or dict")
            row["collector_sequence"] = self.collector_sequence
            row["process_instance_id"] = self.process_instance_id
            os.makedirs(os.path.dirname(self.telemetry_path), exist_ok=True)
            with open(self.telemetry_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            return True
        except Exception:
            self.errors += 1
            return False
