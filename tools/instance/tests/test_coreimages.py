"""core-pin, core-images.yaml и override compose (ADR-0014, п. 6)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
import yaml

from antec_instance import coreimages
from antec_instance.cli import EXIT_INVALID, EXIT_OK, main

COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _digest(ch: str) -> str:
    return "sha256:" + ch * 64


def _release(version: str, **extra: object) -> dict[str, object]:
    data: dict[str, object] = {
        "schema_version": 1,
        "core_version": version,
        "repository": coreimages.CORE_REPOSITORY,
        "commit": COMMIT,
        "images": {
            name: {
                "repository": f"{coreimages.REGISTRY}/antec-{name}",
                "tag": version,
                "digest": _digest(ch),
            }
            for name, ch in zip(coreimages.IMAGE_SERVICES, "abc", strict=True)
        },
        "manifest_schema_versions": {"supported": [1, 2, 3, 4, 5], "deprecated": [1, 2, 3, 4]},
        "mcp_protocol_versions": ["2026-07-28"],
        "migration_head": "0006",
        "future_field": True,
    }
    data.update(extra)
    return data


def _cosign(tmp_path: Path, rc: int) -> Path:
    """Поддельный cosign: пишет аргументы и завершается с кодом rc."""
    script = tmp_path / "cosign"
    log = tmp_path / "cosign.args"
    script.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {log}\n"
        f"[ {rc} -eq 0 ] || echo 'Error: none of the expected identities matched' >&2\nexit {rc}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _release_dir(tmp_path: Path, data: dict[str, object]) -> Path:
    d = tmp_path / "release"
    d.mkdir()
    (d / "release.json").write_text(json.dumps(data))
    (d / "release.json.sigstore.json").write_text("{}")
    return d


def _version(core_dir: Path) -> str:
    return (core_dir / "VERSION").read_text().strip()


def _pin(core_dir: Path, inst: Path, tmp_path: Path, data: dict[str, object], rc: int = 0) -> int:
    return main(
        [
            "--core",
            str(core_dir),
            "core-pin",
            str(inst),
            "--release-dir",
            str(_release_dir(tmp_path, data)),
            "--cosign",
            str(_cosign(tmp_path, rc)),
        ]
    )


def test_image_services_match_core_compose(core_dir: Path) -> None:
    compose = yaml.safe_load((core_dir / "deploy/compose/compose.yaml").read_text())
    local: dict[str, set[str]] = {}
    for service, spec in compose["services"].items():
        image = spec.get("image", "")
        if image.startswith("localhost/antec/"):
            name = image.removeprefix("localhost/antec/").split(":")[0]
            local.setdefault(name, set()).add(service)
    assert local == {n: set(s) for n, s in coreimages.IMAGE_SERVICES.items()}


def test_pin_render_and_validate(
    core_dir: Path, dev_instance: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    version = _version(core_dir)
    assert _pin(core_dir, dev_instance, tmp_path, _release(version)) == EXIT_OK
    args = (tmp_path / "cosign.args").read_text().splitlines()
    assert args[0] == "verify-blob"
    assert coreimages.identity(version) in args
    assert coreimages.OIDC_ISSUER in args
    pinned = coreimages.load(dev_instance)
    assert pinned is not None
    assert pinned.images["postgres"].ref == (
        f"ghcr.io/ahokkonen/antec-postgres:{version}@{_digest('b')}"
    )
    capsys.readouterr()

    out = tmp_path / "out"
    assert main(["--core", str(core_dir), "render", str(dev_instance), "--out", str(out)]) == 0
    # SafeLoader с одним конструктором для !reset из Compose spec.
    text = (out / coreimages.OVERRIDE_FILE).read_text()
    override = yaml.load(text, Loader=_ResetLoader)  # noqa: S506
    services = override["services"]
    assert set(services) == {s for v in coreimages.IMAGE_SERVICES.values() for s in v}
    assert services["platform-worker"] == {
        "image": f"ghcr.io/ahokkonen/antec-platform-api:{version}@{_digest('a')}",
        "build": "RESET",
        "pull_policy": "missing",
    }
    summary = json.loads((out / "instance.json").read_text())
    assert summary["core_images"]["keycloak"].endswith(_digest("c"))

    # Без core-images.yaml override удаляется: снова локальная сборка.
    (dev_instance / coreimages.CORE_IMAGES_FILE).unlink()
    assert main(["--core", str(core_dir), "render", str(dev_instance), "--out", str(out)]) == 0
    assert not (out / coreimages.OVERRIDE_FILE).exists()
    assert json.loads((out / "instance.json").read_text())["core_images"] is None


class _ResetLoader(yaml.SafeLoader):
    pass


_ResetLoader.add_constructor("!reset", lambda _loader, _node: "RESET")


def test_bad_signature_writes_nothing(
    core_dir: Path, dev_instance: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _pin(core_dir, dev_instance, tmp_path, _release(_version(core_dir)), rc=1)
    assert rc == EXIT_INVALID
    assert "не прошла проверку" in capsys.readouterr().err
    assert not (dev_instance / coreimages.CORE_IMAGES_FILE).exists()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"core_version": "9.9.9"}, "release.json для ядра 9.9.9"),
        ({"repository": "https://github.com/evil/antec.ai"}, "ожидается"),
        ({"manifest_schema_versions": {"supported": [6]}}, "schema_version 5"),
        ({"mcp_protocol_versions": ["2027-01-01"]}, "ревизия 2026-07-28"),
        ({"images": {}}, "образы release.json"),
    ],
)
def test_release_mismatch_rejected(
    core_dir: Path,
    dev_instance: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    change: dict[str, object],
    message: str,
) -> None:
    data = _release(_version(core_dir), **change)
    assert _pin(core_dir, dev_instance, tmp_path, data) == EXIT_INVALID
    assert message in capsys.readouterr().err
    assert not (dev_instance / coreimages.CORE_IMAGES_FILE).exists()


def test_foreign_image_repository_rejected(
    core_dir: Path, dev_instance: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = _release(_version(core_dir))
    images = data["images"]
    assert isinstance(images, dict)
    images["postgres"]["repository"] = "ghcr.io/evil/antec-postgres"
    assert _pin(core_dir, dev_instance, tmp_path, data) == EXIT_INVALID
    assert "образ postgres" in capsys.readouterr().err


def test_validate_rejects_stale_pin(
    core_dir: Path, dev_instance: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _pin(core_dir, dev_instance, tmp_path, _release(_version(core_dir))) == EXIT_OK
    path = dev_instance / coreimages.CORE_IMAGES_FILE
    path.write_text(
        path.read_text().replace(f"core_version: {_version(core_dir)}", "core_version: 0.0.1")
    )
    capsys.readouterr()
    assert main(["--core", str(core_dir), "validate", str(dev_instance)]) == EXIT_INVALID
    assert "выполните antec-instance core-pin" in capsys.readouterr().err


def test_missing_release_files(core_dir: Path, dev_instance: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = main(["--core", str(core_dir), "core-pin", str(dev_instance), "--release-dir", str(empty)])
    assert rc == EXIT_INVALID
