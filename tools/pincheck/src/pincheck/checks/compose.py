"""Проверка docker-compose файлов: services.*.image."""

from __future__ import annotations

from yaml.nodes import MappingNode, ScalarNode

from pincheck.checks import CheckContext
from pincheck.checks.image import check_image
from pincheck.imageref import ImageRefError, parse_image_ref
from pincheck.report import Code, Finding, error
from pincheck.yamlload import YamlLoadError, effective_items, load_yaml

# Образы, собираемые локально из build:, живут только в этом пространстве имён.
# Их внешние зависимости проверяются через FROM в Dockerfile.
LOCAL_BUILD_NAMESPACE = "localhost/antec/"


def _check_local_build(raw: str, path: str, line: int) -> list[Finding]:
    fix = (
        f"для сервиса с build: укажите image: {LOCAL_BUILD_NAMESPACE}<имя>:<версия> "
        "и pull_policy: build"
    )
    try:
        ref = parse_image_ref(raw)
    except ImageRefError as exc:
        return [
            error(Code.IMAGE_UNRESOLVABLE, path, line, f"не удалось разобрать образ: {exc}", fix)
        ]
    if not ref.repository.startswith(LOCAL_BUILD_NAMESPACE) or ref.digest is not None:
        return [
            error(
                Code.IMAGE_LOCAL_BUILD,
                path,
                line,
                f"сервис собирается локально (build:), но image {raw!r} не в пространстве "
                f"{LOCAL_BUILD_NAMESPACE} или содержит digest",
                fix,
            )
        ]
    if ref.tag is None or ref.tag in {"latest"}:
        return [
            error(
                Code.IMAGE_LOCAL_BUILD,
                path,
                line,
                f"у локально собираемого образа {raw!r} должен быть тег версии",
                fix,
            )
        ]
    return []


def check_compose(path: str, text: str, ctx: CheckContext) -> list[Finding]:
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
        image = items.get("image")
        if image is None:
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
            findings.extend(_check_local_build(value.value, path, line))
        else:
            findings.extend(check_image(value.value, path, line, ctx))
    return findings
