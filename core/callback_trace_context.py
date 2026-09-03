"""Synchronous callback correlation for non-blocking telemetry only."""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

_current: ContextVar[dict[str, Any] | None] = ContextVar(
    "callback_trace_context", default=None
)


def begin(payload: dict[str, Any]):
    return _current.set(dict(payload))


def end(token) -> None:
    _current.reset(token)


def current() -> dict[str, Any] | None:
    value = _current.get()
    return dict(value) if value else None


def emit(stage: str, **fields: Any) -> None:
    context = _current.get()
    if not context:
        return
    hook = context.get("hook")
    if hook is None:
        return
    payload = dict(context)
    payload.pop("hook", None)
    payload.update(fields)
    payload["stage"] = stage
    try:
        hook.observe_callback(**payload)
    except Exception:
        # Telemetry must never affect callback dispatch.
        return
