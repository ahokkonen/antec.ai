"""Проверка образов в docker-compose и Dockerfile по тестовому реестру."""

from __future__ import annotations

import pytest
from conftest import FIXTURES, Runner

COMPOSE = FIXTURES / "compose"


@pytest.mark.parametrize(
    ("case", "expected_errors"),
    [
        pytest.param("litellm_v1_107_0", ["PIN017"], id="litellm-v1.107.0-denied-tag"),
        pytest.param("malicious_digest", ["PIN018"], id="denylist-digest"),
        pytest.param(
            "malicious_variant_digest", ["PIN013", "PIN018"], id="denylist-digest-other-repo"
        ),
        pytest.param("malicious_digest_prefix", ["PIN018"], id="denylist-digest-prefix"),
        pytest.param("latest", ["PIN011"], id="latest-tag"),
        pytest.param("latest_no_digest", ["PIN010", "PIN011"], id="latest-without-digest"),
        pytest.param("no_digest", ["PIN010"], id="image-without-digest"),
        pytest.param("unknown_image", ["PIN013"], id="unknown-image"),
        pytest.param("below_min", ["PIN015"], id="below-min-version"),
        pytest.param("empty_allowed", ["PIN019"], id="component-with-empty-allowed"),
        pytest.param("candidate", ["PIN014"], id="candidate-in-quarantine"),
        pytest.param("not_allowed", ["PIN014"], id="version-not-in-allowed"),
        pytest.param("prerelease", ["PIN012"], id="prerelease"),
        pytest.param("digest_mismatch", ["PIN016"], id="digest-mismatch"),
        pytest.param("variable", ["PIN020"], id="variable-in-image"),
        pytest.param("missing_prefix", ["PIN014"], id="missing-tag-prefix"),
        pytest.param("variant_suffix", ["PIN014"], id="variant-suffix"),
        pytest.param("no_tag", ["PIN021"], id="digest-without-tag"),
        pytest.param("bad_digest", ["PIN020"], id="malformed-digest"),
        pytest.param("broken", ["PIN040"], id="broken-compose-yaml"),
    ],
)
def test_compose_violations(pincheck: Runner, case: str, expected_errors: list[str]) -> None:
    result = pincheck(COMPOSE / case)
    assert result.exit_code == 1, result.output
    assert sorted(result.errors) == sorted(expected_errors), result.output
    # Каждое сообщение содержит файл, строку и подсказку.
    assert f"{case}/compose.y" in result.output
    assert "как исправить:" in result.output


def test_valid_compose_passes(pincheck: Runner) -> None:
    result = pincheck(COMPOSE / "valid")
    assert result.exit_code == 0, result.output
    assert result.errors == []
    assert result.warnings == []
    assert "Ошибок: 0" in result.output


def test_message_points_to_file_and_line(pincheck: Runner) -> None:
    result = pincheck(COMPOSE / "no_digest")
    assert "compose/no_digest/compose.yaml:3: error PIN010" in result.output
    assert "quay.io/keycloak/keycloak:<tag>@sha256:<digest>" in result.output


def test_candidate_message_mentions_quarantine(pincheck: Runner) -> None:
    result = pincheck(COMPOSE / "candidate")
    assert "карантин до 2026-11-25" in result.output


def test_digest_null_is_warning_by_default(pincheck: Runner) -> None:
    result = pincheck(COMPOSE / "docker_hub_short")
    assert result.exit_code == 0, result.output
    assert result.errors == []
    assert result.warnings == ["PIN006"]


def test_require_digest_turns_null_digest_into_error(pincheck: Runner) -> None:
    result = pincheck(COMPOSE / "docker_hub_short", extra=("--require-digest",))
    assert result.exit_code == 1
    assert result.errors == ["PIN006"]


