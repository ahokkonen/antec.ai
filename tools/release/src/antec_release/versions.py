"""Места, где записана версия ядра (ADR-0014, п. 1), их проверка и замена.

Источник истины: `VERSION` в корне. Остальные места повторяют его, потому что их читают инструменты,
которые не знают о `VERSION` (uv, Docker, compose). Плагины инсталляций сюда не входят: у них
своя версия.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

VERSION_FILE = "VERSION"
SEMVER_RE = re.compile(r"(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)")


class VersionError(Exception):
    pass


@dataclass(frozen=True)
class Location:
    """Файл и шаблон с группой `v`; `count` = точное число вхождений, None = хотя бы одно."""

    path: str
    pattern: re.Pattern[str]
    count: int | None = 1
    description: str = ""


def _pyproject() -> re.Pattern[str]:
    # Первая строка version в [project]; других ключей version в наших pyproject нет.
    return re.compile(r'^\[project\]\n(?:(?!\[).*\n)*?version = "(?P<v>[^"]*)"$', re.M)


def _uv_lock(package: str) -> re.Pattern[str]:
    return re.compile(
        rf'^\[\[package\]\]\nname = "{re.escape(package)}"\nversion = "(?P<v>[^"]*)"$', re.M
    )


def _dunder() -> re.Pattern[str]:
    return re.compile(r'^__version__ = "(?P<v>[^"]*)"$', re.M)


_PLATFORM_IMAGE = re.compile(r"localhost/antec/platform-api:(?P<v>[^\s`\"']+)")

LOCATIONS: tuple[Location, ...] = (
    Location("services/platform-api/pyproject.toml", _pyproject()),
    Location("services/platform-api/uv.lock", _uv_lock("antec-platform")),
    Location(
        "services/platform-api/Dockerfile",
        re.compile(r'org\.opencontainers\.image\.version="(?P<v>[^"]*)"'),
    ),
    Location("deploy/compose/compose.yaml", _PLATFORM_IMAGE, None, "образ platform-api"),
    Location("deploy/compose/README.md", _PLATFORM_IMAGE, None, "образ platform-api"),
    Location(
        "instances/dev/instance.yaml",
        re.compile(r'^core:\n  version: "(?P<v>[^"]*)"$', re.M),
        1,
        "core.version",
    ),
    Location("tools/instance/pyproject.toml", _pyproject()),
    Location("tools/instance/uv.lock", _uv_lock("antec-instance")),
    Location("tools/instance/src/antec_instance/__init__.py", _dunder()),
    Location("tools/pincheck/pyproject.toml", _pyproject()),
    Location("tools/pincheck/uv.lock", _uv_lock("pincheck")),
    Location("tools/pincheck/src/pincheck/__init__.py", _dunder()),
    Location("tools/release/pyproject.toml", _pyproject()),
    Location("tools/release/uv.lock", _uv_lock("antec-release")),
    Location("tools/release/src/antec_release/__init__.py", _dunder()),
)


def parse_semver(value: str) -> tuple[int, int, int]:
    m = SEMVER_RE.fullmatch(value)
    if m is None:
        raise VersionError(f"ожидается версия X.Y.Z без префикса, получено {value!r}")
    return int(m["major"]), int(m["minor"]), int(m["patch"])


def read_version(root: Path) -> str:
    path = root / VERSION_FILE
    try:
        value = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VersionError(f"{path}: {exc.strerror}") from exc
    if value != value.strip() + "\n":
        raise VersionError(f"{path}: одна строка X.Y.Z с переводом строки в конце")
    parse_semver(value.strip())
    return value.strip()


def _read(root: Path, loc: Location) -> str:
    path = root / loc.path
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VersionError(f"{loc.path}: {exc.strerror}") from exc


def check_locations(root: Path, version: str) -> list[str]:
    """Расхождения с `VERSION`: пустой список = всё совпадает."""
    problems: list[str] = []
    for loc in LOCATIONS:
        try:
            text = _read(root, loc)
        except VersionError as exc:
            problems.append(str(exc))
            continue
        what = f" ({loc.description})" if loc.description else ""
        found = list(loc.pattern.finditer(text))
        if not found:
            problems.append(f"{loc.path}{what}: версия не найдена")
            continue
        if loc.count is not None and len(found) != loc.count:
            problems.append(
                f"{loc.path}{what}: ожидается {loc.count} вхождение версии, найдено {len(found)}"
            )
        for m in found:
            if m["v"] != version:
                line = text.count("\n", 0, m.start("v")) + 1
                problems.append(
                    f"{loc.path}:{line}{what}: версия {m['v']}, в {VERSION_FILE} {version}"
                )
    return problems


def rewrite_locations(root: Path, new: str) -> list[str]:
    """Записать `new` во все места и в `VERSION`; вернуть изменённые файлы."""
    parse_semver(new)
    pending: dict[str, str] = {}
    for loc in LOCATIONS:
        text = pending.get(loc.path) or _read(root, loc)
        found = list(loc.pattern.finditer(text))
        if not found or (loc.count is not None and len(found) != loc.count):
            raise VersionError(f"{loc.path}: место версии не найдено однозначно, проверьте check")
        out: list[str] = []
        pos = 0
        for m in found:
            out += [text[pos : m.start("v")], new]
            pos = m.end("v")
        pending[loc.path] = "".join([*out, text[pos:]])
    pending[VERSION_FILE] = new + "\n"
    changed = []
    for rel, text in pending.items():
        path = root / rel
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            changed.append(rel)
    return changed
