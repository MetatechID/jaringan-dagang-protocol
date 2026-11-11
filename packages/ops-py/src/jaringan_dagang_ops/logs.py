"""JSON log lines carrying the current trace and business keys."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from . import context

_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = context.current()
        if ctx:
            out["trace_id"], out["span_id"] = ctx.trace_id, ctx.span_id
            if ctx.request_id:
                out["request_id"] = ctx.request_id
        out.update(context.refs())
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_") and key not in out:
                try:
                    json.dumps(value)
                    out[key] = value
                except (TypeError, ValueError):
                    out[key] = repr(value)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def configure_logging(service: str, level: int | None = None) -> None:
    """Switch the root and uvicorn handlers to JSON lines. Safe to call twice."""
    formatter = JsonFormatter(service)
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    if level is not None:
        root.setLevel(level)
    targets = [root, *(logging.getLogger(n) for n in ("uvicorn", "uvicorn.error", "uvicorn.access"))]
    for logger in targets:
        for handler in logger.handlers:
            handler.setFormatter(formatter)
