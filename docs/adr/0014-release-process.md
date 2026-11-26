# ADR-0014: Релизный процесс ядра и поддержка версий N и N-1

- Статус: предложено
- Дата: 2026-11-26
- Ответственный: CTO
- Связанные: ADR-0004, ADR-0005, ADR-0008, ADR-0010, ADR-0012, ADR-0016, ADR-0017,
  [CHANGELOG.md](../../CHANGELOG.md), [tools/release](../../tools/release/README.md)

## Контекст

ADR-0005 требует, чтобы ядро выпускало релизы по semver (подписанные образы, SBOM, changelog) и
поддерживало версии N и N-1. ADR-0008 (п. 6) отложил до этого ADR переход инсталляций с локальной
сборки на образы ядра по digest. Критерий выхода MVP: «все образы по digest из реестра версий,
SBOM есть».

Состояние на 2026-11-26 (коммит 5332399):

- `VERSION` = `0.1.0`, тегов, релизов и CHANGELOG нет. Версия вручную продублирована в 12 местах:
  pyproject и `__version__` Platform API, `tools/instance`, `tools/pincheck`, label Dockerfile,
  `CLIENT_INFO` MCP-клиента, 4 образа `localhost/antec/platform-api:0.1.0` в compose, README
  стенда, `core.version` в `instances/dev`. Проверяется только `core.version` = `VERSION`.
- Образы ядра (`platform-api`, `postgres`, `keycloak`) только собираются локально
  (`localhost/antec/*`, `pull_policy: build`), не публикуются и не подписываются.
- Манифест: валидны `schema_version` 1-5, политики устаревания нет.
- `up.sh` применяет миграции и `audit.sql` без бэкапа; миграция 0005 необратима (`downgrade`
  бросает исключение); понижение версии ядра ничем не остановлено.
- Репозиторий `instance-mtech` ещё не создан; инсталляции проверяются шаблоном в CI.

Факты (проверено 2026-11-26, граница карантина 7 дней: 2026-11-19):

- cosign 3.1.4 (2026-10-20, Apache-2.0): OSV пусто; GHSA-fx35-mq7g-6g98 (high, обход проверки
  через ключ в legacy bundle) закрыт в 3.1.3. 3.1.5 и 3.1.6 (2026-11-24 и 2026-11-25) в
  карантине; в 3.1.6 есть исправления проверки без опубликованного advisory. Бинарник
  `cosign-linux-amd64` подписан keyless (identity `keyless@projectsigstore.iam.gserviceaccount.com`,
  issuer `https://accounts.google.com`), подпись проверена.
- syft 1.57.2 (2026-11-16, Apache-2.0): OSV пусто, последние GHSA закрыты в 1.52.0 и 1.42.3;
  `checksums.txt` подписан keyless workflow `release.yaml@refs/heads/main`, подпись проверена.
- BuildKit 0.35.0 (2026-11-11, Apache-2.0): GHSA 2026 года касаются вредоносных frontend и
  git-контекстов (у нас их нет, PIN022). Образ `moby/buildkit` не подписан; BuildKit provenance
  указывает на `refs/tags/v0.35.0` (коммит f55949dea4f03bb9d85b73e9afc9a609291289db).
  Без явного `image=` buildx и setup-buildx-action берут BuildKit по плавающему тегу.
- GHCR не поддерживает OCI Referrers API (`/v2/<repo>/referrers/<digest>` отвечает 404);
  cosign 3 хранит подписи в теге `sha256-<hex>` (так подписан `ghcr.io/cerbos/cerbos:0.57.0`,
  проверено cosign 3.1.4).
- release-please 17.11.2 (Apache-2.0): тег, созданный через `GITHUB_TOKEN`, не запускает
  другие workflow, нужен PAT или GitHub App с правом записи.
- GitHub-раннеры `ubuntu-24.04-arm` бесплатны для публичных репозиториев.

## Варианты

Версия и changelog:

