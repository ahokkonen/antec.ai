# pincheck

CI-проверка закрепления версий: не даёт развернуть образ или пакет в обход
[`policy/allowed-versions.yaml`](../../policy/allowed-versions.yaml).
Решения: [ADR-0004](../../docs/adr/0004-dependency-pinning-and-supply-chain.md),
[ADR-0016](../../docs/adr/0016-pincheck.md).

## Что проверяется

1. Сам реестр: схема (неизвестные поля запрещены), даты, дубли ключей и значений,
   allowed не ниже `min_version` и не в denied, кандидат не попал в allowed раньше `not_before`.
2. docker-compose (`compose*.y*ml`, `docker-compose*.y*ml`): `services.*.image`. У сервиса с
   `build:` image должен быть `localhost/antec/<имя>:<версия>` с `pull_policy: build`; Dockerfile
   сборки (`build.context` + `build.dockerfile` или `dockerfile_inline`) проверяется всегда, даже если
   передан только compose-файл.
3. Dockerfile (`Dockerfile`, `Dockerfile.*`, `*.Dockerfile`, `Containerfile`): все `FROM`,
   кроме `scratch` и ссылок на предыдущие стадии.
4. `uv.lock`: `python_forbidden`, `python_denylist`, `python_min_versions`.

Правила для образа: `name:tag@sha256:digest` (без digest всегда ошибка); образ есть в реестре;
тег = `image_tag_prefix` + версия из `allowed` + `image_tag_suffix` (вариант образа, например
`-slim-trixie`), не ниже `min_version`; плавающие теги и пре-релизы
запрещены; тег и digest не в denylist (digest из denylist ловится для любого образа, усечённые
digest из `image_digest_prefixes` сверяются по префиксу); если digest
в реестре зафиксирован, он совпадает. `digest: null` в реестре даёт предупреждение, с
`--require-digest` ошибку.

## Запуск локально

Нужен [uv](https://docs.astral.sh/uv/). Из корня репозитория:

```bash
uv run --project tools/pincheck --locked pincheck --policy policy/allowed-versions.yaml --exclude tools/pincheck/tests/fixtures .
```

Фикстуры тестов намеренно содержат нарушения, поэтому при проверке всего репозитория их
нужно исключать. В репозитории инсталляции (ADR-0005) добавьте `--require-digest`.

Опции: `--exclude GLOB` (можно повторять), `--require-digest`, `--core-images PATH`
(`core-images.yaml` инсталляции: образы ядра разрешены только с его тегом и digest, ADR-0014), `--format text|github|auto`
(auto = аннотации GitHub внутри Actions), `--today YYYY-MM-DD` (дата для проверок карантина).

Код выхода: `0` нет ошибок (предупреждения допустимы), `1` есть ошибки или реестр невалиден,
`2` неверные аргументы командной строки.

## Разработка

```bash
cd tools/pincheck
uv sync --locked
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked mypy
uv run --locked pytest
```

Новые зависимости: только после проверки лицензии, версии и OSV (AGENTS.md); `exclude-newer`
в `pyproject.toml` держит карантин 7 дней и для транзитивных пакетов.

## Коды нарушений

| Код | Что нарушено |
| --- | --- |
| PIN001 | Реестр: некорректный YAML |
| PIN002 | Реестр: схема (тип, формат, неизвестное или отсутствующее поле) |
| PIN003 | Реестр: дубль ключа или значения, один образ в двух компонентах |
| PIN004 | Реестр: дата (`reviewed_at` в будущем, кандидат в allowed до `not_before`) |
| PIN005 | Реестр: версии (allowed ниже `min_version`, allowed или кандидат в denied) |
| PIN006 | digest образа не зафиксирован в реестре (предупреждение; ошибка с `--require-digest`) |
| PIN010 | Образ без digest |
| PIN011 | Плавающий тег |
| PIN012 | Пре-релиз |
| PIN013 | Образ не найден в реестре |
| PIN014 | Версия не в allowed (в том числе кандидат на карантине, неверный префикс, суффикс варианта) |
| PIN015 | Версия ниже `min_version` |
| PIN016 | digest не совпадает с реестром |
| PIN017 | Тег в denylist |
| PIN018 | digest в denylist (точно или по префиксу из `image_digest_prefixes`) |
| PIN019 | Компонент с пустым allowed |
| PIN020 | Ссылку на образ нельзя проверить (переменная, некорректный формат) |
| PIN021 | Образ без тега |
| PIN022 | Сервис с `build:`: image вне `localhost/antec/`, с digest, плавающий тег или пре-релиз, нет `pull_policy: build`, удалённый контекст или `additional_contexts`, переменная в контексте без значения по умолчанию (`${VAR:-путь}` проверяется по умолчанию), Dockerfile сборки не читается |
| PIN023 | Образ ядра antec.ai (`--core-images`) с тегом или digest не из `core-images.yaml` инсталляции (ADR-0014) |
| PIN030 | Python-пакет запрещён в любой версии |
| PIN031 | Версия Python-пакета в denylist |
| PIN032 | Версия Python-пакета ниже минимальной |
| PIN033 | Некорректный `uv.lock` |
| PIN034 | Пре-релиз Python-пакета (a, b, rc, dev по PEP 440) в `uv.lock`; исключения только в `python_prerelease_allowed` с причиной (ADR-0013) |
| PIN040 | Некорректный compose-файл или `FROM` |
| PIN041 | Путь не существует, неизвестный тип файла или реестр не читается |

Теги слияния Compose `!reset` и `!override` поддерживаются: `build: !reset null` в override
снимает локальную сборку (образы ядра по digest в инсталляции, ADR-0014).
