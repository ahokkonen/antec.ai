"""Схема реестра policy/allowed-versions.yaml и его семантическая проверка."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from pincheck.imageref import DIGEST_RE, ImageRefError, normalize_registry_image
from pincheck.report import Code, Finding, error
from pincheck.versions import (
    PyVersion,
    TagKind,
    Version,
    VersionError,
    canonical_py_version,
    classify_tag_version,
)
from pincheck.yamlload import LoadedYaml, YamlLoadError, load_yaml

_PY_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_URL_RE = re.compile(r"^https://[^\s/]+(?:/\S*)?$")
# Минимальная длина префикса digest: 12 hex (48 бит), как в коротких ID Docker.
MIN_DIGEST_PREFIX_HEX = 12
_DIGEST_PREFIX_RE = re.compile(r"^sha256:(?P<hex>[0-9a-f]*)$")


def canonical_py_name(name: str) -> str:
    """Нормализация имени пакета по PEP 503."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _check_version(value: str) -> str:
    try:
        Version.parse(value)
    except VersionError as exc:
        raise ValueError(str(exc)) from exc
    return value


def _check_digest(value: str) -> str:
    if not DIGEST_RE.fullmatch(value):
        raise ValueError("digest должен иметь вид sha256:<64 строчных шестнадцатеричных символа>")
    return value


def _check_digest_prefix(value: str) -> str:
    m = _DIGEST_PREFIX_RE.fullmatch(value)
    if m is None:
        raise ValueError(
            "префикс digest должен иметь вид sha256:<строчные шестнадцатеричные символы> "
            "без многоточия"
        )
    n = len(m.group("hex"))
    if n < MIN_DIGEST_PREFIX_HEX:
        raise ValueError(
            f"префикс digest слишком короткий: {n} hex-символов, минимум {MIN_DIGEST_PREFIX_HEX}"
        )
    if n >= 64:
        raise ValueError("полный digest указывается в image_digests, а не в image_digest_prefixes")
    return value


def _check_url(value: str) -> str:
    if not _URL_RE.fullmatch(value):
        raise ValueError(f"источник должен быть https-ссылкой, получено {value!r}")
    return value


def _check_image(value: str) -> str:
    try:
        normalize_registry_image(value)
    except ImageRefError as exc:
        raise ValueError(str(exc)) from exc
    return value


def _check_py_name(value: str) -> str:
    if not _PY_NAME_RE.fullmatch(value):
        raise ValueError(f"некорректное имя Python-пакета {value!r}")
    return value


def _check_py_version(value: str) -> str:
    try:
        PyVersion.parse(value)
    except VersionError as exc:
        raise ValueError(str(exc)) from exc
    return value


