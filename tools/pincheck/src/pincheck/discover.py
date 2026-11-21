"""Поиск проверяемых файлов."""

from __future__ import annotations

import enum
import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path


class FileKind(enum.Enum):
    COMPOSE = "compose"
    DOCKERFILE = "Dockerfile"
    UV_LOCK = "uv.lock"


_COMPOSE_PATTERNS = ("compose*.yml", "compose*.yaml", "docker-compose*.yml", "docker-compose*.yaml")
_DOCKERFILE_PATTERNS = (
    "dockerfile",
    "dockerfile.*",
    "*.dockerfile",
    "containerfile",
    "containerfile.*",
)
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        ".nox",
    }
)


# Dockerfile.<суффикс> с такими суффиксами считается исходником, а не Dockerfile
# (например, модуль dockerfile.py или документация dockerfile.md).
_NOT_DOCKERFILE_SUFFIXES = frozenset(
    {
        "py",
        "pyi",
        "pyc",
        "md",
        "rst",
        "txt",
        "json",
        "toml",
        "yaml",
        "yml",
        "sh",
        "lock",
        "orig",
        "bak",
        "swp",
        "go",
        "js",
        "ts",
        "rs",
        "html",
    }
)


def classify(name: str) -> FileKind | None:
    lower = name.lower()
    if lower == "uv.lock":
        return FileKind.UV_LOCK
    if any(fnmatch.fnmatchcase(lower, p) for p in _COMPOSE_PATTERNS):
        return FileKind.COMPOSE
    if any(fnmatch.fnmatchcase(lower, p) for p in _DOCKERFILE_PATTERNS):
        stem, dot, suffix = lower.partition(".")
        if dot and stem in {"dockerfile", "containerfile"} and suffix in _NOT_DOCKERFILE_SUFFIXES:
            return None
        return FileKind.DOCKERFILE
    return None


@dataclass(frozen=True, slots=True)
class Target:
    path: Path
    display: str
    kind: FileKind


@dataclass(frozen=True, slots=True)
class Discovery:
    targets: list[Target]
    missing: list[str]  # несуществующие пути из аргументов
    unsupported: list[str]  # явно указанные файлы неизвестного типа


def _display(path: Path, cwd: Path) -> str:
    try:
        return path.resolve().relative_to(cwd).as_posix()
    except ValueError:
        return path.as_posix()


def _excluded(display: str, patterns: list[str]) -> bool:
    for pat in patterns:
        p = pat.strip("/")
        if fnmatch.fnmatchcase(display, p) or display.startswith(f"{p}/"):
            return True
    return False


def discover(paths: list[str], excludes: list[str], cwd: Path | None = None) -> Discovery:
    base = (cwd or Path.cwd()).resolve()
    found: dict[Path, Target] = {}
    missing: list[str] = []
    unsupported: list[str] = []
    for arg in paths:
        p = Path(arg)
        if not p.is_absolute():
            p = base / p
        if p.is_file():
            kind = classify(p.name)
            display = _display(p, base)
            if _excluded(display, excludes):
                continue  # --exclude действует и на явно переданные файлы
            if kind is None:
                unsupported.append(display)
            else:
                found[p.resolve()] = Target(p, display, kind)
            continue
        if not p.is_dir():
            missing.append(arg)
            continue
        for root, dirs, files in os.walk(p):
            root_path = Path(root)
            dirs[:] = sorted(
                d
                for d in dirs
                if d not in SKIP_DIRS and not _excluded(_display(root_path / d, base), excludes)
            )
            for fname in sorted(files):
                kind = classify(fname)
                if kind is None:
                    continue
                fp = root_path / fname
                display = _display(fp, base)
                if _excluded(display, excludes):
                    continue
                found.setdefault(fp.resolve(), Target(fp, display, kind))
    targets = sorted(found.values(), key=lambda t: t.display)
    return Discovery(targets, missing, unsupported)