| Вариант | Версия, лицензия | Плюсы | Минусы | Стоимость поддержки |
| --- | --- | --- | --- | --- |
| **`VERSION` + `antec-release check/bump` + CHANGELOG вручную** | наш код, stdlib | Ни одного нового токена с правом записи; проверка на каждый PR | Раздел CHANGELOG пишется руками | Низкая |
| release-please | 17.11.2, Apache-2.0 | Релизный PR и CHANGELOG из Conventional Commits | PAT или App с записью в репозиторий; Node и npm-зависимости в CI | Средняя |
| git-cliff | 2.14.2, Apache-2.0 | CHANGELOG из коммитов | Ещё один бинарник; разделы «Обновление» всё равно руками | Низкая |

Подпись, SBOM, provenance:

| Вариант | Плюсы | Минусы |
| --- | --- | --- |
| **cosign keyless + syft SPDX как cosign-аттестация + BuildKit provenance в индексе** | Тот же cosign, которым проверяем чужие образы; подпись индекса покрывает provenance | Подписи в теге `sha256-<hex>` (GHCR без referrers) |
| GitHub artifact attestations (`actions/attest*`) | Проверка `gh attestation verify` | Второй механизм рядом с cosign; привязка к GitHub API |
| Ключ cosign в OpenBao | Не нужен Rekor | Ключ нужно хранить, ротировать и раздавать инсталляциям |

Закрепление образов ядра в инсталляции:

| Вариант | Плюсы | Минусы |
| --- | --- | --- |
| **Подписанный `release.json` в GitHub Release, `core-images.yaml` в инсталляции, override compose при рендере** | Digest нельзя записать в коммит, из которого он собран: список приходит отдельным подписанным артефактом | Ещё один файл в инсталляции |
| digest в `policy/allowed-versions.yaml` ядра | Один реестр | Требует второго коммита после релиза: тег указывает не на тот коммит |
| Переменные в `image:` compose | Просто | Запрещено pincheck (ADR-0016, допущение) |

## Решение

1. **Версия.** Единственный источник: `VERSION` в корне. Все компоненты ядра (Platform API,
   `tools/instance`, `tools/pincheck`, `tools/release`, образы) имеют одну версию. Плагины
   (`instances/dev/plugins/*`) версионируются отдельно. `tools/release`:
   - `antec-release check`: все места версии совпадают с `VERSION`, включая собственный пакет в
     `uv.lock`; `CHANGELOG.md` содержит раздел `[Не выпущено]`; с `--tag vX.Y.Z` ещё и тег =
     `v` + `VERSION`, а в CHANGELOG есть раздел `[X.Y.Z]` с датой. Запускается в CI на каждый PR.
   - `antec-release bump X.Y.Z`: переписывает все места и переносит `[Не выпущено]` в `[X.Y.Z]`.
   - `CLIENT_INFO` MCP-клиента берёт версию из метаданных пакета, а не из литерала.
2. **Semver.** До `1.0.0`: `0.MINOR` может содержать несовместимые изменения, `PATCH` только
   исправления и security, без новой `schema_version` манифеста и без необратимых миграций.
   `1.0.0` выпускается до первого клиента (ADR-0005: контракт стабилен до первого клиента).
   Потом обычный semver.
3. **Публичный контракт ядра.** Меняется только в minor (после 1.0 в major) с записью в
   «Несовместимые изменения» и инструкцией в «Обновление»:
   - манифест: ядро N принимает все `schema_version`, которые принимала N-1. Схема сначала
     объявляется устаревшей (предупреждение `validate`), удаляется не раньше чем через 2 minor.
     v1-v4 устарели в 0.1.0, удаляются не раньше 0.3.0;
   - плагины: ревизии MCP, аудитория token exchange и claims токена (ADR-0007), `azp` каналов,
     семантика scope Cerbos, а также виды ресурсов, действия и атрибуты, на которые ссылаются
     политики инсталляции;
   - фасад: пути `/v1`, заголовки `x-antec-*`, коды ошибок, API `agent-runs/{id}/decision`;
   - оператор: раскладка OpenBao `antec/*`, аргументы `up.sh`, `backup.sh`, `restore-verify.sh`,
     `set-*.sh`, формат бэкапа (`restore-verify.sh` N восстанавливает бэкап N-1);
   - БД: миграции только вперёд, необратимые помечены (`IRREVERSIBLE = True` в модуле); откат
     ядра только восстановлением из бэкапа; изменение формата цепочки или контрольных точек
     аудита (`audit.sql`, ADR-0012) несовместимо.
   Переменные `PLATFORM_*` в контракт не входят: их рендерит ядро, инсталляция влияет только
   через `settings` манифеста.
