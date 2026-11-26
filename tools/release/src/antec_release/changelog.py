"""CHANGELOG.md в формате Keep a Changelog (ADR-0014, п. 4)."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from antec_release.versions import VersionError, parse_semver

CHANGELOG_FILE = "CHANGELOG.md"
UNRELEASED = "Не выпущено"
SUBSECTIONS = (
    "Добавлено",
    "Изменено",
    "Несовместимые изменения",
    "Исправлено",
    "Безопасность",
    "Обновление",
)
_HEADING_RE = re.compile(r"^## \[(?P<name>[^\]]+)\](?: - (?P<date>\S+))?[ \t]*$", re.M)
_SUB_RE = re.compile(r"^### (?P<name>.+?)[ \t]*$", re.M)


class ChangelogError(Exception):
    pass


@dataclass(frozen=True)
class Section:
    name: str
    date: dt.date | None
    body: str
    start: int
    end: int

    @property
    def has_entries(self) -> bool:
        return any(line.lstrip().startswith("- ") for line in self.body.splitlines())


@dataclass(frozen=True)
class Changelog:
    text: str
    sections: tuple[Section, ...]

    def get(self, name: str) -> Section | None:
        return next((s for s in self.sections if s.name == name), None)


def parse(text: str) -> Changelog:
    heads = list(_HEADING_RE.finditer(text))
    sections = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        date = None
        if m["date"] is not None:
            try:
                date = dt.date.fromisoformat(m["date"])
            except ValueError as exc:
                raise ChangelogError(f"раздел [{m['name']}]: дата {m['date']!r} не ISO") from exc
        sections.append(Section(m["name"], date, text[m.end() : end], m.start(), end))
    return Changelog(text, tuple(sections))


def load(root: Path) -> Changelog:
    path = root / CHANGELOG_FILE
    try:
        return parse(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ChangelogError(f"{path}: {exc.strerror}") from exc


def check(log: Changelog, version: str, *, release: bool) -> list[str]:
    """Структура CHANGELOG; `release` = проверка перед тегом `v{version}`."""
    problems: list[str] = []
    names = [s.name for s in log.sections]
    if not names or names[0] != UNRELEASED:
        problems.append(f"первым разделом должен быть [{UNRELEASED}]")
    seen: set[str] = set()
    previous: tuple[int, int, int] | None = None
    for s in log.sections:
        if s.name in seen:
            problems.append(f"раздел [{s.name}] повторяется")
        seen.add(s.name)
        for sub in _SUB_RE.finditer(s.body):
            if sub["name"] not in SUBSECTIONS:
                problems.append(
                    f"[{s.name}]: подраздел «{sub['name']}» не из списка: {', '.join(SUBSECTIONS)}"
                )
        if s.name == UNRELEASED:
            if s.date is not None:
                problems.append(f"у раздела [{UNRELEASED}] не бывает даты")
            continue
        try:
            current = parse_semver(s.name)
        except VersionError as exc:
            problems.append(f"раздел [{s.name}]: {exc}")
            continue
        if s.date is None:
            problems.append(f"раздел [{s.name}]: нет даты (## [{s.name}] - ГГГГ-ММ-ДД)")
        if previous is not None and current >= previous:
            problems.append(f"раздел [{s.name}]: версии должны идти по убыванию")
        previous = current
    if release:
        section = log.get(version)
        if section is None:
            problems.append(f"нет раздела [{version}]: сначала antec-release bump {version}")
        elif not section.has_entries:
            problems.append(f"раздел [{version}] пуст")
    return problems


def release(log: Changelog, version: str, today: dt.date) -> str:
    """Перенести записи [Не выпущено] в новый раздел [version] - today."""
    unreleased = log.get(UNRELEASED)
    if unreleased is None:
        raise ChangelogError(f"нет раздела [{UNRELEASED}]")
    if not unreleased.has_entries:
        raise ChangelogError(f"раздел [{UNRELEASED}] пуст: нечего выпускать")
    if log.get(version) is not None:
        raise ChangelogError(f"раздел [{version}] уже есть")
    body = unreleased.body.strip("\n")
    new = f"## [{UNRELEASED}]\n\n## [{version}] - {today.isoformat()}\n\n{body}\n\n"
    rest = log.text[unreleased.end :]
    return log.text[: unreleased.start] + (new if rest else new.rstrip("\n") + "\n") + rest


def notes(log: Changelog, version: str) -> str:
    section = log.get(version)
    if section is None:
        raise ChangelogError(f"нет раздела [{version}]")
    return section.body.strip("\n") + "\n"
