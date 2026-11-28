"""CLI: antec-instance validate | render | init | core-info | core-pin (ADR-0008, ADR-0014).

Код выхода: 0 успех, 1 манифест или политики не прошли проверку, 2 ошибка использования.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from antec_instance import __version__, coreimages
from antec_instance.agents import resolve_agents
from antec_instance.compliance import ProvidersError, load_providers
from antec_instance.core import CoreError, core_version, default_core_dir
from antec_instance.manifest import (
    CORE_MCP_PROTOCOL_VERSIONS,
    DEPRECATED_SCHEMA_REMOVAL,
    DEPRECATED_SCHEMA_VERSIONS,
    SCHEMA_VERSIONS,
    Manifest,
    ManifestError,
    load_manifest,
)
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
    sub.add_parser("core-info", help="совместимость ядра для release.json (JSON, ADR-0014)")
    pin = sub.add_parser(
        "core-pin", help="образы ядра по digest из подписанного release.json (ADR-0014)"
    )
    pin.add_argument("instance", type=Path, help="каталог инсталляции")
    pin.add_argument(
        "--release-dir",
        type=Path,
        default=None,
        help="каталог с release.json и release.json.sigstore.json (по умолчанию скачать "
        "из GitHub Release версии core.version)",
    )
    pin.add_argument("--cosign", default="cosign", help="путь к cosign (по умолчанию из PATH)")
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
    # Ревизии MCP-клиента ядра (ADR-0010, п. 6).
    problems += [
        f"MCP-сервер {s.name}: protocol_version {s.protocol_version} не поддерживается "
        f"MCP-клиентом ядра {version} (только {', '.join(CORE_MCP_PROTOCOL_VERSIONS)})"
        for s in manifest.mcp_servers
        if s.protocol_version not in CORE_MCP_PROTOCOL_VERSIONS
    ]
    problems += check_policies(instance_dir, manifest.instance.id)
    problems += coreimages.check(coreimages.load(instance_dir), manifest)
    try:
        resolve_agents(manifest, instance_dir)
    except ManifestError as exc:
        problems += exc.problems
    if problems:
        raise ManifestError(problems)
    return manifest


def core_info(core_dir: Path) -> dict[str, object]:
    """Что ядро принимает от инсталляции: попадает в release.json (ADR-0014, п. 5)."""
    return {
        "core_version": core_version(core_dir),
        "manifest_schema_versions": {
            "supported": list(SCHEMA_VERSIONS),
            "deprecated": list(DEPRECATED_SCHEMA_VERSIONS),
        },
        "mcp_protocol_versions": list(CORE_MCP_PROTOCOL_VERSIONS),
    }


def _deprecation(manifest: Manifest) -> list[str]:
    if manifest.schema_version not in DEPRECATED_SCHEMA_VERSIONS:
        return []
    return [
        f"schema_version: {manifest.schema_version} устарела и будет удалена не раньше ядра "
        f"{DEPRECATED_SCHEMA_REMOVAL}; перейдите на {SCHEMA_VERSIONS[-1]} (ADR-0014, п. 3)"
    ]


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    core_dir: Path = (args.core or default_core_dir()).resolve()
    try:
        if args.command == "init":
            init_instance(core_dir, args.dest, args.id, args.name)
            print(f"инсталляция {args.id} создана в {args.dest}")
            return EXIT_OK
        if args.command == "core-info":
            print(json.dumps(core_info(core_dir), indent=2))
            return EXIT_OK
        if args.command == "core-pin":
            manifest = load_manifest(args.instance)
            path = coreimages.pin(
                args.instance, manifest, release_dir=args.release_dir, cosign=args.cosign
            )
            print(f"{path}: образы ядра {manifest.core.version} по digest")
            return EXIT_OK
        manifest = _validate(core_dir, args.instance)
        providers = load_providers(core_dir)
        notes = manifest.compliance_warnings(providers)
        for warning in [*_deprecation(manifest), *manifest.class_warnings(), *notes]:
            print(f"предупреждение: {warning}", file=sys.stderr)
        if args.command == "render":
            for path in render(manifest, args.instance, args.out, providers, notes, core_dir):
                print(path)
        else:
            print(f"{args.instance}: инсталляция {manifest.instance.id} в порядке")
    except ManifestError as exc:
        for problem in exc.problems:
            print(f"ошибка: {problem}", file=sys.stderr)
        return EXIT_INVALID
    except coreimages.CoreImagesError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except (CoreError, ProvidersError) as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
