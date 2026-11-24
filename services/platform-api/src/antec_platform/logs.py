"""Журнал в JSON: одна строка на событие, без секретов и текстов сообщений."""

from __future__ import annotations

import datetime as dt
import json
import logging
import traceback
from typing import Any, override

# color_message: копия сообщения uvicorn с ANSI-цветами.
_STANDARD = frozenset(vars(logging.makeLogRecord({})).keys()) | {
    "message",
    "asctime",
    "color_message",
}


def describe_exception(exc: BaseException | None) -> str:
    """Типы и места цепочки исключений без их текстов (ADR-0012, п. 5).

    Тексты ошибок SQL, Pydantic и HTTP могут содержать сообщения пользователя и аргументы.
    """
    parts: list[str] = []
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        frames = traceback.extract_tb(exc.__traceback__)
        where = " < ".join(f"{f.filename}:{f.lineno} {f.name}" for f in reversed(frames))
        parts.append(f"{type(exc).__module__}.{type(exc).__qualname__} at {where}")
        if isinstance(exc, BaseExceptionGroup):
            inner = "; ".join(describe_exception(e) for e in exc.exceptions)
            parts[-1] += f" [{inner}]"
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return " | caused by ".join(parts)


class JsonFormatter(logging.Formatter):
    @override
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD and value is not None:
                out[key] = value
        if record.exc_info:
            out["exc"] = describe_exception(record.exc_info[1])
        return json.dumps(out, ensure_ascii=False, default=str)


def configure(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Журнал запросов пишет AccessLogMiddleware; журнал uvicorn и httpx с адресами не нужен.
    for name in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
