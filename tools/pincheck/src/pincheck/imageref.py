"""Разбор и нормализация ссылок на образы контейнеров (name[:tag][@sha256:digest])."""

from __future__ import annotations

import re
from dataclasses import dataclass

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DOMAIN_COMPONENT = r"(?:[a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9-]*[a-zA-Z0-9])"
_DOMAIN_RE = re.compile(rf"^{_DOMAIN_COMPONENT}(?:\.{_DOMAIN_COMPONENT})*(?::\d+)?$")
_PATH_COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$")
_TAG_RE = re.compile(r"^\w[\w.-]{0,127}$")

DEFAULT_DOMAIN = "docker.io"
_DOCKER_DOMAIN_ALIASES = frozenset({"docker.io", "index.docker.io", "registry-1.docker.io"})


class ImageRefError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ImageRef:
    raw: str
    repository: str  # нормализованный: домен/путь, например docker.io/library/postgres
    tag: str | None
    digest: str | None


def _normalize_repository(name: str) -> str:
    first, sep, remainder = name.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        domain, path = first.lower(), remainder
    else:
        domain, path = DEFAULT_DOMAIN, name
    if not _DOMAIN_RE.fullmatch(domain):
        raise ImageRefError(f"некорректный адрес реестра {domain!r}")
    if domain in _DOCKER_DOMAIN_ALIASES:
        domain = DEFAULT_DOMAIN
        if "/" not in path:
            path = f"library/{path}"
    for component in path.split("/"):
        if not _PATH_COMPONENT_RE.fullmatch(component):
            raise ImageRefError(
                f"некорректный компонент имени образа {component!r} "
                "(допустимы строчные буквы, цифры, '.', '_', '-')"
            )
    return f"{domain}/{path}"


def parse_image_ref(raw: str) -> ImageRef:
    """Разбирает ссылку на образ. Переменные (${...}) должны быть отсеяны вызывающим кодом."""
    text = raw.strip()
    if not text:
        raise ImageRefError("пустая ссылка на образ")
    name_tag, at, digest = text.partition("@")
    digest_value: str | None = None
    if at:
        if not DIGEST_RE.fullmatch(digest):
            raise ImageRefError(
                f"digest {digest!r} должен иметь вид sha256:<64 шестнадцатеричных символа>"
            )
        digest_value = digest
    tag: str | None = None
    last_slash = name_tag.rfind("/")
    colon = name_tag.rfind(":")
    if colon > last_slash:
        name, tag = name_tag[:colon], name_tag[colon + 1 :]
        if not _TAG_RE.fullmatch(tag):
            raise ImageRefError(f"некорректный тег {tag!r}")
    else:
        name = name_tag
    if not name:
        raise ImageRefError("не указано имя образа")
    return ImageRef(raw=raw, repository=_normalize_repository(name), tag=tag, digest=digest_value)


def normalize_registry_image(value: str) -> str:
    """Нормализует поле image реестра: только репозиторий, без тега и digest."""
    ref = parse_image_ref(value)
    if ref.tag is not None or ref.digest is not None:
        raise ImageRefError(
            f"поле image должно содержать только репозиторий без тега и digest, получено {value!r}"
        )
    return ref.repository
