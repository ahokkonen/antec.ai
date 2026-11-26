"""CLI: antec-release check | bump | notes | images | manifest (ADR-0014).

Код выхода: 0 успех, 1 проверка не прошла, 2 ошибка использования.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from antec_release import __version__, changelog, manifest
from antec_release.versions import (
    VersionError,
    check_locations,
    parse_semver,
    read_version,
    rewrite_locations,
)

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_USAGE = 2


def default_root() -> Path:
    """Корень ядра, из которого запущен инструмент (tools/release/src/antec_release -> корень)."""
    return Path(__file__).resolve().parents[4]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="antec-release", description="Релизы ядра antec.ai (ADR-0014)."
    )
    parser.add_argument("--version", action="version", version=f"antec-release {__version__}")
    parser.add_argument("--root", type=Path, default=None, help="корень ядра")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="версия везде = VERSION, структура CHANGELOG")
    check.add_argument("--tag", help="тег релиза vX.Y.Z: проверить его и раздел CHANGELOG")
    bump = sub.add_parser("bump", help="новая версия везде и раздел CHANGELOG")
    bump.add_argument("new", help="версия X.Y.Z")
    bump.add_argument("--date", type=dt.date.fromisoformat, default=None, help="дата раздела")
    notes = sub.add_parser("notes", help="раздел CHANGELOG версии (текст GitHub Release)")
    notes.add_argument("release", help="версия X.Y.Z")
    sub.add_parser("images", help="матрица сборки образов (JSON)")
    man = sub.add_parser("manifest", help="собрать release.json")
    man.add_argument("--commit", required=True, help="полный SHA коммита тега")
    man.add_argument("--digests", type=Path, required=True, help="JSON {образ: digest индекса}")
    man.add_argument("--compat", type=Path, required=True, help="вывод antec-instance core-info")
    man.add_argument("--out", type=Path, required=True)
    return parser


def _check(root: Path, tag: str | None) -> list[str]:
    version = read_version(root)
    problems = check_locations(root, version)
    if tag is not None and tag != f"v{version}":
        problems.append(f"тег {tag}, а VERSION {version}: ожидается v{version}")
    problems += changelog.check(changelog.load(root), version, release=tag is not None)
    return problems


def _bump(root: Path, new: str, today: dt.date) -> list[str]:
    current = read_version(root)
    log = changelog.load(root)
    # Первый релиз выпускает версию, уже записанную в VERSION (0.1.0).
    first_release = new == current and log.get(current) is None
    if parse_semver(new) <= parse_semver(current) and not first_release:
        raise VersionError(f"новая версия {new} должна быть больше текущей {current}")
    if problems := check_locations(root, current):
        raise VersionError("места версии расходятся, сначала check: " + "; ".join(problems))
    text = changelog.release(log, new, today)
    changed = rewrite_locations(root, new)
    (root / changelog.CHANGELOG_FILE).write_text(text, encoding="utf-8")
    return [*changed, changelog.CHANGELOG_FILE]


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root: Path = (args.root or default_root()).resolve()
    try:
        if args.command == "check":
            problems = _check(root, args.tag)
            for problem in problems:
                print(f"ошибка: {problem}", file=sys.stderr)
            if problems:
                return EXIT_INVALID
            print(f"версия {read_version(root)} согласована")
        elif args.command == "bump":
            for path in _bump(root, args.new, args.date or dt.datetime.now(dt.UTC).date()):
                print(path)
        elif args.command == "notes":
            parse_semver(args.release)
            sys.stdout.write(changelog.notes(changelog.load(root), args.release))
        elif args.command == "images":
            print(json.dumps(manifest.images_matrix()))
        else:
            data = manifest.build(
                version=read_version(root),
                commit=args.commit,
                digests=manifest.load_json(args.digests),
                compat=manifest.load_json(args.compat),
                root=root,
            )
            args.out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            print(args.out)
    except (VersionError, changelog.ChangelogError) as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except manifest.ManifestError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
