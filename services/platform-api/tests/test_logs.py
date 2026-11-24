"""Журнал не содержит текстов исключений (ADR-0012, п. 5)."""

from __future__ import annotations

import json
import logging
import sys

from antec_platform.logs import JsonFormatter


def test_exception_text_is_not_logged() -> None:
    try:
        try:
            raise KeyError("первый секрет")
        except KeyError as exc:
            raise ValueError("текст пользователя") from exc
    except ValueError:
        record = logging.LogRecord("x", logging.ERROR, "f.py", 1, "сбой", None, sys.exc_info())
    line = JsonFormatter().format(record)
    assert "секрет" not in line
    assert "текст пользователя" not in line
    exc_text = json.loads(line)["exc"]
    assert "builtins.ValueError" in exc_text
    assert "caused by builtins.KeyError" in exc_text
    assert "test_logs.py" in exc_text
