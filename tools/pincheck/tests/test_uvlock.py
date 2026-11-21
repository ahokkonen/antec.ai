"""Проверка uv.lock."""

from __future__ import annotations

from conftest import FIXTURES, REPO_ROOT, Runner, real_today

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
