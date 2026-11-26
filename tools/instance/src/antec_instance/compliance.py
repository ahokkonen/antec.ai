"""Документы комплаенса из манифеста (ADR-0015, п. 12): проект реестра обработки (GDPR, ст. 30)
и факты о системе для DPIA (ст. 35(7)). Это черновики для юриста или DPO, не готовые документы.

Условия провайдеров: `policy/llm-providers.yaml` ядра (факты на дату проверки).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from antec_instance.manifest import Manifest
from antec_instance.yamlload import YamlError, load_mapping

PROVIDERS_FILE = Path("policy") / "llm-providers.yaml"
NOT_SET = "_не задано_"

PURPOSES = (
    "ассистент и агенты для работы сотрудников (чат, инструменты, документы)",
    "безопасность, расследование инцидентов и выполнение требований: аудит вызовов моделей и "
    "инструментов (ADR-0012)",
)
NOT_PURPOSES = "найм, оценка, мониторинг работы и решения о сотрудниках (ADR-0015, п. 2)"
SUBJECTS = (
    "сотрудники организации (пользователи)",
    "люди, чьи данные пользователи вводят или получают через инструменты",
)
DATA = (
    "учётная запись: имя, email, идентификаторы IdP (Keycloak, Entra ID)",
    "тексты сообщений и ответов, история разговоров",
    "метаданные вызовов моделей и инструментов, расходы",
    "аудит: кто, что, когда, решение (без текстов сообщений)",
    "данные инструментов (почта, календарь, файлы) по классам данных манифеста",
)
MEASURES = (
    "вход только через IdP организации с MFA; токены проверяются Platform API (ADR-0006, ADR-0007)",
    "права на модели, агенты и инструменты в Cerbos; агент не получает больше прав, чем "
    "пользователь (ADR-0009, ADR-0010)",
    "необратимые действия только после подтверждения человеком (ADR-0010)",
    "классы данных: модель получает данные не выше своего допуска (ADR-0011)",
    "аудит до действия, отдельный владелец схемы, цепочка хэшей, подписанные контрольные точки "
    "(ADR-0012)",
    "сроки хранения и автоматическая очистка, права субъекта: выгрузка и удаление (ADR-0012)",
    "тексты сообщений не попадают в журналы и тексты ошибок (ADR-0012)",
    "секреты только в OpenBao; образы по digest из реестра версий (ADR-0004, ADR-0017)",
    "уведомление об ИИ и машиночитаемая метка ответов (AI Act, ст. 50; ADR-0015)",
    "бэкап с проверкой восстановления, включая аудит (ADR-0017)",
)


class ProvidersError(Exception):
    pass


def load_providers(core_dir: Path) -> dict[str, Any]:
    path = core_dir / PROVIDERS_FILE
    try:
        data = load_mapping(path)
    except YamlError as exc:
        raise ProvidersError(f"{PROVIDERS_FILE}: {exc}") from exc
    providers = data.get("providers")
    if not isinstance(providers, dict) or not all(isinstance(v, dict) for v in providers.values()):
        raise ProvidersError(f"{PROVIDERS_FILE}: ожидается словарь providers")
    return providers


def _cell(value: object) -> str:
    text = str(value) if value not in (None, "") else NOT_SET
    return text.replace("|", "\\|").replace("\n", " ")


def _bullets(items: tuple[str, ...]) -> str:
    return "".join(f"- {item}\n" for item in items)


def _models_table(manifest: Manifest, providers: dict[str, Any]) -> str:
    rows = [
        "| Модель | Провайдер | Юрлицо | Страна | Передача | Хранение у провайдера "
        "| ZDR по договору | Регион по договору |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for m in manifest.models:
        p = providers.get(m.provider, {})
        terms = m.provider_terms
        rows.append(
            "| "
            + " | ".join(
                _cell(v)
                for v in (
                    m.name,
                    m.provider,
                    p.get("entity"),
                    p.get("country"),
                    p.get("transfer"),
                    p.get("retention"),
                    ("да" if terms.zdr else "нет") if terms else None,
                    terms.region if terms else None,
                )
            )
            + " |"
        )
    return "\n".join(rows) + "\n"


def _party(manifest: Manifest, role: str) -> str:
    party = getattr(manifest.compliance, role)
    return f"{_cell(party.name)}, {_cell(party.contact)}" if party else NOT_SET


def record(manifest: Manifest, providers: dict[str, Any]) -> str:
    """Проект реестра обработки: ст. 30(1) у контролёра, ст. 30(2) у обработчика."""
    c = manifest.compliance
    r = manifest.retention
    processor = c.operator_role == "processor"
    head = (
        "Реестр обработчика (GDPR, ст. 30(2))"
        if processor
        else "Реестр контролёра (GDPR, ст. 30(1))"
    )
    recipients = [f"провайдеры моделей: {', '.join(sorted({m.provider for m in manifest.models}))}"]
    recipients += [f"MCP-сервер {s.name} ({s.url})" for s in manifest.mcp_servers]
    return f"""# {head}: {manifest.instance.name}

