from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from antec_instance.cli import EXIT_INVALID, EXIT_OK, EXIT_USAGE, main


def test_validate_dev(core_dir: Path, dev_instance: Path) -> None:
    assert main(["--core", str(core_dir), "validate", str(dev_instance)]) == EXIT_OK


def test_render_dev(core_dir: Path, dev_instance: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert (
        main(["--core", str(core_dir), "render", str(dev_instance), "--out", str(out)]) == EXIT_OK
    )
    models = yaml.safe_load((out / "litellm-models.yaml").read_text())
    assert list(models) == ["model_list"]
    assert [m["model_name"] for m in models["model_list"]] == [
        "chat-default",
        "chat-fast",
        "chat-lab",
    ]
    assert models["model_list"][0]["litellm_params"] == {
        "model": "anthropic/claude-opus-5-5",
        "api_key": "os.environ/ANTHROPIC_API_KEY",
    }
    env = dict(
        line.split("=", 1)
        for line in (out / "platform-api.env").read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert env == {
        "PLATFORM_MODELS": "chat-default,chat-fast,chat-lab",
        "PLATFORM_POLICY_SCOPE": "dev",
        "PLATFORM_ALLOWED_CLIENTS": "open-webui,antec-smoke",
    }
    summary = json.loads((out / "instance.json").read_text())
    assert summary["id"] == "dev"
    assert summary["models"] == ["chat-default", "chat-fast", "chat-lab"]


def test_core_version_mismatch(
    core_dir: Path, dev_instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = dev_instance / "instance.yaml"
    manifest.write_text(manifest.read_text().replace('version: "0.1.0"', 'version: "9.9.9"'))
    assert main(["--core", str(core_dir), "validate", str(dev_instance)]) == EXIT_INVALID
    assert "9.9.9" in capsys.readouterr().err


def test_missing_core(tmp_path: Path, dev_instance: Path) -> None:
    assert main(["--core", str(tmp_path), "validate", str(dev_instance)]) == EXIT_USAGE


def test_missing_manifest(core_dir: Path, tmp_path: Path) -> None:
    assert main(["--core", str(core_dir), "validate", str(tmp_path)]) == EXIT_INVALID


def test_init_from_template(core_dir: Path, tmp_path: Path) -> None:
    dest = tmp_path / "instance-acme"
    rc = main(["--core", str(core_dir), "init", str(dest), "--id", "acme", "--name", "Acme Oy"])
    assert rc == EXIT_OK
    assert main(["--core", str(core_dir), "validate", str(dest)]) == EXIT_OK
    text = "".join(p.read_text() for p in dest.rglob("*") if p.is_file())
    assert "__INSTANCE_" not in text
    assert "__CORE_VERSION__" not in text
    assert (dest / ".gitignore").is_file()


def test_init_refuses_non_empty(core_dir: Path, tmp_path: Path) -> None:
    (tmp_path / "x").write_text("x")
    rc = main(["--core", str(core_dir), "init", str(tmp_path), "--id", "acme", "--name", "A"])
    assert rc == EXIT_USAGE


def test_init_bad_id(core_dir: Path, tmp_path: Path) -> None:
    rc = main(["--core", str(core_dir), "init", str(tmp_path / "d"), "--id", "A.B", "--name", "A"])
    assert rc == EXIT_INVALID
    assert not (tmp_path / "d").exists()


def test_init_failure_leaves_no_partial_dir(core_dir: Path, tmp_path: Path) -> None:
    bad_core = tmp_path / "core"
    (bad_core / "templates" / "instance").mkdir(parents=True)
    (bad_core / "VERSION").write_text("0.1.0\n")
    (bad_core / "templates" / "instance" / "instance.yaml").write_text("schema_version: 9\n")
    new = tmp_path / "new"
    empty = tmp_path / "empty"
    empty.mkdir()
    for dest in (new, empty):
        rc = main(["--core", str(bad_core), "init", str(dest), "--id", "acme", "--name", "A"])
        assert rc == EXIT_INVALID
    assert not new.exists()
    assert empty.is_dir()
    assert not any(empty.iterdir())