def test_missing_digest_is_error_regardless_of_flags(pincheck: Runner) -> None:
    assert pincheck(COMPOSE / "no_digest").errors == ["PIN010"]
    assert pincheck(COMPOSE / "no_digest", extra=("--require-digest",)).errors == ["PIN010"]


def test_older_allowed_version_with_pinned_digest_warns(pincheck: Runner) -> None:
    result = pincheck(COMPOSE / "older_allowed")
    assert result.exit_code == 0, result.output
    assert result.warnings == ["PIN006"]


def test_multi_service_compose(pincheck: Runner) -> None:
    result = pincheck(COMPOSE / "multi")
    assert result.exit_code == 1
    assert result.errors == ["PIN010", "PIN011"]
    assert "docker-compose.prod.yml:11:" in result.output


def test_compose_without_services_is_ok(pincheck: Runner) -> None:
    result = pincheck(COMPOSE / "no_services")
    assert result.exit_code == 0, result.output


def test_dockerfile_valid_multistage(pincheck: Runner) -> None:
    result = pincheck(FIXTURES / "dockerfile" / "valid")
    assert result.exit_code == 0, result.output
    assert "Dockerfile: 1" in result.output


def test_dockerfile_invalid(pincheck: Runner) -> None:
    result = pincheck(FIXTURES / "dockerfile" / "invalid")
    assert result.exit_code == 1
    assert result.errors == ["PIN020", "PIN010", "PIN011", "PIN010"]
    assert "Containerfile:2:" in result.output
    assert "Containerfile:4:" in result.output


def test_github_format_emits_annotations(pincheck: Runner) -> None:
    result = pincheck(COMPOSE / "no_digest", extra=("--format", "github"))
    first = result.output.splitlines()[0]
    assert first.startswith("::error file=")
    assert "line=3" in first
    assert "title=PIN010" in first


def test_prefix_match_is_reported_as_prefix(pincheck: Runner) -> None:
    result = pincheck(COMPOSE / "malicious_digest_prefix")
    assert "совпадает по префиксу sha256:c00f9d92c427" in result.output
    assert "single-source" in result.output


def test_compose_merge_explicit_image_overrides_inherited(pincheck: Runner) -> None:
    result = pincheck(COMPOSE / "merge_override")
    assert result.exit_code == 0, result.output


def test_compose_merge_inherited_services_do_not_hide_explicit(pincheck: Runner) -> None:
    result = pincheck(COMPOSE / "merge_services")
    assert result.exit_code == 1
    assert result.errors == ["PIN010", "PIN011"]
    assert "merge_services/compose.yaml:7:" in result.output


@pytest.mark.parametrize(
    ("case", "line", "expected"),
    [
        pytest.param("escape_backtick", 5, ["PIN010", "PIN021"], id="escape-after-syntax"),
        pytest.param(
            "blank_in_continuation", 1, ["PIN010", "PIN011"], id="blank-line-in-continuation"
        ),
        pytest.param("heredoc_stage", 9, ["PIN010", "PIN021"], id="heredoc-fake-stage"),
        pytest.param("heredoc_unterminated", 3, ["PIN040"], id="heredoc-unterminated"),
    ],
)
def test_dockerfile_parsing_regressions(
    pincheck: Runner, case: str, line: int, expected: list[str]
) -> None:
    result = pincheck(FIXTURES / "dockerfile" / case)
    assert result.exit_code == 1
    assert result.errors == expected, result.output
    assert f"{case}/Dockerfile:{line}:" in result.output


def test_local_build_images(pincheck: Runner) -> None:
    assert pincheck(COMPOSE / "local_build_ok").exit_code == 0


def test_local_build_images_invalid(pincheck: Runner) -> None:
    result = pincheck(COMPOSE / "local_build_bad")
    assert result.exit_code == 1
    # a: чужой образ с build; b: latest; c: digest; d: без build -> не из реестра
    assert result.errors == ["PIN022", "PIN022", "PIN022", "PIN010", "PIN013"], result.output
