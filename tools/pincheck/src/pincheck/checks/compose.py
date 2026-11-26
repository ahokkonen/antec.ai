"""Проверка docker-compose файлов: services.*.image."""

from __future__ import annotations

import re
from pathlib import Path

from yaml.nodes import MappingNode, Node, ScalarNode

from pincheck.checks import CheckContext
from pincheck.checks.dockerfile import check_dockerfile
from pincheck.checks.image import check_image
from pincheck.imageref import ImageRefError, parse_image_ref
from pincheck.report import Code, Finding, error
from pincheck.versions import TagKind, classify_tag_version
from pincheck.yamlload import COMPOSE_RESET_TAG, YamlLoadError, effective_items, load_yaml

# Образы, собираемые локально из build:, живут только в этом пространстве имён.
# Их внешние зависимости проверяются через FROM в Dockerfile сборки.
LOCAL_BUILD_NAMESPACE = "localhost/antec/"
_LOCAL_FIX = (
    f"для сервиса с build: укажите image: {LOCAL_BUILD_NAMESPACE}<имя>:<версия> "
    "и pull_policy: build"
)
_PRERELEASE_TOKEN_RE = re.compile(
    r"^(?:rc|alpha|beta|dev|pre|preview|snapshot|nightly|canary|edge|test)\d*$", re.IGNORECASE
)
_REMOTE_CONTEXT_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|git@|github\.com/)", re.IGNORECASE)


def _scalar(node: Node | None) -> str | None:
    if isinstance(node, ScalarNode) and node.tag == "tag:yaml.org,2002:str":
        return str(node.value)
    return None


def _display(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _check_local_tag(raw: str, tag: str | None, ctx: CheckContext) -> str | None:
    """Причина отказа для тега локальной сборки или None. Составной тег вида
    18.7-pgvector0.8.7 допустим; плавающие теги и пре-релизы запрещены, как в реестре."""
    if tag is None:
        return f"у локально собираемого образа {raw!r} должен быть тег версии"
    if tag in ctx.index.floating_tags:
        return f"плавающий тег {tag!r} запрещён (defaults.floating_tags_forbidden)"
    info = classify_tag_version(tag.removeprefix("v"))
    if info.kind is TagKind.NOT_VERSION:
        return f"тег {tag!r} должен начинаться с версии (например 1.2.3 или 18.7-pgvector0.8.7)"
    tokens = re.split(r"[-._+]", re.sub(r"^v?\d+(?:\.\d+)*", "", tag))
    if info.kind is TagKind.PRERELEASE or any(_PRERELEASE_TOKEN_RE.fullmatch(t) for t in tokens):
        return f"тег {tag!r} является пре-релизом; пре-релизы запрещены (ADR-0016)"
    return None


_VAR_DEFAULT_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:?-([^}$]*)\}")


def _check_build_dockerfile(
    build: Node, compose_file: Path | None, path: str, line: int, ctx: CheckContext
) -> list[Finding]:
    """Находит Dockerfile сборки и проверяет его FROM. Непроверяемые сборки отклоняются."""
    context: str | None = "."
    dockerfile: str | None = "Dockerfile"
    inline: str | None = None
    if isinstance(build, ScalarNode):
        context = _scalar(build)
    elif isinstance(build, MappingNode):
        items = effective_items(build)
        if "context" in items:
            context = _scalar(items["context"][1])
        if "dockerfile" in items:
            dockerfile = _scalar(items["dockerfile"][1])
        if "dockerfile_inline" in items:
            inline = _scalar(items["dockerfile_inline"][1])
        if "additional_contexts" in items:
            return [
                error(
                    Code.IMAGE_LOCAL_BUILD,
                    path,
                    line,
                    "build.additional_contexts не поддерживается: внешние источники сборки "
                    "нельзя проверить",
                    "соберите всё из одного локального контекста",
                )
            ]
    else:
        context = None
    if context is None or dockerfile is None:
        return [
            error(
                Code.IMAGE_LOCAL_BUILD,
                path,
                line,
                "build: context и dockerfile должны быть строками",
                _LOCAL_FIX,
            )
        ]
    if inline is not None:
        return check_dockerfile(f"{path}:build.dockerfile_inline", inline, ctx)
    # Интерполяция compose: ${VAR:-default} и ${VAR-default} проверяются по значению по
    # умолчанию (так их увидит compose без переменной); переменная без значения по умолчанию
    # не даёт найти Dockerfile.
    context = _VAR_DEFAULT_RE.sub(lambda m: m.group(1), context)
    if "$" in context:
        return [
            error(
                Code.IMAGE_LOCAL_BUILD,
                path,
                line,
                f"контекст сборки {context!r} содержит переменную без значения по умолчанию",
                "задайте ${VAR:-путь} или укажите путь без переменной",
            )
        ]
    if _REMOTE_CONTEXT_RE.match(context):
        return [
            error(
                Code.IMAGE_LOCAL_BUILD,
                path,
                line,
                f"удалённый контекст сборки {context!r} нельзя проверить",
                "используйте локальный каталог репозитория как context",
            )
        ]
    if compose_file is None:
        return [
            error(
                Code.IMAGE_LOCAL_BUILD,
                path,
                line,
                "не удалось определить каталог compose-файла для проверки Dockerfile сборки",
                "запускайте pincheck на файлах репозитория",
            )
        ]
    df_path = (compose_file.parent / context / dockerfile).resolve()
    try:
        df_text = df_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [
            error(
                Code.IMAGE_LOCAL_BUILD,
                path,
                line,
                f"Dockerfile сборки {_display(df_path)} не читается: {exc}",
                "проверьте build.context и build.dockerfile",
            )
        ]
    return check_dockerfile(_display(df_path), df_text, ctx)


