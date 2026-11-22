"""CLI: antec-instance validate | render | init (ADR-0008).

Код выхода: 0 успех, 1 манифест или политики не прошли проверку, 2 ошибка использования.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from antec_instance import __version__
from antec_instance.agents import resolve_agents
from antec_instance.core import CoreError, core_version, default_core_dir
from antec_instance.manifest import Manifest, ManifestError, load_manifest
from antec_instance.policies import check_policies
from antec_instance.render import render
from antec_instance.template import init_instance

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="antec-instance",
        description="Манифест инсталляции antec.ai: проверка, рендер, init (ADR-0008).",
    )
    parser.add_argument("--version", action="version", version=f"antec-instance {__version__}")
    parser.add_argument(
        "--core",
        type=Path,
        default=None,
        help="каталог ядра (по умолчанию ядро, из которого запущен инструмент)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="проверить instance.yaml и policies/")
    validate.add_argument("instance", type=Path, help="каталог инсталляции")
    rend = sub.add_parser("render", help="проверить и записать производные файлы")
    rend.add_argument("instance", type=Path, help="каталог инсталляции")
    rend.add_argument("--out", type=Path, required=True, help="каталог для производных файлов")
    init = sub.add_parser("init", help="создать инсталляцию из шаблона ядра")
    init.add_argument("dest", type=Path, help="новый (пустой) каталог инсталляции")
    init.add_argument("--id", required=True, help="id инсталляции, он же scope политик")
    init.add_argument("--name", required=True, help="название организации")
    return parser


def _validate(core_dir: Path, instance_dir: Path) -> Manifest:
    manifest = load_manifest(instance_dir)
    problems: list[str] = []
    version = core_version(core_dir)
    if manifest.core.version != version:
        problems.append(
            f"core.version: манифест рассчитан на ядро {manifest.core.version}, "
            f"закреплено ядро {version}"
        )
    problems += check_policies(instance_dir, manifest.instance.id)
    try:
        resolve_agents(manifest, instance_dir)
    except ManifestError as exc:
        problems += exc.problems
    if problems:
        raise ManifestError(problems)
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    core_dir: Path = (args.core or default_core_dir()).resolve()
    try:
        if args.command == "init":
            init_instance(core_dir, args.dest, args.id, args.name)
            print(f"инсталляция {args.id} создана в {args.dest}")
            return EXIT_OK
        manifest = _validate(core_dir, args.instance)
        if args.command == "render":
            for path in render(manifest, args.instance, args.out):
                print(path)
        else:
            print(f"{args.instance}: инсталляция {manifest.instance.id} в порядке")
    except ManifestError as exc:
        for problem in exc.problems:
            print(f"ошибка: {problem}", file=sys.stderr)
        return EXIT_INVALID
    except CoreError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
