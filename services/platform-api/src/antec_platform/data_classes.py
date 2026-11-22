"""Классы данных и допуски моделей (ADR-0011): инвариант Platform API.

Класс у источника (ввод пользователя = класс по умолчанию инсталляции, инструмент = его
data_class), допуск у логической модели. Модель не получает данных выше своего допуска; это
проверяет код, а не политика: политики инсталляции могут только ужесточить (атрибут
`data_class` ресурса в Cerbos).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

CLASSES = ("public", "internal", "confidential", "restricted")
# Допуск модели без записи в models.json: в v1 облачным моделям разрешено всё.
DEFAULT_CLEARANCE = "restricted"
DATA_CLASS_REASON = "data_class"


class DataClassConfigError(Exception):
    pass


def rank(value: str) -> int:
    try:
        return CLASSES.index(value)
    except ValueError as exc:
        raise DataClassConfigError(f"неизвестный класс данных: {value!r}") from exc


def allows(clearance: str, data_class: str) -> bool:
    """Модель с допуском clearance может получать данные класса data_class."""
    return rank(data_class) <= rank(clearance)


def load_clearances(path: Path | None, models: Iterable[str]) -> dict[str, str]:
    """Допуски из models.json (рендер манифеста); модели без записи = restricted."""
    known = list(models)
    out = dict.fromkeys(known, DEFAULT_CLEARANCE)
    if path is None:
        return out
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        for item in raw:
            name, clearance = str(item["name"]), str(item["max_data_class"])
            rank(clearance)
            if name in out:
                out[name] = clearance
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DataClassConfigError(f"{path}: {exc!r}") from exc
    return out
