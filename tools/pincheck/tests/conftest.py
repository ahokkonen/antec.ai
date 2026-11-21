from __future__ import annotations

import io
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from pincheck.cli import run

FIXTURES = Path(__file__).parent / "fixtures"
VALID_POLICY = FIXTURES / "policy" / "valid.yaml"
REPO_ROOT = Path(__file__).resolve().parents[3]
TODAY = "2026-11-21"

_CODE_RE = re.compile(r"^\S+: (error|warning) (PIN\d{3}):", re.MULTILINE)


@dataclass(frozen=True)
class Result:
    exit_code: int
    output: str

    @property
    def errors(self) -> list[str]:
        return [code for sev, code in _CODE_RE.findall(self.output) if sev == "error"]

    @property
    def warnings(self) -> list[str]:
        return [code for sev, code in _CODE_RE.findall(self.output) if sev == "warning"]


Runner = Callable[..., Result]


@pytest.fixture
def pincheck(monkeypatch: pytest.MonkeyPatch) -> Runner:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    def _run(
        *paths: str | Path, policy: str | Path = VALID_POLICY, extra: tuple[str, ...] = ()
    ) -> Result:
        out = io.StringIO()
        argv = ["--policy", str(policy), "--today", TODAY, "--format", "text", *extra]
        argv += [str(p) for p in paths]
        code = run(argv, out)
        return Result(code, out.getvalue())

    return _run


@pytest.fixture
def policy_variant(tmp_path: Path) -> Callable[[str, str], Path]:
    """Копия валидного тестового реестра с одной заменой текста."""

    def _make(old: str, new: str) -> Path:
        text = VALID_POLICY.read_text(encoding="utf-8")
        assert old in text, f"фрагмент не найден в фикстуре: {old!r}"
        path = tmp_path / "policy.yaml"
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return path

    return _make
