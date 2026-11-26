"""Образы ядра и `release.json` (ADR-0014, пп. 5, 6).

`release.json` публикуется в GitHub Release вместе с keyless-подписью. Инсталляция берёт из него
digest образов (`antec-instance core-pin`), а не из коммита: digest нельзя записать в коммит,
из которого образ собран.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REGISTRY = "ghcr.io/ahokkonen"
REPOSITORY_URL = "https://github.com/ahokkonen/antec.ai"
RELEASE_SCHEMA_VERSION = 1
PLATFORMS = ("linux/amd64", "linux/arm64")
MIGRATIONS_DIR = "services/platform-api/src/antec_platform/migrations/versions"
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_REVISION_RE = re.compile(r'^revision: str = "(?P<rev>[^"]+)"$', re.M)
_DOWN_RE = re.compile(r'^down_revision: str \| None = (?:"(?P<rev>[^"]+)"|None)$', re.M)


class ManifestError(Exception):
    pass


@dataclass(frozen=True)
class Image:
    """Собственный образ ядра: имя, каталог сборки, локальное имя на стенде."""

    name: str
    context: str
    local: str

    @property
    def repository(self) -> str:
        return f"{REGISTRY}/antec-{self.name}"


IMAGES: tuple[Image, ...] = (
    Image("platform-api", "services/platform-api", "localhost/antec/platform-api"),
    Image("postgres", "deploy/compose/images/postgres", "localhost/antec/postgres"),
    Image("keycloak", "deploy/compose/images/keycloak", "localhost/antec/keycloak"),
)


def images_matrix() -> list[dict[str, str]]:
    """Матрица сборки для release.yml: образ x платформа."""
    return [
        {
            "name": i.name,
            "context": i.context,
            "repository": i.repository,
            "platform": p,
            "runner": "ubuntu-24.04-arm" if p == "linux/arm64" else "ubuntu-24.04",
        }
        for i in IMAGES
        for p in PLATFORMS
    ]


def migration_head(root: Path) -> str:
    """Последняя ревизия цепочки миграций Platform API (одна голова, без ветвлений)."""
    revisions: dict[str, str | None] = {}
    for path in sorted((root / MIGRATIONS_DIR).glob("*.py")):
        text = path.read_text(encoding="utf-8")
        rev, down = _REVISION_RE.search(text), _DOWN_RE.search(text)
        if rev is None or down is None:
            raise ManifestError(f"{path}: нет revision или down_revision")
        revisions[rev["rev"]] = down["rev"]
    if not revisions:
        raise ManifestError(f"{root / MIGRATIONS_DIR}: миграций нет")
    parents = {d for d in revisions.values() if d is not None}
    heads = sorted(set(revisions) - parents)
    if len(heads) != 1:
        raise ManifestError(f"ожидается одна голова миграций, найдено: {', '.join(heads)}")
    return heads[0]


def _object(value: Any, what: str) -> dict[str, Any]:  # noqa: ANN401 - сырой JSON
    if not isinstance(value, dict):
        raise ManifestError(f"{what}: ожидается объект JSON")
    return value


def build(
    *,
    version: str,
    commit: str,
    digests: dict[str, Any],
    compat: dict[str, Any],
    root: Path,
) -> dict[str, Any]:
    if not _COMMIT_RE.fullmatch(commit):
        raise ManifestError(f"commit: ожидается полный SHA, получено {commit!r}")
    expected = {i.name for i in IMAGES}
    if set(digests) != expected:
        raise ManifestError(
            f"digest образов: ожидаются {', '.join(sorted(expected))}, "
            f"получены {', '.join(sorted(digests)) or 'ничего'}"
        )
    images = {}
    for image in IMAGES:
        digest = digests[image.name]
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise ManifestError(f"{image.name}: digest {digest!r} не sha256:<64 hex>")
        images[image.name] = {"repository": image.repository, "tag": version, "digest": digest}
    if compat.get("core_version") != version:
        raise ManifestError(
            f"core-info: core_version {compat.get('core_version')!r}, а выпускается {version}"
        )
    schemas = _object(compat.get("manifest_schema_versions"), "core-info.manifest_schema_versions")
    mcp = compat.get("mcp_protocol_versions")
    if not isinstance(mcp, list) or not mcp:
        raise ManifestError("core-info.mcp_protocol_versions: ожидается непустой список")
    return {
        "schema_version": RELEASE_SCHEMA_VERSION,
        "core_version": version,
        "repository": REPOSITORY_URL,
        "commit": commit,
        "images": images,
        "manifest_schema_versions": schemas,
        "mcp_protocol_versions": mcp,
        "migration_head": migration_head(root),
    }


def load_json(path: Path) -> dict[str, Any]:
    try:
        return _object(json.loads(path.read_text(encoding="utf-8")), str(path))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{path}: {exc}") from exc
