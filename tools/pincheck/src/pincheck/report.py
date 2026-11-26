"""Нарушения (findings) и их вывод в текстовом формате и в формате аннотаций GitHub Actions."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TextIO


class Severity(enum.StrEnum):
    ERROR = "error"
    WARNING = "warning"


class Code(enum.StrEnum):
    """Стабильные коды нарушений. Номера не переиспользуются."""

    # Реестр
    POLICY_PARSE = "PIN001"
    POLICY_SCHEMA = "PIN002"
    POLICY_DUPLICATE = "PIN003"
    POLICY_DATE = "PIN004"
    POLICY_VERSION = "PIN005"
    DIGEST_NOT_PINNED = "PIN006"
    # Образы
    IMAGE_NO_DIGEST = "PIN010"
    IMAGE_FLOATING_TAG = "PIN011"
    IMAGE_PRERELEASE = "PIN012"
    IMAGE_UNKNOWN = "PIN013"
    IMAGE_VERSION_NOT_ALLOWED = "PIN014"
    IMAGE_BELOW_MIN = "PIN015"
    IMAGE_DIGEST_MISMATCH = "PIN016"
    IMAGE_DENIED_TAG = "PIN017"
    IMAGE_DENIED_DIGEST = "PIN018"
    IMAGE_COMPONENT_NOT_DEPLOYABLE = "PIN019"
    IMAGE_UNRESOLVABLE = "PIN020"
    IMAGE_NO_TAG = "PIN021"
    IMAGE_LOCAL_BUILD = "PIN022"
    IMAGE_CORE_MISMATCH = "PIN023"
    # Python
    PY_FORBIDDEN = "PIN030"
    PY_DENIED_VERSION = "PIN031"
    PY_BELOW_MIN = "PIN032"
    PY_LOCK_PARSE = "PIN033"
    # Файлы
    FILE_PARSE = "PIN040"
    FILE_INPUT = "PIN041"


@dataclass(frozen=True, slots=True)
class Finding:
    severity: Severity
    code: Code
    path: str
    line: int | None
    message: str
    fix: str

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line}" if self.line is not None else self.path


def error(code: Code, path: str, line: int | None, message: str, fix: str) -> Finding:
    return Finding(Severity.ERROR, code, path, line, message, fix)


def warning(code: Code, path: str, line: int | None, message: str, fix: str) -> Finding:
    return Finding(Severity.WARNING, code, path, line, message, fix)


def _sort_key(f: Finding) -> tuple[str, int, str]:
    return (f.path, f.line or 0, f.code.value)


def write_text(findings: list[Finding], out: TextIO) -> None:
    for f in sorted(findings, key=_sort_key):
        out.write(f"{f.location}: {f.severity.value} {f.code.value}: {f.message}\n")
        out.write(f"    как исправить: {f.fix}\n")


def _gh_escape_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _gh_escape_prop(value: str) -> str:
    return _gh_escape_data(value).replace(":", "%3A").replace(",", "%2C")


def write_github(findings: list[Finding], out: TextIO) -> None:
    """Аннотации GitHub Actions (workflow commands) плюс обычный текст для лога."""
    for f in sorted(findings, key=_sort_key):
        props = [f"file={_gh_escape_prop(f.path)}"]
        if f.line is not None:
            props.append(f"line={f.line}")
        props.append(f"title={_gh_escape_prop(f.code.value)}")
        body = _gh_escape_data(f"{f.message}. Как исправить: {f.fix}")
        out.write(f"::{f.severity.value} {','.join(props)}::{body}\n")
    write_text(findings, out)
