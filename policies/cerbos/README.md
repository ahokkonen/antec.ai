# Политики Cerbos

Базовые политики Platform API ([ADR-0009](../../docs/adr/0009-authorization-cerbos.md)), без scope. Всё,
что не разрешено явно, запрещено. Инсталляция дополняет и переопределяет их своими политиками в
scope = id инсталляции ([ADR-0008](../../docs/adr/0008-plugin-contract-and-instance-template.md));
вместе с политиками инсталляции: `deploy/compose/scripts/policies-check.sh`.

| Ресурс | Действия | Кто |
| --- | --- | --- |
| `model` (id = логическое имя модели) | `invoke` | `platform-user`: `chat-default`, `chat-fast`; `platform-admin`: любая |

Роли: роли клиента `platform-api` в Keycloak, в токене claim `platform_roles`. Роль `user` не
использовать: её Cerbos подставляет субъекту без ролей (значение `subject.type` в AuthZEN).

Проверка политик и тестов из `tests/` (CI делает то же):

```bash
docker run --rm -u 65534:65534 -v "$PWD/policies/cerbos:/policies:ro" ghcr.io/cerbos/cerbos:0.57.0@sha256:e5e3b150b3cb0090975605254f255164567d67e233634199fb20972e3eaff300 compile /policies
```
