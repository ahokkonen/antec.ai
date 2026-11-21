"""Модульные тесты разбора ссылок, версий и поиска файлов."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FIXTURES, Runner

from pincheck.discover import FileKind, classify, discover
from pincheck.imageref import ImageRefError, parse_image_ref
from pincheck.versions import PyVersion, TagKind, Version, VersionError, classify_tag_version
from pincheck.yamlload import load_yaml

D = "sha256:" + "c" * 64


@pytest.mark.parametrize(
    ("raw", "repo", "tag", "digest"),
    [
        ("postgres", "docker.io/library/postgres", None, None),
        ("postgres:18.7", "docker.io/library/postgres", "18.7", None),
        (f"library/postgres:18.7@{D}", "docker.io/library/postgres", "18.7", D),
        (f"index.docker.io/postgres:18.7@{D}", "docker.io/library/postgres", "18.7", D),
        ("litellm/litellm:v1.108.7", "docker.io/litellm/litellm", "v1.108.7", None),
        ("GHCR.io/berriai/litellm:v1", "ghcr.io/berriai/litellm", "v1", None),
        ("localhost:5000/team/app:1.0", "localhost:5000/team/app", "1.0", None),
        (f"mirror.local/ghcr.io/x/y@{D}", "mirror.local/ghcr.io/x/y", None, D),
    ],
)
def test_parse_image_ref(raw: str, repo: str, tag: str | None, digest: str | None) -> None:
    ref = parse_image_ref(raw)
    assert (ref.repository, ref.tag, ref.digest) == (repo, tag, digest)


@pytest.mark.parametrize(
    "raw", ["", "Postgres:18", "postgres:18@sha256:abc", "postgres@md5:" + "a" * 32, "a/:1", ":1"]
)
def test_parse_image_ref_rejects(raw: str) -> None:
    with pytest.raises(ImageRefError):
        parse_image_ref(raw)


def test_version_ordering() -> None:
    assert Version.parse("1.108.7") > Version.parse("1.106.3")
    assert Version.parse("18.7") == Version.parse("18.7.0")
    assert Version.parse("0.13.10") > Version.parse("0.13.9")


@pytest.mark.parametrize("text", ["v1.0", "1.0-rc1", "01.2", "1..2", "", "1.2.3.4.5"])
def test_version_rejects(text: str) -> None:
    with pytest.raises(VersionError):
        Version.parse(text)


@pytest.mark.parametrize(
    ("rest", "kind"),
    [
        ("1.108.7", TagKind.RELEASE),
        ("1.108.7-rc1", TagKind.PRERELEASE),
        ("1.108.7rc1", TagKind.PRERELEASE),
        ("2.0.0-beta.2", TagKind.PRERELEASE),
        ("1.0.0.dev3", TagKind.PRERELEASE),
        ("18.7-alpine", TagKind.VARIANT),
        ("1.108.7-stable", TagKind.VARIANT),
        ("bookworm", TagKind.NOT_VERSION),
    ],
)
def test_classify_tag(rest: str, kind: TagKind) -> None:
    assert classify_tag_version(rest).kind is kind


def test_py_version_minimum() -> None:
    minimum = PyVersion.parse("6.0.1")
    assert PyVersion.parse("6.0.0").is_below(minimum)
    assert PyVersion.parse("6.0.1rc1").is_below(minimum)
    assert not PyVersion.parse("6.0.1").is_below(minimum)
    assert not PyVersion.parse("6.0.12.20261113").is_below(minimum)
    assert not PyVersion.parse("1!0.1").is_below(minimum)


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("compose.yaml", FileKind.COMPOSE),
        ("compose.override.yml", FileKind.COMPOSE),
        ("docker-compose.prod.yaml", FileKind.COMPOSE),
        ("Dockerfile", FileKind.DOCKERFILE),
        ("Dockerfile.worker", FileKind.DOCKERFILE),
        ("api.Dockerfile", FileKind.DOCKERFILE),
        ("Containerfile", FileKind.DOCKERFILE),
        ("uv.lock", FileKind.UV_LOCK),
        ("dockerfile.py", None),
        ("Dockerfile.md", None),
        ("composer.json", None),
        ("allowed-versions.yaml", None),
    ],
)
def test_classify_file(name: str, kind: FileKind | None) -> None:
    assert classify(name) is kind


def test_discover_skips_excluded_and_vcs(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "compose.yaml").write_text("services: {}\n")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "uv.lock").write_text("")
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "Dockerfile").write_text("FROM x\n")
    found = discover(["."], ["fixtures"], cwd=tmp_path)
    assert [t.display for t in found.targets] == ["a/compose.yaml"]


def test_explicit_unsupported_and_missing_paths(pincheck: Runner, tmp_path: Path) -> None:
    other = tmp_path / "notes.txt"
    other.write_text("x")
    result = pincheck(other, tmp_path / "missing")
    assert result.exit_code == 1
    assert result.errors == ["PIN041", "PIN041"]


def test_exclude_option(pincheck: Runner) -> None:
    result = pincheck(FIXTURES / "compose", extra=("--exclude", "*"))
    assert result.exit_code == 0, result.output
    assert "Проверять нечего" in result.output


def test_yaml_merge_keys_are_not_duplicates() -> None:
    doc = load_yaml("base: &b\n  a: 1\nsvc:\n  <<: *b\n  a: 2\n")
    assert doc.data["svc"] == {"a": 2}
