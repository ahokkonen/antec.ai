"""План миграций для up.sh и флаг IRREVERSIBLE (ADR-0014, пп. 3, 7). Без БД."""

from __future__ import annotations

import pytest
from alembic.script import ScriptDirectory
from pydantic import SecretStr

from antec_platform.migrate import alembic_config, build_plan
from antec_platform.settings import DatabaseSettings


@pytest.fixture(scope="module")
def script() -> ScriptDirectory:
    settings = DatabaseSettings(database_password=SecretStr("x"))
    return ScriptDirectory.from_config(alembic_config(settings))


def test_empty_database_gets_everything(script: ScriptDirectory) -> None:
    plan = build_plan(script, None)
    assert plan.known
    assert plan.pending[0] == "0001"
    assert plan.pending[-1] == plan.head
    assert len(plan.pending) == len(list(script.walk_revisions()))


def test_up_to_date(script: ScriptDirectory) -> None:
    head = script.get_current_head()
    assert head is not None
    plan = build_plan(script, head)
    assert plan.known
    assert plan.pending == []
    assert plan.irreversible == []


def test_partial_upgrade_lists_irreversible(script: ScriptDirectory) -> None:
    plan = build_plan(script, "0004")
    assert plan.pending[0] == "0005"
    assert "0004" not in plan.pending
    assert "0005" in plan.irreversible


def test_unknown_revision_is_downgrade(script: ScriptDirectory) -> None:
    plan = build_plan(script, "9999")
    assert not plan.known
    assert plan.pending == []


def test_irreversible_flag_matches_downgrade(
    script: ScriptDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IRREVERSIBLE задан явно и совпадает с тем, бросает ли downgrade исключение."""

    class _Op:
        def __getattr__(self, name: str) -> object:
            return lambda *args, **kwargs: None

    for rev in script.walk_revisions():
        module = rev.module
        assert isinstance(getattr(module, "IRREVERSIBLE", None), bool), rev.revision
        for name in ("op", "context"):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, _Op())
        try:
            module.downgrade(app_role="platform_app")
        except RuntimeError:
            raised = True
        else:
            raised = False
        assert module.IRREVERSIBLE is raised, rev.revision
