from __future__ import annotations

from pathlib import Path

import pytest

from antec_release.versions import (
    LOCATIONS,
    VersionError,
    check_locations,
    parse_semver,
    read_version,
    rewrite_locations,
)
from tests.conftest import CORE


def test_repository_is_consistent() -> None:
    assert check_locations(CORE, read_version(CORE)) == []


def test_every_location_matches_in_repository() -> None:
    for loc in LOCATIONS:
        found = list(loc.pattern.finditer((CORE / loc.path).read_text(encoding="utf-8")))
        assert found, loc.path
        if loc.count is not None:
            assert len(found) == loc.count, loc.path


@pytest.mark.parametrize("value", ["1.2", "v1.2.3", "01.2.3", "1.2.3-rc1", " 1.2.3"])
def test_parse_semver_rejects(value: str) -> None:
    with pytest.raises(VersionError):
        parse_semver(value)


def test_read_version_requires_single_line(core_copy: Path) -> None:
    (core_copy / "VERSION").write_text("0.1.0")
    with pytest.raises(VersionError):
        read_version(core_copy)


def test_mismatch_reported_with_line(core_copy: Path) -> None:
    compose = core_copy / "deploy/compose/compose.yaml"
    text = compose.read_text()
    compose.write_text(
        text.replace("localhost/antec/platform-api:0.1.0", "localhost/antec/platform-api:0.0.9", 1)
    )
    problems = check_locations(core_copy, "0.1.0")
    assert len(problems) == 1
    assert "deploy/compose/compose.yaml:" in problems[0]
    assert "0.0.9" in problems[0]


def test_missing_location_reported(core_copy: Path) -> None:
    init = core_copy / "tools/pincheck/src/pincheck/__init__.py"
    init.write_text('"""pincheck"""\n')
    problems = check_locations(core_copy, "0.1.0")
    assert problems == ["tools/pincheck/src/pincheck/__init__.py: версия не найдена"]


def test_duplicate_single_location_reported(core_copy: Path) -> None:
    lock = core_copy / "tools/release/uv.lock"
    block = '[[package]]\nname = "antec-release"\nversion = "0.1.0"\n'
    lock.write_text(lock.read_text() + "\n" + block)
    assert any("найдено 2" in p for p in check_locations(core_copy, "0.1.0"))


def test_rewrite_changes_only_versions(core_copy: Path) -> None:
    before = {loc.path: (core_copy / loc.path).read_text() for loc in LOCATIONS}
    changed = rewrite_locations(core_copy, "0.2.0")
    assert read_version(core_copy) == "0.2.0"
    assert check_locations(core_copy, "0.2.0") == []
    assert set(changed) == {"VERSION", *before}
    for rel, text in before.items():
        after = (core_copy / rel).read_text()
        # Длина та же: 0.1.0 -> 0.2.0, значит заменены только версии.
        assert len(after) == len(text)
        assert after.replace("0.2.0", "0.1.0") == text or "0.2.0" not in text


def test_rewrite_keeps_other_packages_in_lock(core_copy: Path) -> None:
    lock = core_copy / "services/platform-api/uv.lock"
    others = [line for line in lock.read_text().splitlines() if line.startswith("version = ")]
    rewrite_locations(core_copy, "0.2.0")
    after = [line for line in lock.read_text().splitlines() if line.startswith("version = ")]
    assert sum(a != b for a, b in zip(others, after, strict=True)) == 1
