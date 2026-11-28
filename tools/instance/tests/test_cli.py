from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from antec_instance.cli import EXIT_INVALID, EXIT_OK, EXIT_USAGE, main


def test_validate_dev(
    core_dir: Path, dev_instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--core", str(core_dir), "validate", str(dev_instance)]) == EXIT_OK
    # agent-lab нарочно получает инструмент выше допуска модели (проверка ADR-0011 на стенде).
    assert "agent-lab: инструмент mcp-demo.create_note (confidential)" in capsys.readouterr().err


def test_render_dev(core_dir: Path, dev_instance: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert (
        main(["--core", str(core_dir), "render", str(dev_instance), "--out", str(out)]) == EXIT_OK
    )
    models = yaml.safe_load((out / "litellm" / "models.yaml").read_text())
    env = dict(
        line.split("=", 1)
        for line in (out / "open-webui.env").read_text().splitlines()
        if not line.startswith("#")
    )
    (banner,) = json.loads(env["WEBUI_BANNERS"])
    assert banner["dismissible"] is False
    assert set(banner["i18n"]) == {"en-US", "fi-FI", "ru-RU"}
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
        "PLATFORM_DEFAULT_DATA_CLASS": "internal",
        "PLATFORM_ALLOWED_CLIENTS": "open-webui,antec-smoke",
        "PLATFORM_OTEL_ENDPOINT": "http://otel-collector:4318",
    }
    agents = json.loads((out / "platform-api" / "agents.json").read_text())
    assert [a["name"] for a in agents] == ["agent-assistant", "agent-notes", "agent-lab"]
    assert agents[1]["tools"] == ["mcp-demo.get_time", "mcp-demo.create_note"]
    servers = json.loads((out / "platform-api" / "mcp_servers.json").read_text())
    assert servers == [
        {
            "name": "mcp-demo",
            "url": "http://mcp-demo:8080/mcp",
            "audience": "mcp-demo",
            "protocol_version": "2026-07-28",
            "tools": [
                {"name": "get_time", "read_only": True, "data_class": "public"},
                {"name": "create_note", "read_only": False, "data_class": "confidential"},
            ],
        }
    ]
    assert agents[0]["model"] == "chat-fast"
    assert "dev-инсталляции" in agents[0]["instructions"]
    assert agents[0]["max_steps"] == 5
    clearances = json.loads((out / "platform-api" / "models.json").read_text())
    assert {c["name"]: c["max_data_class"] for c in clearances} == {
        "chat-default": "restricted",
        "chat-fast": "restricted",
        "chat-lab": "internal",
    }
    summary = json.loads((out / "instance.json").read_text())
    assert summary["id"] == "dev"
    assert summary["mcp_audiences"] == ["mcp-demo"]
    assert summary["models"] == ["chat-default", "chat-fast", "chat-lab"]


def test_core_version_mismatch(
    core_dir: Path, dev_instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = dev_instance / "instance.yaml"
    current = (core_dir / "VERSION").read_text().strip()
    text = manifest.read_text()
    assert f'version: "{current}"' in text
    manifest.write_text(text.replace(f'version: "{current}"', 'version: "9.9.9"'))
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


def test_instructions_file_problems(core_dir: Path, dev_instance: Path) -> None:
    md = dev_instance / "agents" / "assistant.md"
    md.unlink()
    assert main(["--core", str(core_dir), "validate", str(dev_instance)]) == EXIT_INVALID
    (dev_instance.parent / "outside.md").write_text("x")
    md.symlink_to(dev_instance.parent / "outside.md")
    assert main(["--core", str(core_dir), "validate", str(dev_instance)]) == EXIT_INVALID
    md.unlink()
    md.write_text("  \n")
    assert main(["--core", str(core_dir), "validate", str(dev_instance)]) == EXIT_INVALID


def test_render_removes_legacy_layout(core_dir: Path, dev_instance: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "litellm-models.yaml").write_text("old")
    (out / "agents.json").write_text("old")
    assert (
        main(["--core", str(core_dir), "render", str(dev_instance), "--out", str(out)]) == EXIT_OK
    )
    assert not (out / "litellm-models.yaml").exists()
    assert not (out / "agents.json").exists()


def test_old_mcp_protocol_rejected(
    core_dir: Path, dev_instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = dev_instance / "instance.yaml"
    manifest.write_text(manifest.read_text().replace('"2026-07-28"', '"2025-11-25"'))
    assert main(["--core", str(core_dir), "validate", str(dev_instance)]) == EXIT_INVALID
    assert "2025-11-25" in capsys.readouterr().err


def test_render_compliance(core_dir: Path, dev_instance: Path, tmp_path: Path) -> None:
    manifest = dev_instance / "instance.yaml"
    text = manifest.read_text().replace(
        "  privacy_notice_url: https://antec.invalid/privacy\n",
        "  privacy_notice_url: https://antec.invalid/privacy\n"
        '  ai_notice: {fi-FI: "Tekoäly *vastaa* [x](y) $HOME"}\n',
    )
    manifest.write_text(text)
    out = tmp_path / "out"
    assert (
        main(["--core", str(core_dir), "render", str(dev_instance), "--out", str(out)]) == EXIT_OK
    )
    record = (out / "compliance" / "record.md").read_text()
    assert "Реестр контролёра (GDPR, ст. 30(1))" in record
    assert "Anthropic Ireland" in record
    assert "| chat-fast | anthropic |" in record
    facts = (out / "compliance" / "dpia-facts.md").read_text()
    assert "mcp-demo.create_note" in facts
    assert "без ZDR" in facts
    env = (out / "open-webui.env").read_text()
    # $ экранирован для compose, разметка манифеста не исполняется.
    assert "$$HOME" in env
    (banner,) = json.loads(env.split("WEBUI_BANNERS=", 1)[1].splitlines()[0].replace("$$", "$"))
    fi = banner["i18n"]["fi-FI"]["content"]
    assert fi.startswith("Tekoäly \\*vastaa\\* \\[x\\]\\(y\\) $HOME")
    assert fi.endswith("[Tietosuojaseloste](https://antec.invalid/privacy)")
    assert banner["i18n"]["en-US"]["content"].startswith("You are chatting")


def test_core_info(core_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--core", str(core_dir), "core-info"]) == EXIT_OK
    info = json.loads(capsys.readouterr().out)
    assert info["core_version"] == (core_dir / "VERSION").read_text().strip()
    assert info["manifest_schema_versions"] == {
        "supported": [1, 2, 3, 4, 5, 6],
        "deprecated": [1, 2, 3, 4],
    }
    assert info["mcp_protocol_versions"] == ["2026-07-28"]


def test_deprecated_schema_warns(
    core_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inst = tmp_path / "old"
    inst.mkdir()
    (inst / "policies").mkdir()
    version = (core_dir / "VERSION").read_text().strip()
    (inst / "instance.yaml").write_text(
        "schema_version: 1\n"
        "instance: {id: old, name: Old}\n"
        f'core: {{version: "{version}"}}\n'
        "models:\n"
        "  - name: chat-default\n"
        "    description: d\n"
        "    litellm: {model: anthropic/x, api_key: os.environ/ANTHROPIC_API_KEY}\n"
    )
    assert main(["--core", str(core_dir), "validate", str(inst)]) == EXIT_OK
    err = capsys.readouterr().err
    assert "schema_version: 1 устарела" in err
    assert "0.3.0" in err


def test_current_schema_no_deprecation(
    core_dir: Path, dev_instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--core", str(core_dir), "validate", str(dev_instance)]) == EXIT_OK
    assert "устарела" not in capsys.readouterr().err


def test_render_observability(core_dir: Path, dev_instance: Path, tmp_path: Path) -> None:
    manifest = dev_instance / "instance.yaml"
    text = manifest.read_text()
    assert "observability:\n  enabled: true\n" in text
    out = tmp_path / "out"
    assert main(["--core", str(core_dir), "render", str(dev_instance), "--out", str(out)]) == 0
    env = (out / "platform-api.env").read_text()
    assert "PLATFORM_OTEL_ENDPOINT=http://otel-collector:4318\n" in env
    assert (
        "PLATFORM_OTEL_ENDPOINT=http://otel-collector:4318\n"
        in (out / "platform-worker.env").read_text()
    )
    summary = json.loads((out / "instance.json").read_text())
    assert summary["observability"] == {"enabled": True, "telemetry_retention": "30d"}
    # Без alert_email получатель недоставляемый: не встроенный адрес-пример Grafana.
    contact = yaml.safe_load((out / "grafana/alerting/contact-points.yaml").read_text())
    assert contact["contactPoints"][0]["receivers"][0]["settings"]["addresses"] == (
        "alerts@antec.invalid"
    )
    assert contact["policies"][0]["receiver"] == "antec-email"
    rules = yaml.safe_load((out / "grafana/alerting/rules.yaml").read_text())
    assert {r["uid"] for r in rules["groups"][0]["rules"]} >= {
        "antec-api-down",
        "antec-worker-stalled",
    }
    # Адрес алертов: контакт и политика уведомлений Grafana.
    manifest.write_text(
        text.replace(
            "observability:\n  enabled: true\n",
            'observability:\n  enabled: true\n  alert_email: "ops@acme.fi"\n',
        )
    )
    assert main(["--core", str(core_dir), "render", str(dev_instance), "--out", str(out)]) == 0
    contact = yaml.safe_load((out / "grafana/alerting/contact-points.yaml").read_text())
    assert contact["contactPoints"][0]["receivers"][0]["settings"]["addresses"] == "ops@acme.fi"
    # Выключено: ни адреса Collector, ни контакта.
    manifest.write_text(
        text.replace("observability:\n  enabled: true\n", "observability:\n  enabled: false\n")
    )
    assert main(["--core", str(core_dir), "render", str(dev_instance), "--out", str(out)]) == 0
    assert "PLATFORM_OTEL_ENDPOINT" not in (out / "platform-api.env").read_text()
    assert not (out / "grafana/alerting/contact-points.yaml").exists()
    assert not (out / "grafana/alerting/rules.yaml").exists()
