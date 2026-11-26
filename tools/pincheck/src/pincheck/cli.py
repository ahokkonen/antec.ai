"""CLI: pincheck --policy policy/allowed-versions.yaml [пути...]."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from pincheck import __version__
from pincheck.checks import CheckContext, CoreImage
from pincheck.checks.compose import check_compose
from pincheck.checks.dockerfile import check_dockerfile
from pincheck.checks.uvlock import check_uvlock
from pincheck.coreimages import CoreImagesError, load_core_images
from pincheck.discover import FileKind, discover
from pincheck.policy import load_policy
from pincheck.report import Code, Finding, Severity, error, write_github, write_text

EXIT_OK = 0
EXIT_VIOLATIONS = 1

_CHECKS: dict[FileKind, Callable[[str, str, CheckContext], list[Finding]]] = {
    FileKind.COMPOSE: check_compose,
    FileKind.DOCKERFILE: check_dockerfile,
    FileKind.UV_LOCK: check_uvlock,
}


def _parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"ожидается дата YYYY-MM-DD, получено {value!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pincheck",
        description=(
            "Проверяет реестр версий и сверяет с ним docker-compose, Dockerfile и uv.lock "
            "(ADR-0004, ADR-0016). Код выхода 0: нарушений нет, 1: есть ошибки."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="файлы или каталоги для проверки (по умолчанию текущий каталог)",
    )
    parser.add_argument("--policy", required=True, help="путь к policy/allowed-versions.yaml")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="исключить путь (относительно текущего каталога); можно повторять",
    )
    parser.add_argument(
        "--require-digest",
        action="store_true",
        help="digest: null в реестре для используемого образа считать ошибкой "
        "(prod, репозитории инсталляций)",
    )
    parser.add_argument(
        "--core-images",
        default=None,
        metavar="PATH",
        help="core-images.yaml инсталляции: образы ядра antec.ai разрешены только с этими "
        "тегом и digest (ADR-0014)",
    )
    parser.add_argument(
        "--format",
        choices=["auto", "text", "github"],
        default="auto",
        help="формат вывода; auto = github внутри GitHub Actions, иначе text",
    )
    parser.add_argument(
        "--today",
        type=_parse_date,
        default=None,
        help="текущая дата для проверок карантина (YYYY-MM-DD, по умолчанию UTC)",
    )
    parser.add_argument("--version", action="version", version=f"pincheck {__version__}")
    return parser


def _write(findings: list[Finding], fmt: str, out: TextIO) -> None:
    if fmt == "auto":
        fmt = "github" if os.environ.get("GITHUB_ACTIONS") == "true" else "text"
    if fmt == "github":
        write_github(findings, out)
    else:
        write_text(findings, out)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def run(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    stream = out if out is not None else sys.stdout
    args = build_parser().parse_args(argv)
    today: dt.date = args.today or dt.datetime.now(dt.UTC).date()
    policy_path = Path(args.policy)
    policy_display = args.policy

    try:
        policy_text = _read(policy_path)
    except (OSError, UnicodeDecodeError) as exc:
        _write(
            [
                error(
                    Code.FILE_INPUT,
                    policy_display,
                    None,
                    f"не удалось прочитать реестр: {exc}",
                    "укажите путь к policy/allowed-versions.yaml в --policy",
                )
            ],
            args.format,
            stream,
        )
        return EXIT_VIOLATIONS

    loaded = load_policy(policy_display, policy_text, today)
    if loaded.policy is None or loaded.index is None:
        _write(loaded.findings, args.format, stream)
        stream.write(
            f"Реестр {policy_display} невалиден: ошибок {len(loaded.findings)}. "
            "Файлы не проверялись.\n"
        )
        return EXIT_VIOLATIONS

    core_images: dict[str, CoreImage] = {}
    if args.core_images is not None:
        try:
            core_images = load_core_images(args.core_images, _read(Path(args.core_images)))
        except (OSError, UnicodeDecodeError, CoreImagesError) as exc:
            _write(
                [
                    error(
                        Code.FILE_INPUT,
                        args.core_images,
                        None,
                        f"core-images.yaml не прочитан: {exc}",
                        "создайте его antec-instance core-pin (ADR-0014)",
                    )
                ],
                args.format,
                stream,
            )
            return EXIT_VIOLATIONS
    ctx = CheckContext(
        loaded.policy, loaded.index, policy_display, args.require_digest, core_images
    )
    found = discover(list(args.paths), list(args.exclude))
    findings: list[Finding] = []
    for repository, core in core_images.items():
        hit = loaded.index.denied_match(core.digest)
        if hit is not None:
            findings.append(
                error(
                    Code.IMAGE_DENIED_DIGEST,
                    core.source,
                    None,
                    f"digest образа ядра {repository} в denylist компонента {hit.component!r}",
                    "не разворачивайте этот релиз ядра; действуйте по ADR-0004, п. 10",
                )
            )
    for missing in found.missing:
        findings.append(
            error(
                Code.FILE_INPUT,
                missing,
                None,
                "путь не существует",
                "проверьте аргументы командной строки",
            )
        )
    for unsupported in found.unsupported:
        findings.append(
            error(
                Code.FILE_INPUT,
                unsupported,
                None,
                "неизвестный тип файла (ожидаются compose*.y*ml, "
                "docker-compose*.y*ml, Dockerfile, uv.lock)",
                "передайте каталог или файл поддерживаемого типа",
            )
        )
    counts: Counter[FileKind] = Counter()
    for target in found.targets:
        counts[target.kind] += 1
        try:
            text = _read(target.path)
        except (OSError, UnicodeDecodeError) as exc:
            findings.append(
                error(
                    Code.FILE_INPUT,
                    target.display,
                    None,
                    f"не удалось прочитать файл: {exc}",
                    "проверьте файл",
                )
            )
            continue
        if target.kind is FileKind.COMPOSE:
            findings.extend(check_compose(target.display, text, ctx, source=target.path))
        else:
            findings.extend(_CHECKS[target.kind](target.display, text, ctx))

    findings = list(
        dict.fromkeys(findings)
    )  # Dockerfile может проверяться дважды: сам и через build
    _write(findings, args.format, stream)
    errors = sum(1 for f in findings if f.severity is Severity.ERROR)
    warnings = len(findings) - errors
    if not found.targets and not findings:
        stream.write(
            f"Реестр {policy_display} валиден. Проверять нечего: compose-файлы, "
            "Dockerfile и uv.lock не найдены.\n"
        )
        return EXIT_OK
    summary = ", ".join(f"{kind.value}: {counts[kind]}" for kind in FileKind)
    stream.write(
        f"Реестр {policy_display} валиден. Проверено файлов: {summary}. "
        f"Ошибок: {errors}, предупреждений: {warnings}.\n"
    )
    return EXIT_VIOLATIONS if errors else EXIT_OK


def main() -> None:
    sys.exit(run())
