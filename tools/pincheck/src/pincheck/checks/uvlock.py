"""Проверка uv.lock: запрещённые пакеты, версии из denylist, минимальные версии, пре-релизы."""

from __future__ import annotations

import re
import tomllib

from pincheck.checks import CheckContext
from pincheck.policy import canonical_py_name
from pincheck.report import Code, Finding, error
from pincheck.versions import PyVersion, VersionError, canonical_py_version

_NAME_LINE_RE = re.compile(r'^name\s*=\s*"(?P<name>[^"]+)"\s*$')


def _package_lines(text: str) -> dict[str, list[int]]:
    """Номера строк `name = "..."` внутри блоков [[package]]."""
    result: dict[str, list[int]] = {}
    in_package = False
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if line.startswith("["):
            in_package = line == "[[package]]"
            continue
        if in_package:
            m = _NAME_LINE_RE.match(line)
            if m is not None:
                result.setdefault(canonical_py_name(m.group("name")), []).append(i)
                in_package = False
    return result


def check_uvlock(path: str, text: str, ctx: CheckContext) -> list[Finding]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [
            error(
                Code.PY_LOCK_PARSE,
                path,
                getattr(exc, "lineno", None),
                f"некорректный uv.lock: {exc}",
                "пересоздайте файл командой uv lock",
            )
        ]
    packages = data.get("package", [])
    if not isinstance(packages, list):
        return [
            error(
                Code.PY_LOCK_PARSE,
                path,
                None,
                "в uv.lock нет списка [[package]]",
                "пересоздайте файл командой uv lock",
            )
        ]

    pol = ctx.policy
    forbidden = {canonical_py_name(n) for n in pol.python_forbidden}
    denylist = {
        canonical_py_name(k): {canonical_py_version(v) for v in vs}
        for k, vs in pol.python_denylist.items()
    }
    minimums = {
        canonical_py_name(k): PyVersion.parse(v) for k, v in pol.python_min_versions.items()
    }
    prerelease_allowed = {canonical_py_name(n) for n in pol.python_prerelease_allowed}
    lines = _package_lines(text)
    seen_count: dict[str, int] = {}
    findings: list[Finding] = []
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        raw_name = pkg.get("name")
        raw_version = pkg.get("version")
        if not isinstance(raw_name, str):
            continue
        name = canonical_py_name(raw_name)
        idx = seen_count.get(name, 0)
        seen_count[name] = idx + 1
        candidates = lines.get(name, [])
        line = candidates[idx] if idx < len(candidates) else None
        label = f"{raw_name} {raw_version}" if isinstance(raw_version, str) else raw_name
        if name in forbidden:
            findings.append(
                error(
                    Code.PY_FORBIDDEN,
                    path,
                    line,
                    f"пакет {label} запрещён в любой версии (python_forbidden)",
                    "удалите зависимость (напрямую или транзитивно) и обращайтесь к сервису "
                    "по HTTP; см. ADR-0001",
                )
            )
        if not isinstance(raw_version, str):
            continue  # пакет из workspace/path без версии
        try:
            canon = canonical_py_version(raw_version)
            parsed = PyVersion.parse(raw_version)
        except VersionError as exc:
            findings.append(
                error(
                    Code.PY_LOCK_PARSE, path, line, str(exc), "пересоздайте файл командой uv lock"
                )
            )
            continue
        if canon in denylist.get(name, set()):
            findings.append(
                error(
                    Code.PY_DENIED_VERSION,
                    path,
                    line,
                    f"версия {label} в python_denylist (известная вредоносная или отозванная)",
                    f"смените версию и пересоберите lock: uv lock --upgrade-package {raw_name}; "
                    "если пакет устанавливался, действуйте по ADR-0004, п. 10",
                )
            )
        if parsed.is_prerelease and name not in prerelease_allowed:
            findings.append(
                error(
                    Code.PY_PRERELEASE,
                    path,
                    line,
                    f"версия {label} является пре-релизом (PEP 440); пре-релизы запрещены "
                    "(ADR-0016)",
                    "закрепите стабильную версию или, если проект так нумерует стабильные "
                    "выпуски, добавьте пакет в python_prerelease_allowed с причиной через PR",
                )
            )
        minimum = minimums.get(name)
        if minimum is not None and parsed.is_below(minimum):
            findings.append(
                error(
                    Code.PY_BELOW_MIN,
                    path,
                    line,
                    f"версия {label} ниже минимальной {minimum.text} (python_min_versions)",
                    f'обновите: uv add "{raw_name}>={minimum.text}" или uv lock '
                    f"--upgrade-package {raw_name}",
                )
            )
    return findings