4. **CHANGELOG.md** вручную, формат Keep a Changelog, разделы: Добавлено, Изменено,
   Несовместимые изменения, Исправлено, Безопасность, Обновление. Раздел «Обновление»
   обязателен, если есть миграции, новая `schema_version` или несовместимые изменения.
5. **Сборка и публикация** (`.github/workflows/release.yml`). На PR и main: `antec-release check`
   и сборка всех образов для `linux/amd64` и `linux/arm64` без публикации. По аннотированному
   тегу `v*`, который создаёт и пушит сопровождающий:
   - образы `ghcr.io/ahokkonen/antec-{platform-api,postgres,keycloak}:X.Y.Z`, сборка на нативных
     раннерах (`ubuntu-24.04`, `ubuntu-24.04-arm`) через `docker buildx` с BuildKit из реестра по
     digest, `provenance: mode=max`; индекс собирается `docker buildx imagetools create`.
     Тег образа = версия ядра, версия компонента остаётся в label;
   - подпись `cosign sign` keyless по digest индекса; identity
     `https://github.com/ahokkonen/antec.ai/.github/workflows/release.yml@refs/tags/vX.Y.Z`,
     issuer `https://token.actions.githubusercontent.com`;
   - SBOM syft (SPDX JSON) на каждую платформу, `cosign attest --type spdxjson` к digest
     платформы и файлом в релизе;
   - `release.json`: версия, коммит, digest образов, поддерживаемые и устаревшие
     `schema_version`, ревизии MCP, голова миграций; подпись `cosign sign-blob` (bundle
     `release.json.sigstore.json`);
   - GitHub Release с разделом CHANGELOG, `release.json`, его bundle и SBOM.
   Новых GitHub Actions нет: `docker`, `buildx` и `gh` есть на раннере, cosign и syft ставятся
   бинарниками с sha256 в workflow (как uv, ADR-0016, п. 4). Права `packages: write`,
   `id-token: write`, `contents: write` только у job публикации и только на теге.
6. **Закрепление в инсталляции.** `core/` остаётся submodule на теге (скрипты, compose,
   политики, инструменты). `antec-instance core-pin` скачивает `release.json` версии
   `core.version`, проверяет bundle (`cosign verify-blob` с identity п. 5) и пишет
   `core-images.yaml`. `render` по нему создаёт override compose (`image: …@sha256`,
   `build: !reset null`, `pull_policy: missing`). `pincheck --core-images core-images.yaml`
   требует, чтобы образы `ghcr.io/ahokkonen/antec-*` имели тег = `core.version` и digest из
   файла. Без `core-images.yaml` (ядро, `instances/dev`) образы собираются локально, как сейчас.
7. **Обновление инсталляции:** `core/` на новый тег → `core.version` и манифест по разделу
   «Обновление» → `core-pin` → `validate` → `up.sh`. `up.sh` отказывается работать, если голова
   миграций в БД неизвестна коду (понижение версии), и делает `backup.sh` перед любыми
   ожидающими миграциями (`--no-backup` только для стенда разработки). После: `audit-verify.sh`
   и smoke. CI ядра (job `upgrade`): миграции и `audit.sql` предыдущего релизного тега,
   тестовые данные, затем миграции и `audit.sql` текущего кода, проверка цепочки аудита.
   `restore-verify.sh` в CI не запускается (Связка ключей macOS): он входит в чек-лист релиза.
