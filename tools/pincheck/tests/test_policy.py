"""Валидация самого реестра."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import FIXTURES, REPO_ROOT, Runner

Variant = Callable[[str, str], Path]
EMPTY = FIXTURES / "empty"


def test_real_registry_is_valid(pincheck: Runner) -> None:
    """Боевой реестр репозитория проходит проверку."""
    result = pincheck(EMPTY, policy=REPO_ROOT / "policy" / "allowed-versions.yaml")
    assert result.exit_code == 0, result.output


def test_nothing_to_check_passes_with_message(pincheck: Runner) -> None:
    result = pincheck(EMPTY)
    assert result.exit_code == 0
    assert "Проверять нечего" in result.output


def test_broken_yaml(pincheck: Runner) -> None:
    result = pincheck(EMPTY, policy=FIXTURES / "policy" / "broken_yaml.yaml")
    assert result.exit_code == 1
    assert result.errors == ["PIN001"]
    assert "невалиден" in result.output
    assert "broken_yaml.yaml:3:" in result.output or "broken_yaml.yaml:2:" in result.output


def test_missing_policy_file(pincheck: Runner, tmp_path: Path) -> None:
    result = pincheck(EMPTY, policy=tmp_path / "nope.yaml")
    assert result.exit_code == 1
    assert result.errors == ["PIN041"]


def test_broken_registry_stops_file_checks(pincheck: Runner, policy_variant: Variant) -> None:
    policy = policy_variant("schema_version: 1", "schema_version: 2")
    result = pincheck(FIXTURES / "compose" / "no_digest", policy=policy)
    assert result.exit_code == 1
    assert result.errors == ["PIN002"]
    assert "Файлы не проверялись" in result.output


@pytest.mark.parametrize(
    ("old", "new", "code", "line"),
    [
        pytest.param("schema_version: 1", "schema_version: 2", "PIN002", 3, id="schema-version"),
        pytest.param(
            "reviewed_at: 2026-11-21", "reviewed_at: 2026-11-31", "PIN001", None, id="invalid-date"
        ),
        pytest.param(
            "reviewed_at: 2026-11-21", "reviewed_at: 2026-12-01", "PIN004", 4, id="date-in-future"
        ),
        pytest.param(
            "reviewed_at: 2026-11-21", 'reviewed_at: "21.11.2026"', "PIN002", 4, id="date-format"
        ),
        pytest.param(
            "  keycloak:\n",
            "  litellm:\n    role: x\n  keycloak:\n",
            "PIN003",
            None,
            id="duplicate-component-key",
        ),
        pytest.param(
            'allowed: ["26.8.2", "26.8.1"]',
            'allowed: ["26.8.2", "26.8.2"]',
            "PIN003",
            None,
            id="duplicate-in-allowed",
        ),
        pytest.param(
            "image: ghcr.io/cerbos/cerbos",
            "image: postgres",
            "PIN003",
            None,
            id="same-image-in-two-components",
        ),
        pytest.param(
            'allowed: ["1.108.7"]', 'allowed: ["v1.108.7"]', "PIN002", None, id="prefix-in-allowed"
        ),
        pytest.param(
            'allowed: ["1.108.7"]',
            'allowed: ["1.108.7-rc1"]',
            "PIN002",
            None,
            id="prerelease-in-allowed",
        ),
        pytest.param(
            'allowed: ["1.108.7"]',
            'allowed: ["1.108.7", "1.110.0"]',
            "PIN004",
            None,
            id="candidate-before-not-before",
        ),
        pytest.param(
            'allowed: ["1.108.7"]',
            'allowed: ["1.108.7", "1.105.0"]',
            "PIN005",
            None,
            id="allowed-below-min",
        ),
        pytest.param(
            'allowed: ["1.108.7"]',
            'allowed: ["1.108.7", "1.107.0"]',
            "PIN005",
            None,
            id="allowed-and-denied",
        ),
        pytest.param(
            'version: "1.110.0"', 'version: "1.107.0"', "PIN005", None, id="candidate-denied"
        ),
        pytest.param(
            'digest: "sha256:aaaa', 'digest: "sha256:AAAA', "PIN002", None, id="bad-digest-format"
        ),
        pytest.param(
            "image: quay.io/keycloak/keycloak",
            "image: quay.io/keycloak/keycloak:26.8.2",
            "PIN002",
            None,
            id="image-with-tag",
        ),
        pytest.param(
            "  prerelease_forbidden: true",
            "  prerelease_forbidden_in_prod: true",
            "PIN002",
            None,
            id="legacy-field",
        ),
        pytest.param(
            "  prerelease_forbidden: true",
            "  prerelease_forbidden: false",
            "PIN002",
            None,
            id="prerelease-cannot-be-allowed",
        ),
        pytest.param(
            "    role: secrets\n",
            "    role: secrets\n    rol: x\n",
            "PIN002",
            None,
            id="unknown-field",
        ),
        pytest.param(
            "python_forbidden: [litellm]",
            "python_forbidden: [litellm, LiteLLM]",
            "PIN003",
            None,
            id="duplicate-python-forbidden",
        ),
        pytest.param(
            '  pyyaml: "6.0.1"',
            '  pyyaml: "not-a-version"',
            "PIN002",
            None,
            id="bad-python-min-version",
        ),
    ],
)
def test_broken_registry(
    pincheck: Runner, policy_variant: Variant, old: str, new: str, code: str, line: int | None
) -> None:
    result = pincheck(EMPTY, policy=policy_variant(old, new))
    assert result.exit_code == 1, result.output
    assert code in result.errors, result.output
    if line is not None:
        assert f"policy.yaml:{line}:" in result.output


def test_duplicate_key_reports_both_lines(pincheck: Runner, policy_variant: Variant) -> None:
    result = pincheck(
        EMPTY, policy=policy_variant("quarantine_days: 7", "quarantine_days: 7\nquarantine_days: 8")
    )
    assert result.errors == ["PIN003"]
    assert "policy.yaml:6:" in result.output
    assert "первое вхождение в строке 5" in result.output


def test_candidate_after_not_before_may_be_promoted(
    pincheck: Runner, policy_variant: Variant, tmp_path: Path
) -> None:
    policy = policy_variant('allowed: ["1.108.7"]', 'allowed: ["1.110.0", "1.108.7"]')
    result = pincheck(EMPTY, policy=policy, extra=("--today", "2026-11-25"))
    assert result.exit_code == 0, result.output


def test_schema_error_points_to_line(pincheck: Runner, policy_variant: Variant) -> None:
    result = pincheck(EMPTY, policy=policy_variant('allowed: ["1.108.7"]', 'allowed: ["v1.108.7"]'))
    assert "policy.yaml:15: error PIN002: components.litellm.allowed.0" in result.output
