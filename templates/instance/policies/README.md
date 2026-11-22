# Политики инсталляции

Политики Cerbos поверх базовых политик ядра (`core/policies/cerbos`, ADR-0008, п. 4).

- Каждая `resourcePolicy` и `principalPolicy` здесь: `scope: __INSTANCE_ID__` и
  `scopePermissions: SCOPE_PERMISSIONS_OVERRIDE_PARENT`. Политики без scope принадлежат ядру.
- Правило инсталляции, которое сработало, решает; что не переопределено, решает ядро.
  Так можно и расширить, и сузить права ядра: изменения проходят ревью PR.
- Тесты рядом (`tests/*_test.yaml`), ресурсы в тестах со `scope: __INSTANCE_ID__`.

Пример: модель `chat-strong` только для роли `finance` (роль клиента `platform-api` в Keycloak).

```yaml
apiVersion: api.cerbos.dev/v1
resourcePolicy:
  version: default
  resource: model
  scope: __INSTANCE_ID__
  scopePermissions: SCOPE_PERMISSIONS_OVERRIDE_PARENT
  rules:
    - name: finance-strong-model
      actions: [invoke]
      effect: EFFECT_ALLOW
      roles: [finance]
      condition:
        match:
          expr: R.id == "chat-strong"
```

Проверка вместе с политиками ядра:

```bash
core/deploy/compose/scripts/policies-check.sh .
```
