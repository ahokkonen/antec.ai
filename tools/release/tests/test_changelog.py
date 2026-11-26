from __future__ import annotations

import datetime as dt

import pytest

from antec_release import changelog
from tests.conftest import CORE

HEAD = "# Изменения\n\nТекст.\n\n"


def _log(body: str) -> changelog.Changelog:
    return changelog.parse(HEAD + body)


def test_repository_changelog_is_valid() -> None:
    version = (CORE / "VERSION").read_text().strip()
    assert changelog.check(changelog.load(CORE), version, release=False) == []


def test_unreleased_must_be_first() -> None:
    log = _log("## [0.1.0] - 2026-11-26\n\n### Добавлено\n\n- a\n")
    assert "первым разделом должен быть [Не выпущено]" in changelog.check(
        log, "0.1.0", release=False
    )


def test_unknown_subsection_and_missing_date() -> None:
    log = _log("## [Не выпущено]\n\n### Разное\n\n- a\n\n## [0.1.0]\n\n- b\n")
    problems = changelog.check(log, "0.1.0", release=False)
    assert any("«Разное»" in p for p in problems)
    assert any("нет даты" in p for p in problems)


def test_versions_descending_and_unique() -> None:
    log = _log(
        "## [Не выпущено]\n\n## [0.1.0] - 2026-11-01\n\n- a\n\n"
        "## [0.2.0] - 2026-11-02\n\n- b\n\n## [0.1.0] - 2026-11-03\n\n- c\n"
    )
    problems = changelog.check(log, "0.2.0", release=False)
    assert any("по убыванию" in p for p in problems)
    assert any("повторяется" in p for p in problems)


def test_bad_date() -> None:
    with pytest.raises(changelog.ChangelogError):
        _log("## [Не выпущено]\n\n## [0.1.0] - 26.11.2026\n")


def test_release_requires_section_with_entries() -> None:
    log = _log("## [Не выпущено]\n\n- a\n\n## [0.1.0] - 2026-11-26\n\n### Добавлено\n")
    assert "раздел [0.1.0] пуст" in changelog.check(log, "0.1.0", release=True)
    assert any(
        "сначала antec-release bump 0.2.0" in p for p in changelog.check(log, "0.2.0", release=True)
    )


def test_release_moves_unreleased() -> None:
    log = _log("## [Не выпущено]\n\n### Добавлено\n\n- a\n\n## [0.1.0] - 2026-11-01\n\n- old\n")
    text = changelog.release(log, "0.2.0", dt.date(2026, 12, 1))
    new = changelog.parse(text)
    assert [s.name for s in new.sections] == ["Не выпущено", "0.2.0", "0.1.0"]
    assert not new.sections[0].has_entries
    assert changelog.notes(new, "0.2.0") == "### Добавлено\n\n- a\n"
    assert changelog.notes(new, "0.1.0") == "- old\n"
    assert changelog.check(new, "0.2.0", release=True) == []
    assert text.startswith(HEAD)


def test_release_first_version_at_end_of_file() -> None:
    text = changelog.release(_log("## [Не выпущено]\n\n- a\n"), "0.1.0", dt.date(2026, 11, 26))
    assert text.endswith("## [0.1.0] - 2026-11-26\n\n- a\n")


def test_release_refuses_empty_or_existing() -> None:
    with pytest.raises(changelog.ChangelogError, match="пуст"):
        changelog.release(_log("## [Не выпущено]\n\n### Добавлено\n"), "0.1.0", dt.date.today())
    log = _log("## [Не выпущено]\n\n- a\n\n## [0.1.0] - 2026-11-01\n\n- b\n")
    with pytest.raises(changelog.ChangelogError, match="уже есть"):
        changelog.release(log, "0.1.0", dt.date.today())
