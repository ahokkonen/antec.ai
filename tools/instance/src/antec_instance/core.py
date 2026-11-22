"""Каталог ядра: версия (`VERSION`) и шаблон инсталляции (`templates/instance`)."""

from __future__ import annotations

import re
from pathlib import Path

VERSION_FILE = "VERSION"
TEMPLATE_DIR = Path("templates") / "instance"


class CoreError(Exception):
    pass


def default_core_dir() -> Path:
    """Ядро, из которого запущен инструмент (tools/instance/src/antec_instance -> корень)."""
    return Path(__file__).resolve().parents[4]


def core_version(core_dir: Path) -> str:
    try:
        version = (core_dir / VERSION_FILE).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CoreError(f"не найден {VERSION_FILE} ядра в {core_dir}: {exc.strerror}") from exc
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise CoreError(f"{core_dir / VERSION_FILE}: ожидается версия X.Y.Z, получено {version!r}")
    return version
