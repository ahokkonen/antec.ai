"""Образы ядра по digest из подписанного release.json (ADR-0014, п. 6).

`core-pin` скачивает `release.json` и его bundle из GitHub Release версии `core.version`,
проверяет keyless-подпись `cosign verify-blob` с identity workflow релиза ядра и пишет
`core-images.yaml` в корень инсталляции. `render` по нему создаёт override compose, который
заменяет локальную сборку образов ядра образами из GHCR. Без `core-images.yaml` (ядро,
`instances/dev`) образы собираются локально.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from antec_instance.manifest import Manifest, ManifestError

CORE_IMAGES_FILE = "core-images.yaml"
OVERRIDE_FILE = "compose.core-images.yaml"
CORE_REPOSITORY = "https://github.com/ahokkonen/antec.ai"
REGISTRY = "ghcr.io/ahokkonen"
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
RELEASE_JSON = "release.json"
RELEASE_BUNDLE = "release.json.sigstore.json"
_MAX_DOWNLOAD = 1024 * 1024

# Сервисы compose ядра на каждый образ ядра; сверяется тестом с deploy/compose/compose.yaml.
IMAGE_SERVICES: dict[str, tuple[str, ...]] = {
    "platform-api": ("platform-migrate", "platform-api", "platform-worker", "platform-subject"),
    "postgres": ("postgres",),
    "keycloak": ("keycloak",),
}

Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
Semver = Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+$")]


class CoreImagesError(Exception):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Loose(BaseModel):
    # release.json следующих версий может содержать новые поля: читаем только нужные.
    model_config = ConfigDict(extra="ignore", frozen=True)


class ImageRef(_Strict):
    repository: str
    tag: Semver
    digest: Digest

    @property
    def ref(self) -> str:
        return f"{self.repository}:{self.tag}@{self.digest}"


class SchemaVersions(_Loose):
    supported: list[int]
    deprecated: list[int] = []


class Release(_Loose):
    schema_version: Literal[1]
    core_version: Semver
    repository: str
    commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    images: dict[str, ImageRef]
    manifest_schema_versions: SchemaVersions
    mcp_protocol_versions: list[str]
    migration_head: str


class CoreImages(_Strict):
    schema_version: Literal[1]
    core_version: Semver
    commit: str
    release_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    images: dict[str, ImageRef]


def identity(version: str) -> str:
    return f"{CORE_REPOSITORY}/.github/workflows/release.yml@refs/tags/v{version}"


def release_url(version: str, name: str) -> str:
    return f"{CORE_REPOSITORY}/releases/download/v{version}/{name}"


def _download(url: str, dest: Path) -> None:
    if not url.startswith("https://"):
        raise CoreImagesError(f"{url}: только https")
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - только https выше
            data = resp.read(_MAX_DOWNLOAD + 1)
    except OSError as exc:
        raise CoreImagesError(f"не удалось скачать {url}: {exc}") from exc
    if len(data) > _MAX_DOWNLOAD:
        raise CoreImagesError(f"{url}: больше {_MAX_DOWNLOAD} байт")
    dest.write_bytes(data)


def verify_signature(release: Path, bundle: Path, version: str, cosign: str) -> None:
    exe = shutil.which(cosign)
    if exe is None:
        raise CoreImagesError(f"не найден {cosign}: нужен cosign из реестра версий (ADR-0014)")
    cmd = [
        exe,
        "verify-blob",
        "--bundle",
        str(bundle),
        "--certificate-identity",
        identity(version),
        "--certificate-oidc-issuer",
        OIDC_ISSUER,
        str(release),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise CoreImagesError(
            f"подпись release.json {version} не прошла проверку: "
            f"{detail[-1] if detail else 'код ' + str(result.returncode)}"
        )


def parse_release(data: bytes, manifest: Manifest) -> Release:
    try:
        release = Release.model_validate(json.loads(data))
    except (ValueError, ValidationError) as exc:
        raise CoreImagesError(f"release.json не прошёл проверку схемы: {exc}") from exc
    version = manifest.core.version
    problems = []
    if release.core_version != version:
        problems.append(f"release.json для ядра {release.core_version}, а core.version {version}")
    if release.repository != CORE_REPOSITORY:
        problems.append(f"release.json из {release.repository}, ожидается {CORE_REPOSITORY}")
    if set(release.images) != set(IMAGE_SERVICES):
        problems.append(f"образы release.json: {', '.join(sorted(release.images))}")
    for name, image in release.images.items():
        if image.repository != f"{REGISTRY}/antec-{name}" or image.tag != version:
            problems.append(f"образ {name}: {image.repository}:{image.tag} не из релиза {version}")
    if manifest.schema_version not in release.manifest_schema_versions.supported:
        problems.append(
            f"schema_version {manifest.schema_version} манифеста не поддерживается ядром "
            f"{version}: читайте раздел «Обновление» CHANGELOG"
        )
    problems += [
        f"MCP-сервер {s.name}: ревизия {s.protocol_version} не поддерживается ядром {version}"
        for s in manifest.mcp_servers
        if s.protocol_version not in release.mcp_protocol_versions
    ]
    if problems:
        raise CoreImagesError("; ".join(problems))
    return release


def pin(
    instance_dir: Path,
    manifest: Manifest,
    *,
    release_dir: Path | None = None,
    cosign: str = "cosign",
) -> Path:
    """Проверить подписанный release.json версии core.version и записать core-images.yaml."""
    version = manifest.core.version
    with tempfile.TemporaryDirectory(prefix="antec-core-pin-") as tmp:
        src = Path(tmp)
        if release_dir is None:
            for name in (RELEASE_JSON, RELEASE_BUNDLE):
                _download(release_url(version, name), src / name)
        else:
            for name in (RELEASE_JSON, RELEASE_BUNDLE):
                if not (release_dir / name).is_file():
                    raise CoreImagesError(f"нет {release_dir / name}")
                shutil.copyfile(release_dir / name, src / name)
        # Подпись до разбора: непроверенный JSON дальше не читается.
        verify_signature(src / RELEASE_JSON, src / RELEASE_BUNDLE, version, cosign)
        data = (src / RELEASE_JSON).read_bytes()
    release = parse_release(data, manifest)
    pinned = CoreImages(
        schema_version=1,
        core_version=release.core_version,
        commit=release.commit,
        release_sha256=hashlib.sha256(data).hexdigest(),
        images=release.images,
    )
    path = instance_dir / CORE_IMAGES_FILE
    header = (
        "# Создан antec-instance core-pin из подписанного release.json ядра (ADR-0014).\n"
        "# Не редактировать: обновление ядра = новый core.version и повторный core-pin.\n"
    )
    body = yaml.safe_dump(pinned.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
    # Атомарно: прерванный core-pin не оставляет обрезанный файл.
    fd, tmp = tempfile.mkstemp(prefix=f".{CORE_IMAGES_FILE}.", dir=instance_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(header + body)
        Path(tmp).chmod(0o644)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def load(instance_dir: Path) -> CoreImages | None:
    path = instance_dir / CORE_IMAGES_FILE
    if not path.exists():
        return None
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        return CoreImages.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ManifestError([f"{CORE_IMAGES_FILE}: {exc}"]) from exc


def check(pinned: CoreImages | None, manifest: Manifest) -> list[str]:
    if pinned is None:
        return []
    problems = []
    if pinned.core_version != manifest.core.version:
        problems.append(
            f"{CORE_IMAGES_FILE}: образы ядра {pinned.core_version}, а core.version "
            f"{manifest.core.version}: выполните antec-instance core-pin"
        )
    if set(pinned.images) != set(IMAGE_SERVICES):
        problems.append(f"{CORE_IMAGES_FILE}: образы {', '.join(sorted(pinned.images))}")
    return problems


def override(pinned: CoreImages) -> str:
    """Override compose: образ по digest, без локальной сборки. `!reset` из Compose spec."""
    lines = [
        "# Сгенерировано tools/instance render из core-images.yaml (ADR-0014). Не редактировать.",
        "services:",
    ]
    for name, services in IMAGE_SERVICES.items():
        ref = pinned.images[name].ref
        if not re.fullmatch(r"[a-z0-9./:@_-]+", ref):
            raise CoreImagesError(f"образ {name}: недопустимые символы в {ref!r}")
        for service in services:
            lines += [
                f"  {service}:",
                f"    image: {ref}",
                "    build: !reset null",
                "    pull_policy: missing",
            ]
    return "\n".join(lines) + "\n"


def summary(pinned: CoreImages | None) -> dict[str, str] | None:
    return None if pinned is None else {n: i.ref for n, i in pinned.images.items()}