VersionStr = Annotated[StrictStr, AfterValidator(_check_version)]
DigestStr = Annotated[StrictStr, AfterValidator(_check_digest)]
DigestPrefixStr = Annotated[StrictStr, AfterValidator(_check_digest_prefix)]
UrlStr = Annotated[StrictStr, AfterValidator(_check_url)]
ImageStr = Annotated[StrictStr, AfterValidator(_check_image)]
PyNameStr = Annotated[StrictStr, AfterValidator(_check_py_name)]
PyVersionStr = Annotated[StrictStr, AfterValidator(_check_py_version)]
NonEmptyStr = Annotated[StrictStr, Field(min_length=1)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Defaults(_Model):
    floating_tags_forbidden: list[NonEmptyStr]
    prerelease_forbidden: Literal[True]


class Candidate(_Model):
    version: VersionStr
    not_before: dt.date


class Signature(_Model):
    method: NonEmptyStr
    note: NonEmptyStr | None = None
    howto: NonEmptyStr | None = None


Confidence = Literal["multi-source", "single-source"]


class _DigestEvidence(_Model):
    """Происхождение записи denylist: откуда взят digest и насколько он подтверждён."""

    source: UrlStr
    confidence: Confidence
    corroborated_by: list[UrlStr] = Field(default_factory=list)
    image: ImageStr | None = None  # к какому образу относится (может отличаться от компонента)
    note: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _confidence_matches_sources(self) -> _DigestEvidence:
        if self.confidence == "multi-source" and not self.corroborated_by:
            raise ValueError(
                "confidence: multi-source требует хотя бы одну ссылку в corroborated_by"
            )
        if self.confidence == "single-source" and self.corroborated_by:
            raise ValueError(
                "есть corroborated_by: укажите confidence: multi-source вместо single-source"
            )
        return self


class DeniedDigest(_DigestEvidence):
    digest: DigestStr


class DeniedDigestPrefix(_DigestEvidence):
    prefix: DigestPrefixStr


class Denied(_Model):
    pypi: list[PyVersionStr] = Field(default_factory=list)
    image_tags: list[NonEmptyStr] = Field(default_factory=list)
    image_digests: list[DeniedDigest] = Field(default_factory=list)
    image_digest_prefixes: list[DeniedDigestPrefix] = Field(default_factory=list)
    note: NonEmptyStr | None = None


class Advisory(_Model):
    id: NonEmptyStr
    summary: NonEmptyStr


class Component(_Model):
    role: NonEmptyStr
    license: NonEmptyStr
    image: ImageStr | None = None
    note: NonEmptyStr | None = None
    allowed: list[VersionStr]
    image_tag_prefix: StrictStr = ""
    image_tag_suffix: StrictStr = ""
    candidate: Candidate | None = None
    min_version: VersionStr | None = None
    digest: DigestStr | None = None
    signature: Signature | None = None
    denied: Denied = Field(default_factory=Denied)
    python_package_policy: NonEmptyStr | None = None
    advisories: list[Advisory] = Field(default_factory=list)
    sources: list[NonEmptyStr] = Field(default_factory=list)

    @property
    def repository(self) -> str | None:
        return normalize_registry_image(self.image) if self.image is not None else None

    def tag_for(self, version: str) -> str:
        """Тег образа для версии: префикс + версия + суффикс варианта."""
        return f"{self.image_tag_prefix}{version}{self.image_tag_suffix}"


class Policy(_Model):
    schema_version: Literal[1]
    reviewed_at: dt.date
    quarantine_days: Annotated[StrictInt, Field(ge=0)]
    defaults: Defaults
    components: dict[NonEmptyStr, Component]
    python_forbidden: list[PyNameStr] = Field(default_factory=list)
    python_denylist: dict[PyNameStr, list[PyVersionStr]] = Field(default_factory=dict)
    python_min_versions: dict[PyNameStr, PyVersionStr] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ComponentIndex:
    """Индексы реестра для быстрой проверки образов."""

    by_repository: dict[str, tuple[str, Component]]
    denied_digests: dict[str, tuple[str, DeniedDigest]]  # digest -> (компонент, запись)
    denied_prefixes: list[tuple[str, DeniedDigestPrefix]]  # (компонент, запись)
    floating_tags: frozenset[str]

    def denied_match(self, digest: str) -> DenyMatch | None:
        """Совпадение digest с denylist: точное или по префиксу. Проверяется для любого образа."""
        exact = self.denied_digests.get(digest)
        if exact is not None:
            return DenyMatch(exact[0], exact[1].digest, exact[1].confidence, by_prefix=False)
        for comp_name, entry in self.denied_prefixes:
            if digest.startswith(entry.prefix):
                return DenyMatch(comp_name, entry.prefix, entry.confidence, by_prefix=True)
        return None


@dataclass(frozen=True, slots=True)
class DenyMatch:
    component: str
    value: str  # digest или префикс из реестра
    confidence: str
    by_prefix: bool


@dataclass(slots=True)
class LoadedPolicy:
    path: str
    policy: Policy | None
    findings: list[Finding] = field(default_factory=list)
    index: ComponentIndex | None = None

    @property
    def ok(self) -> bool:
        return self.policy is not None and not self.findings


def _pydantic_findings(exc: ValidationError, doc: LoadedYaml, path: str) -> list[Finding]:
    findings: list[Finding] = []
    for err in exc.errors(include_url=False):
        loc = [p for p in err["loc"] if isinstance(p, str | int)]
        dotted = ".".join(str(p) for p in loc) or "<корень>"
        msg = err["msg"]
        if err["type"] == "extra_forbidden":
            fix = "удалите поле или исправьте опечатку; допустимые поля описаны в шапке реестра"
            msg = "неизвестное поле"
        elif err["type"] == "missing":
            fix = "добавьте обязательное поле"
        else:
            fix = "исправьте значение по описанию полей в шапке реестра"
        findings.append(error(Code.POLICY_SCHEMA, path, doc.line_of(loc), f"{dotted}: {msg}", fix))
    return findings


def _duplicates(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    dups: list[str] = []
    for v in values:
        if v in seen and v not in dups:
            dups.append(v)
        seen.add(v)
    return dups


class _SemanticChecker:
    def __init__(self, policy: Policy, doc: LoadedYaml, path: str, today: dt.date) -> None:
        self.p = policy
        self.doc = doc
        self.path = path
        self.today = today
        self.findings: list[Finding] = []
        self.global_index = _build_index(policy)

    def _err(self, code: Code, loc: tuple[str | int, ...], message: str, fix: str) -> None:
        self.findings.append(error(code, self.path, self.doc.line_of(loc), message, fix))

    def run(self) -> list[Finding]:
        if self.p.reviewed_at > self.today:
            self._err(
                Code.POLICY_DATE,
                ("reviewed_at",),
                f"reviewed_at {self.p.reviewed_at} в будущем (сегодня {self.today})",
                "укажите дату фактического ревью реестра",
            )
        for tag in _duplicates(self.p.defaults.floating_tags_forbidden):
            self._err(
                Code.POLICY_DUPLICATE,
                ("defaults", "floating_tags_forbidden"),
                f"тег {tag!r} повторяется в floating_tags_forbidden",
                "удалите повтор",
            )
        repos: dict[str, str] = {}
        for name, comp in self.p.components.items():
            self._component(name, comp, repos)
        self._python()
        return self.findings

    def _component(self, name: str, comp: Component, repos: dict[str, str]) -> None:
        base: tuple[str | int, ...] = ("components", name)
        repo = comp.repository
        if repo is not None:
            if repo in repos:
                self._err(
                    Code.POLICY_DUPLICATE,
                    (*base, "image"),
                    f"образ {repo} указан и в компоненте {repos[repo]!r}, и в {name!r}",
                    "один образ должен принадлежать одному компоненту",
                )
            else:
                repos[repo] = name
        elif comp.digest is not None:
            self._err(
                Code.POLICY_SCHEMA,
                (*base, "digest"),
                f"{name}: digest задан, но image = null",
                "укажите image или уберите digest",
            )
        for v in _duplicates(comp.allowed):
            self._err(
                Code.POLICY_DUPLICATE,
                (*base, "allowed"),
                f"{name}: версия {v} повторяется в allowed",
                "удалите повтор",
            )
        listed: dict[str, list[str]] = {
            "image_tags": comp.denied.image_tags,
            "image_digests": [d.digest for d in comp.denied.image_digests],
            "image_digest_prefixes": [d.prefix for d in comp.denied.image_digest_prefixes],
            "pypi": [canonical_py_version(v) for v in comp.denied.pypi],
        }
        for key, values in listed.items():
            for v in _duplicates(values):
                self._err(
                    Code.POLICY_DUPLICATE,
                    (*base, "denied", key),
                    f"{name}: {v} повторяется в denied.{key}",
                    "удалите повтор",
                )
        if comp.image_tag_prefix and not re.fullmatch(r"[A-Za-z]{1,8}", comp.image_tag_prefix):
            self._err(
                Code.POLICY_SCHEMA,
                (*base, "image_tag_prefix"),
                f"{name}: image_tag_prefix {comp.image_tag_prefix!r} должен состоять из 1-8 букв",
                'обычно это "v" или пустая строка',
            )
        if comp.image_tag_suffix and (
            not re.fullmatch(r"(-[a-z][a-z0-9]*){1,3}", comp.image_tag_suffix)
            or len(comp.image_tag_suffix) > 32
            or classify_tag_version(f"1{comp.image_tag_suffix}").kind is TagKind.PRERELEASE
        ):
            self._err(
                Code.POLICY_SCHEMA,
                (*base, "image_tag_suffix"),
                f"{name}: image_tag_suffix {comp.image_tag_suffix!r} должен быть вариантом образа "
                "вида -slim-trixie (до 3 частей, до 32 символов, не пре-релиз)",
                'например "-slim-trixie" или пустая строка',
            )
        minimum = Version.parse(comp.min_version) if comp.min_version else None
        denied_versions = {canonical_py_version(v) for v in comp.denied.pypi}
        denied_tags = set(comp.denied.image_tags)
        for i, text in enumerate(comp.allowed):
            ver = Version.parse(text)
            loc = (*base, "allowed", i)
            if minimum is not None and ver < minimum:
                self._err(
                    Code.POLICY_VERSION,
                    loc,
                    f"{name}: разрешённая версия {ver} ниже min_version {minimum}",
                    "удалите версию из allowed или пересмотрите min_version",
                )
            if canonical_py_version(text) in denied_versions or (comp.tag_for(text) in denied_tags):
                self._err(
                    Code.POLICY_VERSION,
                    loc,
                    f"{name}: версия {ver} одновременно в allowed и в denied",
                    "удалите версию из allowed: denylist имеет приоритет",
                )
        if comp.digest is not None:
            # Denylist общий для всех компонентов, как и при проверке образов.
            hit = self.global_index.denied_match(comp.digest)
            if hit is not None:
                self._err(
                    Code.POLICY_VERSION,
                    (*base, "digest"),
                    f"{name}: зафиксированный digest совпадает с denylist компонента "
                    f"{hit.component!r} ({hit.value})",
                    "зафиксируйте digest чистого образа",
                )
        if comp.candidate is not None:
            self._candidate(name, comp, comp.candidate, minimum, denied_versions, denied_tags)

    def _candidate(
        self,
        name: str,
        comp: Component,
        cand: Candidate,
        minimum: Version | None,
        denied_versions: set[str],
        denied_tags: set[str],
    ) -> None:
        loc: tuple[str | int, ...] = ("components", name, "candidate")
        cv = Version.parse(cand.version)
        allowed = [Version.parse(a) for a in comp.allowed]
        if cv in allowed and self.today < cand.not_before:
            self._err(
                Code.POLICY_DATE,
                (*loc, "version"),
                f"{name}: кандидат {cv} уже в allowed, но карантин до {cand.not_before} "
                f"(сегодня {self.today})",
                f"уберите {cv} из allowed до {cand.not_before} или оформите security-исключение",
            )
        if minimum is not None and cv < minimum:
            self._err(
                Code.POLICY_VERSION,
                (*loc, "version"),
                f"{name}: кандидат {cv} ниже min_version {minimum}",
                "выберите другого кандидата",
            )
        if canonical_py_version(cand.version) in denied_versions or (
            comp.tag_for(cand.version) in denied_tags
        ):
            self._err(
                Code.POLICY_VERSION,
                (*loc, "version"),
                f"{name}: кандидат {cv} находится в denied",
                "выберите другого кандидата",
            )

    def _python(self) -> None:
        names = [canonical_py_name(n) for n in self.p.python_forbidden]
        for n in _duplicates(names):
            self._err(
                Code.POLICY_DUPLICATE,
                ("python_forbidden",),
                f"пакет {n} повторяется в python_forbidden",
                "удалите повтор",
            )
        for key in ("python_denylist", "python_min_versions"):
            mapping: dict[str, object] = getattr(self.p, key)
            for n in _duplicates([canonical_py_name(k) for k in mapping]):
                self._err(
                    Code.POLICY_DUPLICATE,
                    (key,),
                    f"пакет {n} указан в {key} несколько раз в разном написании",
                    "оставьте одну запись",
                )
        for pkg, versions in self.p.python_denylist.items():
            canon = [canonical_py_version(v) for v in versions]
            for v in _duplicates(canon):
                self._err(
                    Code.POLICY_DUPLICATE,
                    ("python_denylist", pkg),
                    f"{pkg}: версия {v} повторяется в python_denylist",
                    "удалите повтор",
                )


def _build_index(policy: Policy) -> ComponentIndex:
    by_repo: dict[str, tuple[str, Component]] = {}
    denied: dict[str, tuple[str, DeniedDigest]] = {}
    prefixes: list[tuple[str, DeniedDigestPrefix]] = []
    for name, comp in policy.components.items():
        repo = comp.repository
        if repo is not None:
            by_repo.setdefault(repo, (name, comp))
        for d in comp.denied.image_digests:
            denied.setdefault(d.digest, (name, d))
        prefixes.extend((name, d) for d in comp.denied.image_digest_prefixes)
    return ComponentIndex(
        by_repository=by_repo,
        denied_digests=denied,
        denied_prefixes=prefixes,
        floating_tags=frozenset(policy.defaults.floating_tags_forbidden),
    )


def load_policy(path: str, text: str, today: dt.date) -> LoadedPolicy:
    try:
        doc = load_yaml(text)
    except YamlLoadError as exc:
        code = Code.POLICY_DUPLICATE if "повторяющийся ключ" in exc.message else Code.POLICY_PARSE
        fix = "удалите повторяющийся ключ" if code is Code.POLICY_DUPLICATE else "исправьте YAML"
        return LoadedPolicy(path, None, [error(code, path, exc.line, exc.message, fix)])
    if not isinstance(doc.data, dict):
        return LoadedPolicy(
            path,
            None,
            [
                error(
                    Code.POLICY_SCHEMA,
                    path,
                    1,
                    "реестр должен быть YAML-словарём",
                    "проверьте, что указан правильный файл",
                )
            ],
        )
    try:
        policy = Policy.model_validate(doc.data)
    except ValidationError as exc:
        return LoadedPolicy(path, None, _pydantic_findings(exc, doc, path))
    findings = _SemanticChecker(policy, doc, path, today).run()
    if findings:
        return LoadedPolicy(path, policy, findings)
    return LoadedPolicy(path, policy, [], _build_index(policy))


__all__ = [
    "Component",
    "ComponentIndex",
    "LoadedPolicy",
    "Policy",
    "canonical_py_name",
    "load_policy",
]
