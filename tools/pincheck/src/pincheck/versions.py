"""Строгий разбор версий реестра и классификация тегов образов."""

from __future__ import annotations

import enum
import functools
import re
from dataclasses import dataclass

_STRICT_RE = re.compile(r"^(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*)){0,3}$")
_NUMERIC_PREFIX_RE = re.compile(r"^\d+(?:\.\d+)*")
_PRERELEASE_SUFFIX_RE = re.compile(
    r"^[-._+]?(?:rc|alpha|beta|a|b|c|dev|pre|preview|snapshot|nightly|canary|edge|test)"
    r"(?:[-._]?\d+)?(?:[-._+].*)?$",
    re.IGNORECASE,
)
# PEP 440: release-сегмент и признак пре-/dev-релиза.
_PEP440_RE = re.compile(
    r"^v?(?:(?P<epoch>\d+)!)?(?P<release>\d+(?:\.\d+)*)(?P<rest>.*)$", re.IGNORECASE
)
_PEP440_PRE_RE = re.compile(r"^[-._]?(?:a|b|c|rc|alpha|beta|pre|preview|dev)", re.IGNORECASE)


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
    """Минимальное представление версии PEP 440 для сравнения с минимальной версией."""

    epoch: int
    release: tuple[int, ...]
    is_pre: bool
    suffix: str
    text: str

    @classmethod
    def parse(cls, text: str) -> PyVersion:
        m = _PEP440_RE.fullmatch(text.strip())
        if m is None:
            raise VersionError(f"не удалось разобрать версию Python-пакета {text!r}")
        rest = m.group("rest")
        return cls(
            epoch=int(m.group("epoch") or 0),
            release=tuple(int(p) for p in m.group("release").split(".")),
            is_pre=bool(rest) and _PEP440_PRE_RE.match(rest) is not None,
            suffix=rest.lower(),
            text=text,
        )

    def release_key(self) -> tuple[int, ...]:
        r = self.release
        while len(r) > 1 and r[-1] == 0:
            r = r[:-1]
        return r

    def is_below(self, minimum: PyVersion) -> bool:
        """True, если версия ниже минимальной. Пре-релиз той же версии считается ниже."""
        width = max(len(self.release), len(minimum.release))
        mine = (self.epoch, *self.release, *(0,) * (width - len(self.release)))
        theirs = (minimum.epoch, *minimum.release, *(0,) * (width - len(minimum.release)))
        if mine != theirs:
            return mine < theirs
        return self.is_pre and not minimum.is_pre


def canonical_py_version(text: str) -> str:
    """Нормализация для точного сравнения с denylist: регистр, префикс v, хвостовые нули."""
    v = PyVersion.parse(text)
    release = ".".join(str(p) for p in v.release_key())
    epoch = f"{v.epoch}!" if v.epoch else ""
    return f"{epoch}{release}{v.suffix}"
