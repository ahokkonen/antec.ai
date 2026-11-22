from __future__ import annotations

from pathlib import Path

from antec_instance.policies import check_policies

POLICY = """apiVersion: api.cerbos.dev/v1
resourcePolicy:
  version: default
  resource: model
{extra}  rules: []
"""


def _write(root: Path, name: str, text: str) -> None:
    path = root / "policies" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_dev_instance_policies_ok(dev_instance: Path) -> None:
    assert check_policies(dev_instance, "dev") == []


def test_no_policies_dir(tmp_path: Path) -> None:
    assert check_policies(tmp_path, "acme") == []


def test_unscoped_policy_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "model.yaml", POLICY.format(extra=""))
    [problem] = check_policies(tmp_path, "acme")
    assert "принадлежат ядру" in problem


def test_foreign_scope_rejected(tmp_path: Path) -> None:
    extra = "  scope: other\n  scopePermissions: SCOPE_PERMISSIONS_OVERRIDE_PARENT\n"
    _write(tmp_path, "model.yaml", POLICY.format(extra=extra))
    assert "'acme'" in check_policies(tmp_path, "acme")[0]


def test_scope_permissions_required(tmp_path: Path) -> None:
    _write(tmp_path, "model.yml", POLICY.format(extra="  scope: acme\n"))
    assert "scopePermissions" in check_policies(tmp_path, "acme")[0]


def test_tests_and_shared_kinds_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "tests/model_test.yaml", "name: X\n")
    _write(tmp_path, "testdata/x.yaml", "anything: 1\n")
    _write(
        tmp_path,
        "roles.yaml",
        "apiVersion: api.cerbos.dev/v1\nderivedRoles:\n  name: r\n  definitions: []\n",
    )
    assert check_policies(tmp_path, "acme") == []


def test_role_policy_and_garbage_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "role.yaml", "apiVersion: api.cerbos.dev/v1\nrolePolicy: {}\n")
    _write(tmp_path, "bad.yaml", "a: [\n")
    _write(tmp_path, "dup.yaml", "apiVersion: x\napiVersion: y\n")
    problems = check_policies(tmp_path, "acme")
    assert len(problems) == 3
    assert any("rolePolicy" in p for p in problems)
    assert any("повторяющийся ключ" in p for p in problems)


def test_symlink_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "real.yaml", POLICY.format(extra=""))
    (tmp_path / "policies" / "link.yaml").symlink_to(tmp_path / "policies" / "real.yaml")
    assert any("символические" in p for p in check_policies(tmp_path, "acme"))


def test_hidden_files_rejected(tmp_path: Path) -> None:
    _write(tmp_path, ".x/model.yaml", POLICY.format(extra=""))
    _write(tmp_path, ".hidden.yaml", POLICY.format(extra=""))
    problems = check_policies(tmp_path, "acme")
    assert len(problems) == 2
    assert all("скрытые" in p for p in problems)


def test_common_top_level_keys_allowed(tmp_path: Path) -> None:
    extra = "  scope: acme\n  scopePermissions: SCOPE_PERMISSIONS_OVERRIDE_PARENT\n"
    text = "$schema: https://api.cerbos.dev/latest/cerbos/policy/v1/Policy.schema.json\n"
    text += "disabled: false\n" + POLICY.format(extra=extra)
    _write(tmp_path, "model.yaml", text)
    assert check_policies(tmp_path, "acme") == []


def test_unhashable_key_is_error(tmp_path: Path) -> None:
    _write(tmp_path, "bad.yaml", "? [a, b]\n: 1\n")
    assert "скаляром" in check_policies(tmp_path, "acme")[0]