8. **N и N-1.** Ветка `release/0.N` создаётся при первом патче линии. Security-исправление
   сначала в main, затем cherry-pick в `release/0.N` и `release/0.(N-1)` и патч-теги.
   Инсталляция на версии старше N-1 обязана обновиться (ADR-0005, п. 4).
9. **Совместимость плагинов.** Плагин не объявляет диапазон версий ядра: совместимость задают
   ревизия MCP (`release.json`, `mcp_protocol_versions`) и `schema_version` манифеста. Ядро
   поддерживает ревизию MCP не меньше двух minor после того, как появилась более новая.
10. **Чек-лист релиза** (`tools/release/README.md`): `bump` в PR, мёрж, `restore-verify.sh` на
    стенде, аннотированный тег, проверка `cosign verify` и `release.json`.

## Последствия

- Плюс: одна команда меняет версию везде, CI ловит расхождения.
- Плюс: инсталляция получает образы ядра по digest с проверяемой подписью, SBOM и provenance,
  без новых GitHub Actions.
- Плюс: понижение версии и миграция без бэкапа больше невозможны через `up.sh`.
- Минус: CHANGELOG пишется руками, его наполнение проверяет только ревью.
- Минус: keyless-подпись зависит от публичного Sigstore (Fulcio, Rekor); identity и время
  релиза публичны (репозиторий и так публичный).
- Минус: GHCR не даёт неизменяемых тегов; доверие идёт только по digest из подписанного
  `release.json`.
- Минус: подписи в теге `sha256-<hex>`: несколько аттестаций к одному digest пишутся
  последовательно в одном job.
- Минус: версии docker и buildx задаёт образ раннера; BuildKit закреплён по digest.
- Сделать (после первого публикования): сделать пакеты GHCR публичными (настройка владельца);
  ruleset на теги `v*`.
- Отложено до второй инсталляции (пилот): автоматический PR с новой версией ядра во все
  `instance-<org>`, отчёт о версиях инсталляций, реестр образов плагинов инсталляции
  (`pincheck` с несколькими `--policy`, ADR-0008).
- Отложено до продакшна: зеркало реестра образов (ADR-0004, п. 7), подписанный бандл вместо
  submodule (ADR-0008, п. 6).

## План выхода (как заменить)

Граница: OCI-образы по digest, подписи и аттестации Sigstore, `release.json` как документированный
JSON. Замена GHCR на другой OCI-реестр меняет имя образа и `docker login`. Замена keyless на
ключ меняет `cosign sign` и проверку в `core-pin`. Замена ручного CHANGELOG на release-please не
трогает инсталляции. Оценка: 1-2 дня на каждую замену.

## Допущения и открытые вопросы

- Допущение: один сопровождающий создаёт теги вручную; подпись git-тегов не нужна, доверие
  идёт от identity workflow.
- Допущение: `restore-verify.sh` перед каждым релизом достаточно для проверки формата бэкапа,
  пока CI не умеет распечатывать OpenBao без Связки ключей.
- [ ] Перейти на cosign 3.1.6 после 2026-12-02 (или раньше, если появится advisory).
- [ ] Проверка подписи образов при `pull` на сервере (policy-controller или проверка в `up.sh`).
- [ ] Автоматический PR в репозитории инсталляций и отчёт о версиях (пилот).

## Ссылки

- https://github.com/sigstore/cosign/releases/tag/v3.1.4
- https://github.com/sigstore/cosign/security/advisories/GHSA-fx35-mq7g-6g98
- https://github.com/anchore/syft/releases/tag/v1.57.2
- https://github.com/moby/buildkit/releases/tag/v0.35.0
- https://blog.sigstore.dev/cosign-3-0-available/
- https://docs.docker.com/build/ci/github-actions/multi-platform/
- https://docs.github.com/en/actions/concepts/security/github_token
- https://semver.org/spec/v2.0.0.html
- https://keepachangelog.com/ru/1.1.0/