def _check_local_build(
    raw: str,
    items: dict[str, tuple[ScalarNode, Node]],
    compose_file: Path | None,
    path: str,
    line: int,
    ctx: CheckContext,
) -> list[Finding]:
    findings: list[Finding] = []
    try:
        ref = parse_image_ref(raw)
    except ImageRefError as exc:
        return [
            error(
                Code.IMAGE_UNRESOLVABLE,
                path,
                line,
                f"не удалось разобрать образ: {exc}",
                _LOCAL_FIX,
            )
        ]
    if not ref.repository.startswith(LOCAL_BUILD_NAMESPACE) or ref.digest is not None:
        findings.append(
            error(
                Code.IMAGE_LOCAL_BUILD,
                path,
                line,
                f"сервис собирается локально (build:), но image {raw!r} не в пространстве "
                f"{LOCAL_BUILD_NAMESPACE} или содержит digest",
                _LOCAL_FIX,
            )
        )
    else:
        reason = _check_local_tag(raw, ref.tag, ctx)
        if reason is not None:
            findings.append(error(Code.IMAGE_LOCAL_BUILD, path, line, reason, _LOCAL_FIX))
    policy = items.get("pull_policy")
    if policy is None or _scalar(policy[1]) != "build":
        got = _scalar(policy[1]) if policy is not None else None
        findings.append(
            error(
                Code.IMAGE_LOCAL_BUILD,
                path,
                line,
                "у локально собираемого образа нужен pull_policy: build "
                f"(сейчас: {got or 'не задан'}): иначе Compose может скачать или взять "
                "чужой образ с тем же именем",
                "добавьте pull_policy: build",
            )
        )
    findings.extend(_check_build_dockerfile(items["build"][1], compose_file, path, line, ctx))
    return findings


def check_compose(
    path: str, text: str, ctx: CheckContext, source: Path | None = None
) -> list[Finding]:
    try:
        doc = load_yaml(text)
    except YamlLoadError as exc:
        return [error(Code.FILE_PARSE, path, exc.line, exc.message, "исправьте YAML")]
    root = doc.root
    if root is None:
        return []
    if not isinstance(root, MappingNode):
        return [
            error(
                Code.FILE_PARSE,
                path,
                root.start_mark.line + 1,
                "compose-файл должен быть YAML-словарём",
                "проверьте структуру файла",
            )
        ]
    services_pair = effective_items(root).get("services")
    services = services_pair[1] if services_pair is not None else None
    if services is None:
        return []
    if not isinstance(services, MappingNode):
        return [
            error(
                Code.FILE_PARSE,
                path,
                services.start_mark.line + 1,
                "services должен быть словарём",
                "проверьте структуру файла",
            )
        ]
    findings: list[Finding] = []
    for svc_name, (_, svc) in effective_items(services).items():
        if not isinstance(svc, MappingNode):
            continue
        items = effective_items(svc)
        # build: !reset null снимает локальную сборку в override (образы ядра, ADR-0014).
        if "build" in items and items["build"][1].tag == COMPOSE_RESET_TAG:
            del items["build"]
        image = items.get("image")
        if image is None:
            if "build" in items:
                # Без image Compose всё равно собирает образ: проверяем FROM его Dockerfile.
                line = items["build"][0].start_mark.line + 1
                findings.extend(_check_build_dockerfile(items["build"][1], source, path, line, ctx))
            continue
        value = image[1]
        line = value.start_mark.line + 1
        if not isinstance(value, ScalarNode) or value.tag != "tag:yaml.org,2002:str":
            findings.append(
                error(
                    Code.FILE_PARSE,
                    path,
                    line,
                    f"services.{svc_name}.image должен быть строкой",
                    "укажите образ строкой name:tag@sha256:digest",
                )
            )
            continue
        if "build" in items:
            findings.extend(_check_local_build(value.value, items, source, path, line, ctx))
        else:
            findings.extend(check_image(value.value, path, line, ctx))
    return findings
