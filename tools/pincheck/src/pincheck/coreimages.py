"""core-images.yaml инсталляции: образы ядра antec.ai по digest (ADR-0014, п. 6).

Файл пишет `antec-instance core-pin` после проверки подписи release.json; pincheck только
сверяет с ним ссылки на образы ядра в compose-файлах.
"""

from __future__ import annotations

import re

from pincheck.checks import CoreImage
from pincheck.imageref import DIGEST_RE, ImageRefError, normalize_registry_image
from pincheck.yamlload import YamlLoadError, load_yaml

_TAG_RE = re.compile(r"\d+\.\d+\.\d+")


class CoreImagesError(Exception):
    pass


def load_core_images(path: str, text: str) -> dict[str, CoreImage]:
    try:
        data = load_yaml(text).data
    except YamlLoadError as exc:
        raise CoreImagesError(exc.message) from exc
    images = data.get("images") if isinstance(data, dict) else None
    if not isinstance(images, dict) or not images:
        raise CoreImagesError("нет словаря images")
    out: dict[str, CoreImage] = {}
    for name, spec in images.items():
        if not isinstance(spec, dict):
            raise CoreImagesError(f"images.{name}: ожидается словарь")
        repository, tag, digest = spec.get("repository"), spec.get("tag"), spec.get("digest")
        if not isinstance(repository, str) or not isinstance(tag, str):
            raise CoreImagesError(f"images.{name}: нужны repository и tag")
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            raise CoreImagesError(f"images.{name}: digest должен быть sha256:<64 hex>")
        if not _TAG_RE.fullmatch(tag):
            raise CoreImagesError(f"images.{name}: тег {tag!r} не X.Y.Z")
        try:
            key = normalize_registry_image(repository)
        except ImageRefError as exc:
            raise CoreImagesError(f"images.{name}: {exc}") from exc
        out[key] = CoreImage(tag, digest, path)
    return out
