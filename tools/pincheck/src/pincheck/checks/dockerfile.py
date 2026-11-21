"""Проверка инструкций FROM в Dockerfile / Containerfile."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pincheck.checks import CheckContext
from pincheck.checks.image import check_image
from pincheck.report import Code, Finding, error

_DIRECTIVE_RE = re.compile(r"^#\s*(?P<name>[A-Za-z][A-Za-z0-9_-]*)\s*=\s*(?P<value>.*?)\s*$")
_FROM_RE = re.compile(r"^\s*FROM\s+(?P<args>.+?)\s*$", re.IGNORECASE)
_HEREDOC_RE = re.compile(r"<<(?P<dash>-?)(?P<q>[\"']?)(?P<word>[A-Za-z_][A-Za-z0-9_]*)(?P=q)")
_HEREDOC_INSTRUCTIONS = frozenset({"RUN", "COPY", "ADD"})


@dataclass(frozen=True, slots=True)
class Instruction:
    line: int  # номер первой физической строки
    text: str


@dataclass(slots=True)
class ParsedDockerfile:
    instructions: list[Instruction] = field(default_factory=list)
    problems: list[tuple[int, str]] = field(default_factory=list)


def _escape_char(lines: list[str]) -> str:
    """Символ escape из блока парсер-директив в начале файла (syntax, check, escape, ...).

    Блок заканчивается на первой строке, которая не является директивой.
    """
    escape = "\\"
    for raw in lines:
        m = _DIRECTIVE_RE.fullmatch(raw.strip())
        if m is None:
            break
        if m.group("name").lower() == "escape" and m.group("value") in {"\\", "`"}:
            escape = m.group("value")
    return escape


def parse_dockerfile(text: str) -> ParsedDockerfile:
    """Логические инструкции: продолжения склеены, комментарии, пустые строки и тела heredoc
    (RUN/COPY/ADD <<EOF) пропущены."""
    lines = text.splitlines()
    escape = _escape_char(lines)
    result = ParsedDockerfile()
    buf: list[str] = []
    start = 0
    pending_heredocs: list[tuple[str, bool]] = []  # (разделитель, <<- снимает табуляцию)
    heredoc_start = 0
    for i, raw in enumerate(lines, start=1):
        if pending_heredocs:
            delim, strip_tabs = pending_heredocs[0]
            candidate = raw.lstrip("\t") if strip_tabs else raw
            if candidate == delim:
                pending_heredocs.pop(0)
                heredoc_start = i + 1
            continue
        stripped = raw.strip()
        # Комментарии и пустые строки игнорируются и внутри продолжения (как в BuildKit).
        if not stripped or stripped.startswith("#"):
            continue
        if not buf:
            start = i
        if stripped.endswith(escape):
            buf.append(stripped[: -len(escape)].strip())
            continue
        buf.append(stripped)
        instruction = " ".join(part for part in buf if part)
        buf = []
        result.instructions.append(Instruction(start, instruction))
        keyword = instruction.split(maxsplit=1)[0].upper()
        if keyword in _HEREDOC_INSTRUCTIONS:
            pending_heredocs = [
                (m.group("word"), m.group("dash") == "-") for m in _HEREDOC_RE.finditer(instruction)
            ]
            heredoc_start = i + 1
    if buf:
        result.instructions.append(Instruction(start, " ".join(part for part in buf if part)))
    if pending_heredocs:
        result.problems.append(
            (
                heredoc_start,
                f"heredoc без завершающей строки {pending_heredocs[0][0]!r}: "
                "остаток файла нельзя разобрать однозначно",
            )
        )
    return result


def check_dockerfile(path: str, text: str, ctx: CheckContext) -> list[Finding]:
    findings: list[Finding] = []
    stages: set[str] = set()
    parsed = parse_dockerfile(text)
    for problem_line, problem in parsed.problems:
        findings.append(
            error(
                Code.FILE_PARSE,
                path,
                problem_line,
                problem,
                "завершите heredoc строкой с разделителем",
            )
        )
    for instr in parsed.instructions:
        line, logical = instr.line, instr.text
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
