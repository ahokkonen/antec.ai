"""Проверка docker-compose файлов: services.*.image."""

from __future__ import annotations

from yaml.nodes import MappingNode, ScalarNode

from pincheck.checks import CheckContext
from pincheck.checks.image import check_image
from pincheck.report import Code, Finding, error
from pincheck.yamlload import YamlLoadError, load_yaml


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
    services = next(
        (v for k, v in root.value if isinstance(k, ScalarNode) and k.value == "services"), None
    )
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
    for svc_key, svc in services.value:
        if not isinstance(svc, MappingNode):
            continue
        for key, value in svc.value:
            if not (isinstance(key, ScalarNode) and key.value == "image"):
                continue
            line = value.start_mark.line + 1
            if not isinstance(value, ScalarNode) or value.tag != "tag:yaml.org,2002:str":
                findings.append(
                    error(
                        Code.FILE_PARSE,
                        path,
                        line,
                        f"services.{svc_key.value}.image должен быть строкой",
                        "укажите образ строкой name:tag@sha256:digest",
                    )
                )
                continue
            findings.extend(check_image(value.value, path, line, ctx))
    return findings