Сгенерировано `antec-instance render` из манифеста (ADR-0015). Черновик: проверяет юрист или DPO.

| Поле | Значение |
| --- | --- |
| Контролёр | {_party(manifest, "controller")} |
| Обработчик | {_party(manifest, "processor") if processor else "не применимо"} |
| DPO или контакт | {_cell(c.dpo_contact)} |
| Уведомление о конфиденциальности | {_cell(c.privacy_notice_url)} |

## Цели (ст. 30(1)(b))

{_bullets(PURPOSES)}
Не цели: {NOT_PURPOSES}.

## Категории субъектов и данных (ст. 30(1)(c))

Субъекты:

{_bullets(SUBJECTS)}
Данные:

{_bullets(DATA)}
## Получатели (ст. 30(1)(d))

{_bullets(tuple(recipients))}
## Передачи вне ЕЭЗ (ст. 30(1)(e)) и условия провайдеров

{_models_table(manifest, providers)}
Источник: `policy/llm-providers.yaml` ядра.

## Сроки (ст. 30(1)(f))

- тексты сообщений и чаты Open WebUI: {r.content_days} дней;
- метаданные вызовов и запусков: {r.metadata_days} дней;
- аудит, расходы LiteLLM, события Keycloak: {r.audit_days} дней;
- бэкапы: 35 дней, старые удаляются вручную (автоматического удаления нет).

## Меры защиты (ст. 30(1)(g), ст. 32)

{_bullets(MEASURES)}"""


def dpia_facts(manifest: Manifest, providers: dict[str, Any], warnings: list[str]) -> str:
    """Факты о системе для разделов 1 и 4 DPIA (docs/compliance/dpia.md инсталляции)."""
    agents = [
        "| Агент | Модель | Инструменты (только чтение) | Инструменты с подтверждением |",
        "| --- | --- | --- | --- |",
    ]
    tools = {(s.name, t.name): t for s in manifest.mcp_servers for t in s.tools}
    for a in manifest.agents:
        refs = [(ref, tools[tuple(ref.split(".", 1))]) for ref in a.tools]  # type: ignore[index]
        read = ", ".join(r for r, t in refs if t.read_only) or "нет"
        write = ", ".join(r for r, t in refs if not t.read_only) or "нет"
        agents.append(f"| {a.name} | {a.model} | {read} | {write} |")
    classes = [f"- модель {m.name}: допуск {m.max_data_class}" for m in manifest.models] + [
        f"- класс ввода пользователя: {manifest.data.default_class}"
    ]
    notes = _bullets(tuple(warnings)) if warnings else "- нет\n"
    return f"""# Факты для DPIA: {manifest.instance.name}

Сгенерировано `antec-instance render` (ADR-0015). Ядро {manifest.core.version}, манифест
schema_version {manifest.schema_version}. Переносится в `docs/compliance/dpia.md`, разделы 1 и 4.

## Модели и провайдеры

{_models_table(manifest, providers)}
## Агенты и инструменты

{chr(10).join(agents)}

## Классы данных (ADR-0011)

{chr(10).join(classes)}

## Сроки хранения (ADR-0012)

Тексты {manifest.retention.content_days} дней, метаданные {manifest.retention.metadata_days} дней,
аудит {manifest.retention.audit_days} дней.

## Технические меры ядра

{_bullets(MEASURES)}
## Что закрыть до DPIA

{notes}"""
