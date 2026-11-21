"""Проверка инструкций FROM в Dockerfile / Containerfile."""

from __future__ import annotations

import re
from collections.abc import Iterator

from pincheck.checks import CheckContext
from pincheck.checks.image import check_image
from pincheck.report import Code, Finding, error

_DIRECTIVE_RE = re.compile(r"^#\s*escape\s*=\s*(\S)\s*$", re.IGNORECASE)
_FROM_RE = re.compile(r"^\s*FROM\s+(?P<args>.+?)\s*$", re.IGNORECASE)


def _logical_lines(text: str) -> Iterator[tuple[int, str]]:
    """Склеивает строки с продолжением; возвращает (номер первой строки, текст)."""
    escape = "\\"
    lines = text.splitlines()
    # Парсер-директивы допустимы только в начале файла.
    for raw in lines:
        m = _DIRECTIVE_RE.match(raw.strip())
        if m is None:
            break
        escape = m.group(1)
    buf: list[str] = []
    start = 0
    for i, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not buf and (not stripped or stripped.startswith("#")):
            continue
        if buf and stripped.startswith("#"):
            continue  # комментарии внутри продолжения игнорируются
        if not buf:
            start = i
        if stripped.endswith(escape):
            buf.append(stripped[: -len(escape)])
            continue
        buf.append(stripped)
        yield start, " ".join(part for part in buf if part)
        buf = []
    if buf:
        yield start, " ".join(part for part in buf if part)


def check_dockerfile(path: str, text: str, ctx: CheckContext) -> list[Finding]:
    findings: list[Finding] = []
    stages: set[str] = set()
    for line, logical in _logical_lines(text):
        m = _FROM_RE.match(logical)
        if m is None:
            continue
        tokens = m.group("args").split()
        while tokens and tokens[0].startswith("--"):
            tokens.pop(0)  # --platform=... и другие флаги
        if not tokens:
            findings.append(
                error(
                    Code.FILE_PARSE,
                    path,
                    line,
                    "FROM без образа",
                    "укажите образ name:tag@sha256:digest",
                )
            )
            continue
        image = tokens[0]
        alias: str | None = None
        if len(tokens) == 3 and tokens[1].lower() == "as":
            alias = tokens[2].lower()
        elif len(tokens) != 1:
            findings.append(
                error(
                    Code.FILE_PARSE,
                    path,
                    line,
                    f"не удалось разобрать FROM: {logical!r}",
                    "используйте FROM [--platform=...] image [AS name]",
                )
            )
            continue
        # scratch и ссылки на предыдущие стадии сборки не являются внешними образами.
        if image.lower() != "scratch" and image.lower() not in stages:
            findings.extend(check_image(image, path, line, ctx))
        if alias is not None:
            stages.add(alias)
    return findings
