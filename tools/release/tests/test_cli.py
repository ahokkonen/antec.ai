from __future__ import annotations

import json
from pathlib import Path

import pytest

from antec_release.cli import EXIT_INVALID, EXIT_OK, EXIT_USAGE, main
from antec_release.manifest import IMAGES, PLATFORMS, ManifestError, migration_head

COMMIT = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "a" * 64


def _run(root: Path, *args: str) -> int:
    return main(["--root", str(root), *args])


def test_check_ok(core_copy: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(core_copy, "check") == EXIT_OK
    assert "согласована" in capsys.readouterr().out


def test_check_tag_mismatch(core_copy: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(core_copy, "check", "--tag", "v9.9.9") == EXIT_INVALID
    assert "ожидается v0.1.0" in capsys.readouterr().err


def test_first_release_bump_then_tag_check(
    core_copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(core_copy, "check", "--tag", "v0.1.0") == EXIT_INVALID
    assert _run(core_copy, "bump", "0.1.0", "--date", "2026-11-26") == EXIT_OK
    assert _run(core_copy, "check", "--tag", "v0.1.0") == EXIT_OK
    capsys.readouterr()
    assert _run(core_copy, "notes", "0.1.0") == EXIT_OK
    assert "релизный процесс" in capsys.readouterr().out.lower()


def test_bump_requires_greater_version(core_copy: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(core_copy, "bump", "0.0.9") == EXIT_INVALID
    assert "больше текущей" in capsys.readouterr().err


def test_bump_next_minor(core_copy: Path) -> None:
    assert _run(core_copy, "bump", "0.1.0", "--date", "2026-11-26") == EXIT_OK
    # Повтор той же версии после релиза запрещён.
    assert _run(core_copy, "bump", "0.1.0") == EXIT_INVALID
    log = core_copy / "CHANGELOG.md"
    log.write_text(log.read_text().replace("## [Не выпущено]\n", "## [Не выпущено]\n\n- b\n", 1))
    assert _run(core_copy, "bump", "0.2.0", "--date", "2026-12-01") == EXIT_OK
    assert (core_copy / "VERSION").read_text() == "0.2.0\n"
    assert _run(core_copy, "check", "--tag", "v0.2.0") == EXIT_OK


def test_bump_refuses_inconsistent_tree(core_copy: Path) -> None:
    init = core_copy / "tools/instance/src/antec_instance/__init__.py"
    init.write_text(init.read_text().replace("0.1.0", "0.0.1"))
    assert _run(core_copy, "bump", "0.1.0") == EXIT_INVALID
    assert (core_copy / "VERSION").read_text() == "0.1.0\n"


def test_images_matrix(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["images"]) == EXIT_OK
    matrix = json.loads(capsys.readouterr().out)
    assert len(matrix) == len(IMAGES) * len(PLATFORMS)
    arm = [m for m in matrix if m["platform"] == "linux/arm64"]
    assert {m["runner"] for m in arm} == {"ubuntu-24.04-arm"}
    assert {m["repository"] for m in matrix} == {
        "ghcr.io/ahokkonen/antec-platform-api",
        "ghcr.io/ahokkonen/antec-postgres",
        "ghcr.io/ahokkonen/antec-keycloak",
    }
    for m in matrix:
        assert (Path(__file__).resolve().parents[3] / m["context"] / "Dockerfile").is_file()


def _compat(version: str = "0.1.0") -> dict[str, object]:
    return {
        "core_version": version,
        "manifest_schema_versions": {"supported": [1, 2, 3, 4, 5], "deprecated": [1, 2, 3, 4]},
        "mcp_protocol_versions": ["2026-07-28"],
    }


def _manifest(core_copy: Path, tmp_path: Path, digests: object, compat: object) -> int:
    (tmp_path / "d.json").write_text(json.dumps(digests))
    (tmp_path / "c.json").write_text(json.dumps(compat))
    return _run(
        core_copy,
        "manifest",
        "--commit",
        COMMIT,
        "--digests",
        str(tmp_path / "d.json"),
        "--compat",
        str(tmp_path / "c.json"),
        "--out",
        str(tmp_path / "release.json"),
    )


def test_manifest(core_copy: Path, tmp_path: Path) -> None:
    digests = {i.name: DIGEST for i in IMAGES}
    assert _manifest(core_copy, tmp_path, digests, _compat()) == EXIT_OK
    data = json.loads((tmp_path / "release.json").read_text())
    assert data["core_version"] == "0.1.0"
    assert data["commit"] == COMMIT
    assert data["images"]["platform-api"] == {
        "repository": "ghcr.io/ahokkonen/antec-platform-api",
        "tag": "0.1.0",
        "digest": DIGEST,
    }
    assert data["migration_head"] == migration_head(core_copy)
    assert data["mcp_protocol_versions"] == ["2026-07-28"]


@pytest.mark.parametrize(
    ("digests", "compat"),
    [
        ({"platform-api": DIGEST}, _compat()),
        ({i.name: "sha256:abc" for i in IMAGES}, _compat()),
        ({i.name: DIGEST for i in IMAGES}, _compat("0.2.0")),
        ({i.name: DIGEST for i in IMAGES}, {**_compat(), "mcp_protocol_versions": []}),
    ],
)
def test_manifest_rejects(core_copy: Path, tmp_path: Path, digests: object, compat: object) -> None:
    assert _manifest(core_copy, tmp_path, digests, compat) == EXIT_USAGE
    assert not (tmp_path / "release.json").exists()


def test_migration_head_detects_branches(core_copy: Path) -> None:
    versions = core_copy / "services/platform-api/src/antec_platform/migrations/versions"
    (versions / "9999_branch.py").write_text(
        'revision: str = "9999"\ndown_revision: str | None = "0001"\n'
    )
    with pytest.raises(ManifestError, match="одна голова"):
        migration_head(core_copy)
