"""Проверка политик Cerbos инсталляции (ADR-0008, п. 4).

Каждая resource и principal policy инсталляции живёт в scope = id инсталляции с
`SCOPE_PERMISSIONS_OVERRIDE_PARENT`; базовые политики (без scope) принадлежат только ядру.
Компиляцию и тесты политик делает сам Cerbos (`cerbos compile` над ядром и инсталляцией вместе),
здесь только то, чего Cerbos не знает: принадлежность политик инсталляции.
"""

from __future__ import annotations

from pathlib import Path

from antec_instance.yamlload import YamlError, load_all

POLICIES_DIR = "policies"
OVERRIDE_PARENT = "SCOPE_PERMISSIONS_OVERRIDE_PARENT"
_SCOPED_KINDS = ("resourcePolicy", "principalPolicy")
# Без scope: не меняют решений сами по себе, их импортируют политики того же scope.
_SHARED_KINDS = ("derivedRoles", "exportVariables", "exportConstants")
_POLICY_SUFFIXES = (".yaml", ".yml", ".json")
# Общие поля верхнего уровня политики Cerbos (policy.proto), кроме самой политики.
_COMMON_KEYS = frozenset(
    {"apiVersion", "$schema", "description", "disabled", "metadata", "variables"}
)


def _is_policy_file(path: Path, root: Path) -> bool:
    if path.suffix not in _POLICY_SUFFIXES or not path.is_file():
        return False
    rel = path.relative_to(root)
    # Тесты и фикстуры Cerbos: *_test.yaml и каталоги testdata.
    return not (path.stem.endswith("_test") or "testdata" in rel.parts[:-1])


def _check_document(doc: object, instance_id: str) -> str | None:
    if not isinstance(doc, dict) or doc.get("apiVersion") != "api.cerbos.dev/v1":
        return "ожидается политика Cerbos (apiVersion: api.cerbos.dev/v1)"
    kinds = [k for k in doc if k not in _COMMON_KEYS]
    if len(kinds) != 1:
        return "в документе должна быть ровно одна политика"
    kind = kinds[0]
    if kind in _SHARED_KINDS:
        return None
    if kind not in _SCOPED_KINDS:
        return f"вид политики {kind} не поддерживается контрактом инсталляции v1"
    body = doc[kind]
    if not isinstance(body, dict):
        return f"{kind}: ожидается словарь"
    if body.get("scope") != instance_id:
        return (
            f"{kind}: scope должен быть {instance_id!r} (id инсталляции), "
            f"получено {body.get('scope')!r}; политики без scope принадлежат ядру"
        )
    if body.get("scopePermissions") != OVERRIDE_PARENT:
        return f"{kind}: scopePermissions должен быть {OVERRIDE_PARENT}"
    return None


def check_policies(instance_dir: Path, instance_id: str) -> list[str]:
    """Нарушения в каталоге `policies/` инсталляции; каталога нет = переопределений нет."""
    root = instance_dir / POLICIES_DIR
    if not root.is_dir():
        return []
    problems: list[str] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_symlink():
            problems.append(f"{path.relative_to(instance_dir)}: символические ссылки запрещены")
            continue
        # Скрытые файлы и каталоги запрещены, а не пропускаются: что из них прочитает Cerbos,
        # валидатор не решает (ADR-0008, п. 4).
        if path.is_file() and any(part.startswith(".") for part in rel.parts):
            problems.append(f"{path.relative_to(instance_dir)}: скрытые файлы и каталоги запрещены")
            continue
        if not _is_policy_file(path, root):
            continue
        name = str(path.relative_to(instance_dir))
        try:
            docs = load_all(path)
        except YamlError as exc:
            problems.append(f"{name}: {exc}")
            continue
        docs = [d for d in docs if d is not None]
        if not docs:
            problems.append(f"{name}: пустой файл")
        for doc in docs:
            problem = _check_document(doc, instance_id)
            if problem:
                problems.append(f"{name}: {problem}")
    return problems
