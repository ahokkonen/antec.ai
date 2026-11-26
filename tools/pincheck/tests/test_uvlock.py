"""Проверка uv.lock."""

from __future__ import annotations

from pathlib import Path

from conftest import FIXTURES, REPO_ROOT, VALID_POLICY, Runner, real_today

UVLOCK = FIXTURES / "uvlock"


def test_litellm_denied_version(pincheck: Runner) -> None:
    result = pincheck(UVLOCK / "litellm_denied")
    assert result.exit_code == 1
    assert sorted(result.errors) == ["PIN030", "PIN031"]
    assert "uv.lock:10:" in result.output


def test_litellm_forbidden_in_any_version(pincheck: Runner) -> None:
    result = pincheck(UVLOCK / "litellm_any")
    assert result.exit_code == 1
    assert result.errors == ["PIN030"]
    assert "ADR-0001" in result.output


def test_below_python_min_version(pincheck: Runner) -> None:
    result = pincheck(UVLOCK / "below_min")
    assert result.exit_code == 1
    assert result.errors == ["PIN032"]
    assert "PyYAML 6.0.0" in result.output


def test_clean_lock(pincheck: Runner) -> None:
    result = pincheck(UVLOCK / "clean")
    assert result.exit_code == 0, result.output
    assert "uv.lock: 1" in result.output


def test_broken_lock(pincheck: Runner) -> None:
    result = pincheck(UVLOCK / "broken")
    assert result.exit_code == 1
    assert result.errors == ["PIN033"]


def test_own_lockfile_is_clean(pincheck: Runner) -> None:
    """uv.lock самого pincheck соответствует боевому реестру."""
    result = pincheck(
        REPO_ROOT / "tools" / "pincheck" / "uv.lock",
        policy=REPO_ROOT / "policy" / "allowed-versions.yaml",
        extra=("--today", real_today()),
    )
    assert result.exit_code == 0, result.output


def _lock(tmp_path: Path, version: str) -> Path:
    d = tmp_path / "lock"
    d.mkdir(exist_ok=True)
    (d / "uv.lock").write_text(
        "version = 1\n\n[[package]]\n"
        f'name = "opentelemetry-semantic-conventions"\nversion = "{version}"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )
    return d


def _policy_with_allowlist(tmp_path: Path) -> Path:
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        VALID_POLICY.read_text()
        + "python_prerelease_allowed:\n"
        + "  Opentelemetry_Semantic_Conventions:\n"
        + "    versions: '0\\.\\d+b\\d+'\n"
        + "    reason: беточная нумерация проекта (ADR-0013)\n"
    )
    return policy


def test_prerelease_python_package_rejected(pincheck: Runner, tmp_path: Path) -> None:
    for version in ("0.66b2", "1.0rc1", "2.0.dev3", "1.0a1"):
        result = pincheck(_lock(tmp_path, version))
        assert result.errors == ["PIN034"], result.output


def test_prerelease_allowlist(pincheck: Runner, tmp_path: Path) -> None:
    policy = _policy_with_allowlist(tmp_path)
    result = pincheck(_lock(tmp_path, "0.66b2"), policy=policy)
    assert result.exit_code == 0, result.output
    # Шаблон ограничивает версии: настоящий rc того же пакета запрещён.
    result = pincheck(_lock(tmp_path, "1.0.0rc1"), policy=policy)
    assert result.errors == ["PIN034"], result.output


def test_bad_prerelease_pattern(pincheck: Runner, tmp_path: Path) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        VALID_POLICY.read_text()
        + "python_prerelease_allowed:\n  x:\n    versions: '('\n    reason: r\n"
    )
    result = pincheck(_lock(tmp_path, "1.0"), policy=policy)
    assert result.exit_code == 1
    assert "PIN002" in result.errors, result.output


def test_stable_and_post_release_pass(pincheck: Runner, tmp_path: Path) -> None:
    for version in ("1.45.2", "1.0.post1"):
        result = pincheck(_lock(tmp_path, version))
        assert result.errors == [], result.output
