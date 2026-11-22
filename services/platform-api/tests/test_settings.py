"""Настройки: секреты только из файлов, списки через запятую."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from antec_platform.settings import Settings


def _settings(secrets: Path) -> Settings:
    return Settings(_secrets_dir=secrets)


@pytest.fixture
def secrets(tmp_path: Path) -> Path:
    (tmp_path / "platform_database_password").write_text("p@ss/w:rd\n")
    (tmp_path / "platform_gateway_api_key").write_text("sk-test")
    return tmp_path


@pytest.mark.usefixtures("no_env")
def test_secrets_from_files(secrets: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLATFORM_OIDC_ISSUER", "http://localhost:8080/realms/antec/")
    monkeypatch.setenv("PLATFORM_OIDC_JWKS_URL", "http://keycloak:8080/certs")
    monkeypatch.setenv("PLATFORM_ALLOWED_CLIENTS", "open-webui, antec-smoke")
    settings = _settings(secrets)
    assert settings.database_password.get_secret_value() == "p@ss/w:rd"
    assert settings.gateway_api_key.get_secret_value() == "sk-test"
    assert settings.allowed_clients == ["open-webui", "antec-smoke"]
    assert settings.oidc_issuer == "http://localhost:8080/realms/antec"
    url = settings.url()
    assert url.password == "p@ss/w:rd"
    assert "p@ss" not in repr(url)
    assert "sk-test" not in repr(settings)


@pytest.mark.usefixtures("no_env")
def test_missing_secret_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLATFORM_OIDC_ISSUER", "http://x")
    monkeypatch.setenv("PLATFORM_OIDC_JWKS_URL", "http://x/certs")
    with pytest.raises(ValidationError):
        _settings(tmp_path)


@pytest.mark.usefixtures("no_env")
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PLATFORM_ALLOWED_CLIENTS", ""),
        ("PLATFORM_ALLOWED_CLIENTS", "bad client"),
        ("PLATFORM_MODELS", "Chat Default"),
        ("PLATFORM_APP_ROLE", "Robert'); drop"),
        ("PLATFORM_POLICY_SCOPE", "a.b"),
        ("PLATFORM_POLICY_SCOPE", "Acme"),
    ],
)
def test_invalid_values(
    secrets: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv("PLATFORM_OIDC_ISSUER", "http://x")
    monkeypatch.setenv("PLATFORM_OIDC_JWKS_URL", "http://x/certs")
    monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        _settings(secrets)


@pytest.mark.usefixtures("no_env")
@pytest.mark.parametrize(
    ("value", "expected"), [("", None), ("dev", "dev"), ("acme-oy", "acme-oy")]
)
def test_policy_scope(
    secrets: Path, monkeypatch: pytest.MonkeyPatch, value: str, expected: str | None
) -> None:
    monkeypatch.setenv("PLATFORM_OIDC_ISSUER", "http://x")
    monkeypatch.setenv("PLATFORM_OIDC_JWKS_URL", "http://x/certs")
    monkeypatch.setenv("PLATFORM_POLICY_SCOPE", value)
    assert _settings(secrets).policy_scope == expected
