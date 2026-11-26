"""Образы ядра из core-images.yaml и теги слияния Compose (ADR-0014, п. 6)."""

from __future__ import annotations

from pathlib import Path

from conftest import Runner

DIGEST = "sha256:" + "b" * 64
DENIED = "sha256:1bbcb1443bc962e45f5aad53fe3d2c30600e0379715fb6c664cdd3ac126c339e"
REPO = "ghcr.io/ahokkonen/antec-postgres"


def _core_images(tmp_path: Path, digest: str = DIGEST) -> Path:
    path = tmp_path / "core-images.yaml"
    path.write_text(
        "schema_version: 1\n"
        "core_version: 0.1.0\n"
        "images:\n"
        f"  postgres: {{repository: {REPO}, tag: 0.1.0, digest: '{digest}'}}\n"
    )
    return path


def _override(tmp_path: Path, image: str) -> Path:
    d = tmp_path / "rendered"
    d.mkdir()
    (d / "compose.core-images.yaml").write_text(
        "services:\n"
        "  postgres:\n"
        f"    image: {image}\n"
        "    build: !reset null\n"
        "    pull_policy: missing\n"
        "  other:\n"
        "    environment: !override {A: '1'}\n"
    )
    return d


def test_pinned_core_image_passes(pincheck: Runner, tmp_path: Path) -> None:
    rendered = _override(tmp_path, f"{REPO}:0.1.0@{DIGEST}")
    result = pincheck(rendered, extra=("--core-images", str(_core_images(tmp_path))))
    assert result.exit_code == 0, result.output
    assert result.errors == []


def test_core_image_without_core_images_is_unknown(pincheck: Runner, tmp_path: Path) -> None:
    result = pincheck(_override(tmp_path, f"{REPO}:0.1.0@{DIGEST}"))
    assert result.errors == ["PIN013"], result.output


def test_core_image_other_digest_or_tag(pincheck: Runner, tmp_path: Path) -> None:
    core = _core_images(tmp_path)
    for i, image in enumerate([f"{REPO}:0.1.0@sha256:{'c' * 64}", f"{REPO}:0.2.0@{DIGEST}"]):
        sub = tmp_path / str(i)
        sub.mkdir()
        result = pincheck(_override(sub, image), extra=("--core-images", str(core)))
        assert result.errors == ["PIN023"], result.output
        assert "antec-instance core-pin" in result.output


def test_core_image_without_digest(pincheck: Runner, tmp_path: Path) -> None:
    rendered = _override(tmp_path, f"{REPO}:0.1.0")
    result = pincheck(rendered, extra=("--core-images", str(_core_images(tmp_path))))
    assert "PIN010" in result.errors, result.output


def test_denied_digest_in_core_images(pincheck: Runner, tmp_path: Path) -> None:
    rendered = _override(tmp_path, f"{REPO}:0.1.0@{DENIED}")
    result = pincheck(rendered, extra=("--core-images", str(_core_images(tmp_path, DENIED))))
    # И сам core-images.yaml, и ссылка в compose.
    assert result.errors.count("PIN018") == 2, result.output


def test_bad_core_images_file(pincheck: Runner, tmp_path: Path) -> None:
    bad = tmp_path / "core-images.yaml"
    bad.write_text("images:\n  postgres: {repository: x, tag: latest, digest: 'sha256:1'}\n")
    result = pincheck(tmp_path, extra=("--core-images", str(bad)))
    assert result.exit_code == 1
    assert result.errors == ["PIN041"], result.output


def test_reset_build_without_image_is_ignored(pincheck: Runner, tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services:\n  x:\n    build: !reset null\n")
    result = pincheck(tmp_path)
    assert result.exit_code == 0, result.output
