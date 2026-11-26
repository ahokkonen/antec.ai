# tools/release

Релизы ядра antec.ai (ADR-0014): единая версия из `VERSION`, проверка CHANGELOG, матрица сборки
образов и `release.json`. Только стандартная библиотека; dev-зависимости те же, что у pincheck.

```bash
uv run --project tools/release --locked antec-release check
```

| Команда | Что делает |
| --- | --- |
| `check [--tag vX.Y.Z]` | Версия во всех местах (pyproject, `uv.lock`, `__version__`, Dockerfile, compose, README стенда, `instances/dev`) = `VERSION`; структура `CHANGELOG.md`. С `--tag`: тег = `v` + `VERSION`, раздел версии есть, с датой и записями |
| `bump X.Y.Z [--date ГГГГ-ММ-ДД]` | Переписывает все места версии и `VERSION`, переносит `[Не выпущено]` в `[X.Y.Z] - дата`. Версия должна быть больше текущей; исключение: первый релиз 0.1.0 |
| `notes X.Y.Z` | Раздел CHANGELOG версии (текст GitHub Release) |
| `images` | Матрица сборки `release.yml`: образ, каталог сборки, репозиторий GHCR, платформа, раннер |
| `manifest --commit --digests --compat --out` | Собирает `release.json` из digest индексов, вывода `antec-instance core-info` и головы миграций |

Код выхода: 0 успех, 1 проверка не прошла, 2 ошибка входных данных.

## release.json

```json
{
  "schema_version": 1,
  "core_version": "0.1.0",
  "repository": "https://github.com/ahokkonen/antec.ai",
  "commit": "<SHA коммита тега>",
  "images": {
    "platform-api": {"repository": "ghcr.io/ahokkonen/antec-platform-api", "tag": "0.1.0", "digest": "sha256:…"}
  },
  "manifest_schema_versions": {"supported": [1, 2, 3, 4, 5], "deprecated": [1, 2, 3, 4]},
  "mcp_protocol_versions": ["2026-07-28"],
  "migration_head": "0006"
}
```

Подпись: `release.json.sigstore.json` (cosign keyless, identity
`https://github.com/ahokkonen/antec.ai/.github/workflows/release.yml@refs/tags/vX.Y.Z`, issuer
`https://token.actions.githubusercontent.com`).

## Чек-лист релиза

1. Ветка `release-X.Y.Z`: проверить раздел `[Не выпущено]` (несовместимые изменения и
   «Обновление», если есть миграции или новая `schema_version`), затем:

   ```bash
   uv run --project tools/release --locked antec-release bump X.Y.Z
   ```

   PR, зелёный CI, мёрж.
2. На стенде из main: `deploy/compose/scripts/backup.sh`, `deploy/compose/scripts/restore-verify.sh`
   и `deploy/compose/scripts/platform-smoke.sh`.
3. Аннотированный тег на коммите мёржа и push:

   ```bash
   git tag -a vX.Y.Z -m "antec.ai X.Y.Z"
   ```

   ```bash
   git push origin vX.Y.Z
   ```

4. `release.yml` собирает и публикует образы, подписывает их, прикладывает SBOM и создаёт
   GitHub Release. Проверить подпись образа:

   ```bash
   cosign verify ghcr.io/ahokkonen/antec-platform-api@sha256:<digest> --certificate-identity https://github.com/ahokkonen/antec.ai/.github/workflows/release.yml@refs/tags/vX.Y.Z --certificate-oidc-issuer https://token.actions.githubusercontent.com
   ```

5. Только после первой публикации: пакеты `antec-*` в GHCR сделать публичными (Package settings).

## Патч для N-1

Ветка `release/0.N` создаётся от тега `v0.N.<последний>` при первом патче этой линии.
Исправление идёт в main, затем `git cherry-pick -x` в `release/0.N` (и `release/0.(N-1)`),
`bump` патч-версии в ветке, тег на ветке. Поддерживаются только N и N-1 (ADR-0005, п. 4).
