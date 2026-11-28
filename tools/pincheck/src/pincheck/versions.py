"""Строгий разбор версий реестра и классификация тегов образов."""

from __future__ import annotations

import enum
import functools
import re
from dataclasses import dataclass

from packaging.version import InvalidVersion
from packaging.version import Version as PepVersion

_STRICT_RE = re.compile(r"^(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*)){0,3}$")
_NUMERIC_PREFIX_RE = re.compile(r"^\d+(?:\.\d+)*")
_PRERELEASE_SUFFIX_RE = re.compile(
    r"^[-._+]?(?:rc|alpha|beta|a|b|c|dev|pre|preview|snapshot|nightly|canary|edge|test)"
    r"(?:[-._]?\d+)?(?:[-._+].*)?$",
    re.IGNORECASE,
)


class VersionError(ValueError):
    pass


@functools.total_ordering
@dataclass(frozen=True, slots=True)
class Version:
    """Версия из реестра: только числа через точку (1-4 компонента), без префиксов."""

    parts: tuple[int, ...]
    text: str

    @classmethod
    def parse(cls, text: str) -> Version:
        if not _STRICT_RE.fullmatch(text):
            raise VersionError(
                f"версия {text!r} должна состоять из чисел через точку без префиксов и суффиксов, "
                "например 1.108.7"
            )
        return cls(tuple(int(p) for p in text.split(".")), text)

    def _key(self) -> tuple[int, ...]:
        return self.parts + (0,) * (4 - len(self.parts))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __lt__(self, other: Version) -> bool:
        return self._key() < other._key()

    def __str__(self) -> str:
        return self.text


class TagKind(enum.Enum):
    RELEASE = "release"
    PRERELEASE = "prerelease"
    VARIANT = "variant"  # числовая версия с суффиксом варианта (-alpine, -cuda)
    NOT_VERSION = "not_version"  # stable, bookworm и т.п.


@dataclass(frozen=True, slots=True)
class TagInfo:
    kind: TagKind
    version: Version | None


def classify_tag_version(rest: str) -> TagInfo:
    """Классифицирует часть тега после префикса (например "1.108.7" или "0.13.4-rc1")."""
    m = _NUMERIC_PREFIX_RE.match(rest)
    if m is None:
        return TagInfo(TagKind.NOT_VERSION, None)
    suffix = rest[m.end() :]
    if not suffix:
        try:
            return TagInfo(TagKind.RELEASE, Version.parse(rest))
        except VersionError:
            return TagInfo(TagKind.VARIANT, None)
    if _PRERELEASE_SUFFIX_RE.fullmatch(suffix):
        return TagInfo(TagKind.PRERELEASE, None)
    return TagInfo(TagKind.VARIANT, None)


@dataclass(frozen=True, slots=True)
class PyVersion:
    """Версия Python-пакета с полным порядком PEP 440 (epoch, pre, post, dev, local)."""

    value: PepVersion
    text: str

    @classmethod
    def parse(cls, text: str) -> PyVersion:
        try:
            return cls(PepVersion(text.strip()), text)
        except InvalidVersion as exc:
            raise VersionError(f"не удалось разобрать версию Python-пакета {text!r}") from exc

    def is_below(self, minimum: PyVersion) -> bool:
        return self.value < minimum.value

    @property
    def is_prerelease(self) -> bool:
        """a, b, rc и dev по PEP 440 (ADR-0016: пре-релизы запрещены всегда)."""
        return self.value.is_prerelease


def canonical_py_version(text: str) -> str:
    """Нормализованная форма PEP 440 для точного сравнения: регистр, разделители, хвостовые нули
    (1.0 и 1.0.0 совпадают, как в PEP 440)."""
    v = PyVersion.parse(text).value
    normalized = str(v)
    epoch = f"{v.epoch}!" if v.epoch else ""
    base = ".".join(str(p) for p in v.release)
    suffix = normalized[len(epoch) + len(base) :]
    release = v.release
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]
    return f"{epoch}{'.'.join(str(p) for p in release)}{suffix}"
