"""Проверка docker-compose файлов: services.*.image."""

from __future__ import annotations

from yaml.nodes import MappingNode, ScalarNode

from pincheck.checks import CheckContext
from pincheck.checks.image import check_image
from pincheck.report import Code, Finding, error
from pincheck.yamlload import YamlLoadError, effective_items, load_yaml


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
        image = effective_items(svc).get("image")
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
        findings.extend(check_image(value.value, path, line, ctx))
    return findings
