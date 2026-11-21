"""Проверка одной ссылки на образ по реестру. Общая для docker-compose и Dockerfile."""

from __future__ import annotations

from pincheck.checks import CheckContext
from pincheck.imageref import ImageRefError, parse_image_ref
from pincheck.report import Code, Finding, error, warning
from pincheck.versions import TagKind, Version, classify_tag_version


def _fmt_allowed(prefix: str, allowed: list[str]) -> str:
    return ", ".join(f"{prefix}{a}" for a in allowed)


def check_image(raw: str, path: str, line: int | None, ctx: CheckContext) -> list[Finding]:
    out: list[Finding] = []
    pol = ctx.policy_path

    if "$" in raw:
        return [
            error(
                Code.IMAGE_UNRESOLVABLE,
                path,
                line,
                f"образ {raw!r} содержит переменную: значение нельзя проверить статически",
                "укажите образ литералом name:tag@sha256:digest; переменные для образов запрещены",
            )
        ]
    try:
        ref = parse_image_ref(raw)
    except ImageRefError as exc:
        return [
            error(
                Code.IMAGE_UNRESOLVABLE,
                path,
                line,
                f"не удалось разобрать образ {raw!r}: {exc}",
                "используйте формат name:tag@sha256:digest",
            )
        ]

    if ref.digest is None:
        out.append(
            error(
                Code.IMAGE_NO_DIGEST,
                path,
                line,
                f"образ {raw!r} указан без digest",
                f"закрепите образ как {ref.repository}:<tag>@sha256:<digest> "
                f"(digest из {pol} или из проверенного pull)",
            )
        )
    else:
        hit = ctx.index.denied_match(ref.digest)
        if hit is not None:
            how = (
                f"совпадает по префиксу {hit.value}"
                if hit.by_prefix
                else f"{ref.digest} в denylist"
            )
            out.append(
                error(
                    Code.IMAGE_DENIED_DIGEST,
                    path,
                    line,
                    f"digest {how} компонента {hit.component!r} "
                    f"(известный вредоносный или отозванный образ, {hit.confidence})",
                    f"немедленно замените на разрешённую версию из {pol}; если образ запускался, "
                    "действуйте по ADR-0004, п. 10 (ротация секретов, пересборка хоста)",
                )
            )

    if ref.tag is None:
        out.append(
            error(
                Code.IMAGE_NO_TAG,
                path,
                line,
                f"образ {raw!r} указан без тега",
                "укажите тег версии перед digest: name:tag@sha256:digest",
            )
        )
        return out

    if ref.tag in ctx.index.floating_tags:
        out.append(
            error(
                Code.IMAGE_FLOATING_TAG,
                path,
                line,
                f"плавающий тег {ref.tag!r} запрещён (defaults.floating_tags_forbidden)",
                f"укажите конкретную версию из allowed в {pol}",
            )
        )
        return out

    entry = ctx.index.by_repository.get(ref.repository)
    if entry is None:
        out.append(
            error(
                Code.IMAGE_UNKNOWN,
                path,
                line,
                f"образ {ref.repository} не найден в реестре",
                f"добавьте компонент с этим image в {pol} через PR с ревью (ADR-0004) "
                "или используйте образ из реестра",
            )
        )
        return out
    name, comp = entry

    if ref.tag in comp.denied.image_tags:
        out.append(
            error(
                Code.IMAGE_DENIED_TAG,
                path,
                line,
                f"тег {ref.tag!r} компонента {name!r} в denylist",
                "используйте версию из allowed: "
                + _fmt_allowed(comp.image_tag_prefix, comp.allowed),
            )
        )
        return out

    if not comp.allowed:
        out.append(
            error(
                Code.IMAGE_COMPONENT_NOT_DEPLOYABLE,
                path,
                line,
                f"компонент {name!r} нельзя развернуть: список allowed пуст",
                f"зафиксируйте версию в {pol} (components.{name}.allowed) через PR с ревью",
            )
        )
        return out

    prefix = comp.image_tag_prefix
    expected = _fmt_allowed(prefix, comp.allowed)
    if prefix and not ref.tag.startswith(prefix):
        out.append(
            error(
                Code.IMAGE_VERSION_NOT_ALLOWED,
                path,
                line,
                f"тег {ref.tag!r} компонента {name!r} должен начинаться с префикса {prefix!r}",
                f"используйте тег из allowed: {expected}",
            )
        )
        return out
    info = classify_tag_version(ref.tag[len(prefix) :])
    if info.kind is TagKind.PRERELEASE:
        out.append(
            error(
                Code.IMAGE_PRERELEASE,
                path,
                line,
                f"тег {ref.tag!r} является пре-релизом; пре-релизы запрещены (ADR-0016)",
                f"используйте стабильную версию из allowed: {expected}",
            )
        )
        return out
    if info.version is None:
        what = "вариант с суффиксом" if info.kind is TagKind.VARIANT else "не версия"
        out.append(
            error(
                Code.IMAGE_VERSION_NOT_ALLOWED,
                path,
                line,
                f"тег {ref.tag!r} компонента {name!r} не поддерживается ({what})",
                f"используйте тег вида {prefix}X.Y.Z из allowed: {expected}",
            )
        )
        return out
    version = info.version

    if comp.min_version is not None and version < Version.parse(comp.min_version):
        out.append(
            error(
                Code.IMAGE_BELOW_MIN,
                path,
                line,
                f"версия {version} компонента {name!r} ниже min_version {comp.min_version}",
                f"обновите до версии из allowed: {expected}",
            )
        )
        return out

    allowed = [Version.parse(a) for a in comp.allowed]
    if version not in allowed:
        cand = comp.candidate
        if cand is not None and Version.parse(cand.version) == version:
            msg = (
                f"версия {version} компонента {name!r} является кандидатом и ещё не разрешена "
                f"(карантин до {cand.not_before})"
            )
            fix = f"дождитесь ревью и переноса в allowed; сейчас разрешено: {expected}"
        else:
            msg = f"версия {version} компонента {name!r} не входит в allowed"
            fix = f"используйте {expected} или добавьте версию в {pol} через PR с ревью"
        out.append(error(Code.IMAGE_VERSION_NOT_ALLOWED, path, line, msg, fix))
        return out

    if ref.digest is None or ctx.index.denied_match(ref.digest) is not None:
        return out

    current = allowed[0]
    make = error if ctx.require_digest else warning
    if comp.digest is None:
        out.append(
            make(
                Code.DIGEST_NOT_PINNED,
                path,
                line,
                f"digest компонента {name!r} в реестре не зафиксирован (digest: null), "
                "соответствие образа реестру не проверено",
                f"после проверки подписи и denylist зафиксируйте digest в {pol} "
                f"(components.{name}.digest)",
            )
        )
    elif version == current:
        if ref.digest != comp.digest:
            out.append(
                error(
                    Code.IMAGE_DIGEST_MISMATCH,
                    path,
                    line,
                    f"digest образа не совпадает с зафиксированным в реестре для "
                    f"{name} {version}: ожидается {comp.digest}",
                    "используйте digest из реестра; если образ пересобран upstream, "
                    "проведите проверку по ADR-0004 и обновите реестр через PR",
                )
            )
    else:
        out.append(
            make(
                Code.DIGEST_NOT_PINNED,
                path,
                line,
                f"в реестре digest зафиксирован только для текущей версии {current} "
                f"компонента {name!r}, для {version} не проверен",
                f"перейдите на {prefix}{current} или зафиксируйте digest для этой версии",
            )
        )
    return out
