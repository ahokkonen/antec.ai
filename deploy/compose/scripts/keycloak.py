#!/usr/bin/env python3
"""Настройка и проверка Keycloak локального стенда через Admin API (ADR-0007).

Только стандартная библиотека: скрипт запускается на хосте из scripts/up.sh.
Секреты приходят JSON-объектом через stdin, не через argv и не через окружение.

    keycloak.py apply [--recreate-realm]   идемпотентная настройка master и realm antec
    keycloak.py open-webui-identities      администраторы и antec_uid для сверки Open WebUI
    keycloak.py smoke                      вход через браузер и обмен токенов (RFC 8693)
    keycloak.py user-token                 вход через браузер, access token на stdout

Realm как код: deploy/compose/keycloak/realm-antec.json создаётся, если realm нет.
Существующие объекты realm файл не меняет (недостающие создаются): локально realm пересоздаётся
(`up.sh --recreate-realm`), сверка с дрейфом на сервере решается отдельно (ADR-0007).
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import http.server
import json
import re
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

BASE_URL = "http://127.0.0.1:8080"
REALM = "antec"
KEYCLOAK_DIR = Path(__file__).resolve().parent.parent / "keycloak"
BOOTSTRAP_CLIENT = "antec-bootstrap"
AUTOMATION_CLIENT = "antec-automation"
ADMIN_USER = "antec-admin"
BREAKGLASS_USER = "breakglass"
IDP_ALIAS = "entra"
ADMIN_API_SCOPE = "kc-admin-api"
OTP_BROWSER_FLOW = "antec browser"
DEFAULT_SA_SCOPES = ("service_account", "web-origins", "acr", "profile", "roles", "basic", "email")
# Атрибуты, которые брокер Entra пишет в профиль пользователя (oid, tid).
# antec_uid: стабильный идентификатор пользователя, не меняется при пересоздании realm
# (Entra: oid, локальные пользователи: логин). По нему Open WebUI находит аккаунт (OAUTH_SUB_CLAIM).
PROFILE_ATTRIBUTES = ("entra_oid", "entra_tid", "antec_uid")
UID_ATTRIBUTE = "antec_uid"
OPEN_WEBUI_CLIENT = "open-webui"
# Grafana (ADR-0013): роли клиента viewer и admin; ролей по умолчанию нет, без роли вход запрещён.
GRAFANA_CLIENT = "grafana"
GRAFANA_ROLES = ("viewer", "admin")
# Срок access token клиента open-webui, секунды. Open WebUI обновляет токен, когда до истечения
# меньше 5 минут: при сроке realm в 5 минут обновление шло бы перед каждым запросом (ADR-0006).
OPEN_WEBUI_ACCESS_LIFESPAN = "900"
# Роли клиента open-webui: user (доступ, есть у всех через роли по умолчанию), admin (администратор
# Open WebUI, назначается явно). Open WebUI берёт их из claim roles (ENABLE_OAUTH_ROLE_MANAGEMENT).
OPEN_WEBUI_ROLES = ("user", "admin")
# Роли Platform API (ADR-0009): клиентские роли platform-api; platform-user у всех через роли realm
# по умолчанию, platform-admin назначается явно. В токены клиентов с client scope platform-api
# (open-webui, antec-smoke) они попадают claim'ом platform_roles.
PLATFORM_CLIENT = "platform-api"
PLATFORM_ROLES = ("platform-user", "platform-admin")
PLATFORM_DEFAULT_ROLE = "platform-user"
PLATFORM_ROLES_CLAIM = "platform_roles"
PROFILE_PERMISSIONS: dict[str, list[str]] = {"view": ["admin"], "edit": ["admin"]}
SMOKE_REDIRECT_PORT = 8765

type Json = dict[str, Any]


class KeycloakError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"\033[1m[antec]\033[0m keycloak: {msg}", file=sys.stderr)


def _request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: Json | list[Any] | None = None,
    form: dict[str, str] | None = None,
) -> tuple[int, Any, dict[str, str]]:
    headers = {"Accept": "application/json"}
    data: bytes | None = None
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    # Только HTTP к Keycloak стенда: схема и хост фиксированы константой BASE_URL.
    if not url.startswith(f"{BASE_URL}/"):
        raise KeycloakError(f"неожиданный адрес запроса: {url}")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            raw = resp.read()
            status = resp.status
            resp_headers = dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
        resp_headers = dict(exc.headers.items())
    payload: Any = None
    if raw:
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw.decode(errors="replace")
    return status, payload, resp_headers


def token_request(realm: str, form: dict[str, str]) -> tuple[int, Any]:
    status, payload, _ = _request(
        "POST", f"{BASE_URL}/realms/{realm}/protocol/openid-connect/token", form=form
    )
    return status, payload


def client_token(realm: str, client_id: str, secret: str) -> str | None:
    status, payload = token_request(
        realm,
        {"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret},
    )
    if status == 200 and isinstance(payload, dict):
        return cast(str, payload["access_token"])
    return None


def jwt_claims(token: str) -> Json:
    part = token.split(".")[1]
    return cast(Json, json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))))


@dataclass
class Admin:
    """Клиент Admin API с токеном служебного клиента realm master."""

    token: str

    # Ответ Admin API: произвольный JSON, структуру проверяют вызывающие.
    def call(self, method: str, path: str, body: Json | list[Any] | None = None) -> Any:  # noqa: ANN401
        status, payload, headers = _request(
            method, f"{BASE_URL}/admin/realms{path}", token=self.token, body=body
        )
        if status >= 400:
            raise KeycloakError(f"{method} {path}: HTTP {status}: {payload}")
        if status == 201 and "Location" in headers:
            return headers["Location"].rsplit("/", 1)[-1]
        return payload

    def find_client(self, realm: str, client_id: str) -> Json | None:
        found = self.call("GET", f"/{realm}/clients?clientId={urllib.parse.quote(client_id)}")
        return cast(Json, found[0]) if found else None

    def find_user(self, realm: str, username: str) -> Json | None:
        q = urllib.parse.quote(username)
        found = self.call("GET", f"/{realm}/users?username={q}&exact=true")
        return cast(Json, found[0]) if found else None

    def realm_exists(self, realm: str) -> bool:
        status, _, _ = _request("GET", f"{BASE_URL}/admin/realms/{realm}", token=self.token)
        if status == 404:
            return False
        if status >= 400:
            raise KeycloakError(f"GET /{realm}: HTTP {status}")
        return True


def _secret(secrets_in: Json, key: str) -> str:
    value = secrets_in.get(key)
    if not isinstance(value, str) or not value:
        raise KeycloakError(f"во входных данных нет секрета {key!r}")
    return value


def connect(secrets_in: Json) -> Admin:
    """Токен служебного клиента: постоянного, а при первом запуске временного bootstrap."""
    for client_id, key in (
        (AUTOMATION_CLIENT, "automation_client_secret"),
        (BOOTSTRAP_CLIENT, "bootstrap_client_secret"),
    ):
        token = client_token("master", client_id, _secret(secrets_in, key))
        if token is None:
            continue
        status, _, _ = _request("GET", f"{BASE_URL}/admin/realms/master", token=token)
        if status == 200:
            return Admin(token)
    raise KeycloakError(
        "не удалось получить токен ни постоянного, ни bootstrap-клиента realm master"
    )


def ensure_master(admin: Admin, secrets_in: Json) -> Admin:
    """Постоянный служебный клиент и администратор master; удаление bootstrap-клиента."""
    automation_secret = _secret(secrets_in, "automation_client_secret")
    client = admin.find_client("master", AUTOMATION_CLIENT)
    if client is None:
        log(f"создание служебного клиента {AUTOMATION_CLIENT} в realm master")
        admin.call(
            "POST",
            "/master/clients",
            {
                "clientId": AUTOMATION_CLIENT,
                "name": "Автоматизация стенда (scripts/keycloak.py)",
                "enabled": True,
                "publicClient": False,
                "clientAuthenticatorType": "client-secret",
                "secret": automation_secret,
                "standardFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": True,
                # Роли в токене только через явный scope mapping (ниже), не full scope.
                "fullScopeAllowed": False,
                # С 26.8.2 Admin API принимает только токены с аудиторией Admin API,
                # которую добавляет scope kc-admin-api.
                "defaultClientScopes": [*DEFAULT_SA_SCOPES, ADMIN_API_SCOPE],
            },
        )
        client = admin.find_client("master", AUTOMATION_CLIENT)
        if client is None:
            raise KeycloakError(f"клиент {AUTOMATION_CLIENT} не найден после создания")
    elif client.get("secret") != automation_secret or client.get("fullScopeAllowed"):
        client["secret"] = automation_secret
        client["fullScopeAllowed"] = False
        admin.call("PUT", f"/master/clients/{client['id']}", client)
    # Роль admin проверяется при каждом запуске: прерванный первый запуск (клиент создан,
    # роль не назначена) восстанавливается повторным apply, пока bootstrap-клиент доступен.
    sa = admin.call("GET", f"/master/clients/{client['id']}/service-account-user")
    ensure_realm_role(admin, "master", sa["id"], "admin")
    scoped = admin.call("GET", f"/master/clients/{client['id']}/scope-mappings/realm")
    if not any(r["name"] == "admin" for r in scoped):
        role = admin.call("GET", "/master/roles/admin")
        admin.call("POST", f"/master/clients/{client['id']}/scope-mappings/realm", [role])
    if ADMIN_API_SCOPE not in (client.get("defaultClientScopes") or []):
        scope_id = next(
            sc["id"]
            for sc in admin.call("GET", "/master/client-scopes")
            if sc["name"] == ADMIN_API_SCOPE
        )
        admin.call("PUT", f"/master/clients/{client['id']}/default-client-scopes/{scope_id}")

    token = client_token("master", AUTOMATION_CLIENT, automation_secret)
    if token is None:
        raise KeycloakError(f"клиент {AUTOMATION_CLIENT} создан, но токен не выдаётся")
    admin = Admin(token)
    # Проверка, что токен принимает Admin API (в 26.8.2 проверяется аудитория токена).
    admin.call("GET", "/master")

    user = admin.find_user("master", ADMIN_USER)
    if user is None:
        log(f"создание администратора {ADMIN_USER} (realm master, TOTP обязателен)")
        admin.call(
            "POST",
            "/master/users",
            {
                "username": ADMIN_USER,
                "enabled": True,
                "email": "antec-admin@antec.invalid",
                "emailVerified": True,
                "firstName": "antec",
                "lastName": "admin",
                "requiredActions": ["CONFIGURE_TOTP"],
                "credentials": [
                    {
                        "type": "password",
                        "value": _secret(secrets_in, "admin_password"),
                        "temporary": False,
                    }
                ],
            },
        )
        user = admin.find_user("master", ADMIN_USER)
        if user is None:
            raise KeycloakError(f"пользователь {ADMIN_USER} не найден после создания")
    # Роль и обязательный OTP проверяются при каждом запуске и до удаления bootstrap-клиента.
    ensure_realm_role(admin, "master", user["id"], "admin")
    ensure_otp_browser_flow(admin, "master")

    bootstrap = admin.find_client("master", BOOTSTRAP_CLIENT)
    if bootstrap is not None:
        log(f"удаление временного bootstrap-клиента {BOOTSTRAP_CLIENT}")
        admin.call("DELETE", f"/master/clients/{bootstrap['id']}")
    return admin


def ensure_realm_role(admin: Admin, realm: str, user_id: str, role_name: str) -> None:
    mapped = admin.call("GET", f"/{realm}/users/{user_id}/role-mappings/realm")
    if not any(r["name"] == role_name for r in mapped):
        role = admin.call("GET", f"/{realm}/roles/{role_name}")
        admin.call("POST", f"/{realm}/users/{user_id}/role-mappings/realm", [role])


def ensure_otp_browser_flow(admin: Admin, realm: str) -> None:
    """Вход по паролю всегда требует OTP: копия browser flow, где шаг 2FA обязателен.

    В стандартном flow OTP условный (только если настроен), поэтому удаление второго фактора
    через account console оставляет вход по одному паролю. Здесь OTP Form обязателен: без
    настроенного OTP Keycloak требует его настройку. Вход через брокер (Entra) этот шаг не
    проходит: MFA там обеспечивает внешний IdP.
    """
    flows = admin.call("GET", f"/{realm}/authentication/flows")
    if not any(f["alias"] == OTP_BROWSER_FLOW for f in flows):
        log(f"realm {realm}: browser flow с обязательным OTP ({OTP_BROWSER_FLOW})")
        admin.call(
            "POST", f"/{realm}/authentication/flows/browser/copy", {"newName": OTP_BROWSER_FLOW}
        )
    path = f"/{realm}/authentication/flows/{urllib.parse.quote(OTP_BROWSER_FLOW)}/executions"
    executions = cast(list[Json], admin.call("GET", path))
    otp_index = next(
        (i for i, e in enumerate(executions) if e.get("providerId") == "auth-otp-form"), None
    )
    if otp_index is None:
        raise KeycloakError(f"realm {realm}: в {OTP_BROWSER_FLOW} нет OTP Form")
    otp_level = int(executions[otp_index]["level"])
    parent_index = next(
        i for i in range(otp_index - 1, -1, -1) if int(executions[i]["level"]) == otp_level - 1
    )
    wanted: dict[str, str] = {executions[parent_index]["id"]: "REQUIRED"}
    for e in executions[parent_index + 1 :]:
        if int(e["level"]) < otp_level:
            break
        if int(e["level"]) == otp_level:
            wanted[e["id"]] = "REQUIRED" if e.get("providerId") == "auth-otp-form" else "DISABLED"
    for e in executions:
        if e["id"] in wanted and e["requirement"] != wanted[e["id"]]:
            admin.call("PUT", path, {**e, "requirement": wanted[e["id"]]})
    if admin.call("GET", f"/{realm}")["browserFlow"] != OTP_BROWSER_FLOW:
        admin.call("PUT", f"/{realm}", {"browserFlow": OTP_BROWSER_FLOW})


# Сроки сессии realm, которые up.sh держит равными realm-antec.json и на существующем realm.
# Простой SSO-сессии = сроку сессии Open WebUI (JWT_EXPIRES_IN в compose.yaml, 8 часов): иначе
# после простоя Open WebUI остаётся открытым без токена Keycloak и не видит моделей.
REALM_SESSION_KEYS = ("ssoSessionIdleTimeout", "ssoSessionMaxLifespan")


def ensure_realm_sessions(admin: Admin, spec: Json) -> None:
    current = admin.call("GET", f"/{REALM}")
    changed = {k: spec[k] for k in REALM_SESSION_KEYS if current.get(k) != spec[k]}
    if changed:
        log(f"realm {REALM}: сроки сессии {changed}")
        admin.call("PUT", f"/{REALM}", changed)


def ensure_event_retention(admin: Admin, secrets_in: Json) -> None:
    """Срок событий входа и админ-событий = retention.audit_days манифеста (ADR-0012, п. 1).

    Атрибуты передаются целиком: PUT с частью атрибутов удаляет остальные.
    """
    seconds = int(secrets_in["audit_days"]) * 86400
    current = admin.call("GET", f"/{REALM}")
    attributes = dict(current.get("attributes") or {})
    if current.get("eventsExpiration") == seconds and attributes.get(
        "adminEventsExpiration"
    ) == str(seconds):
        return
    log(f"realm {REALM}: срок событий {secrets_in['audit_days']} дней")
    attributes["adminEventsExpiration"] = str(seconds)
    admin.call("PUT", f"/{REALM}", {"eventsExpiration": seconds, "attributes": attributes})


def ensure_realm(admin: Admin, secrets_in: Json) -> None:
    """Создаёт realm и недостающие объекты из realm-antec.json; существующие не меняет.

    Повторный запуск после прерванного первого достраивает realm (scopes, клиенты,
    breakglass), но не перезаписывает объекты, которые уже есть. Исключение: сроки сессии
    (REALM_SESSION_KEYS) приводятся к realm-antec.json и на существующем realm.
    """
    spec = cast(Json, json.loads((KEYCLOAK_DIR / "realm-antec.json").read_text()))
    scopes = cast(list[Json], spec.pop("clientScopes", []))
    clients = cast(list[Json], spec.pop("clients", []))
    if not admin.realm_exists(REALM):
        log(f"создание realm {REALM} из keycloak/realm-antec.json")
        admin.call("POST", "", spec)
    ensure_realm_sessions(admin, spec)
    ensure_event_retention(admin, secrets_in)
    have_scopes = {sc["name"] for sc in admin.call("GET", f"/{REALM}/client-scopes")}
    for scope in scopes:
        if scope["name"] not in have_scopes:
            admin.call("POST", f"/{REALM}/client-scopes", scope)
    client_secrets = client_secret_map(secrets_in)
    for client in clients:
        if admin.find_client(REALM, client["clientId"]) is None:
            client = copy.deepcopy(client)
            if client["clientId"] in client_secrets:
                client["secret"] = client_secrets[client["clientId"]]
            admin.call("POST", f"/{REALM}/clients", client)
    ensure_user_profile(admin)
    ensure_client_secrets(admin, secrets_in)
    ensure_otp_browser_flow(admin, REALM)
    ensure_open_webui_access(admin)
    ensure_grafana_access(admin)
    ensure_platform_roles(admin)
    if admin.find_user(REALM, BREAKGLASS_USER) is None:
        log(f"создание аварийного пользователя {BREAKGLASS_USER} (TOTP обязателен)")
        admin.call(
            "POST",
            f"/{REALM}/users",
            {
                "username": BREAKGLASS_USER,
                "enabled": True,
                "email": "breakglass@antec.invalid",
                "emailVerified": True,
                "firstName": "Аварийный",
                "lastName": "вход",
                "attributes": {UID_ATTRIBUTE: [BREAKGLASS_USER]},
                "requiredActions": ["CONFIGURE_TOTP"],
                "credentials": [
                    {
                        "type": "password",
                        "value": _secret(secrets_in, "breakglass_password"),
                        "temporary": False,
                    }
                ],
            },
        )
    breakglass = admin.find_user(REALM, BREAKGLASS_USER)
    if breakglass is None:
        raise KeycloakError(f"пользователь {BREAKGLASS_USER} не найден после создания")
    attrs = cast(Json, breakglass.get("attributes") or {})
    if attrs.get(UID_ATTRIBUTE) != [BREAKGLASS_USER]:
        breakglass["attributes"] = {**attrs, UID_ATTRIBUTE: [BREAKGLASS_USER]}
        admin.call("PUT", f"/{REALM}/users/{breakglass['id']}", breakglass)
    # Аварийный пользователь администрирует и Open WebUI.
    ensure_client_role_mapping(admin, breakglass["id"], OPEN_WEBUI_CLIENT, "admin")
    ensure_client_role_mapping(admin, breakglass["id"], GRAFANA_CLIENT, "admin")
    ensure_local_uids(admin)


def realm_users(admin: Admin) -> list[Json]:
    users: list[Json] = []
    first = 0
    while True:
        page = cast(
            list[Json],
            admin.call("GET", f"/{REALM}/users?first={first}&max=200&briefRepresentation=false"),
        )
        users.extend(page)
        if len(page) < 200:
            return users
        first += 200


def ensure_local_uids(admin: Admin) -> None:
    """antec_uid = логин для локальных пользователей без атрибута.

    Без antec_uid вход в Open WebUI невозможен (OAUTH_SUB_CLAIM). Пользователям брокеров атрибут
    пишет маппер брокера (Entra: oid) при каждом входе, их не трогаем.
    """
    for user in realm_users(admin):
        attrs = cast(Json, user.get("attributes") or {})
        if attrs.get(UID_ATTRIBUTE):
            continue
        if admin.call("GET", f"/{REALM}/users/{user['id']}/federated-identity"):
            continue
        log(f"{UID_ATTRIBUTE} для локального пользователя {user['username']}")
        user["attributes"] = {**attrs, UID_ATTRIBUTE: [user["username"]]}
        admin.call("PUT", f"/{REALM}/users/{user['id']}", user)


def ensure_client_role_mapping(admin: Admin, user_id: str, client_id: str, role: str) -> None:
    client = admin.find_client(REALM, client_id)
    if client is None:
        raise KeycloakError(f"в realm {REALM} нет клиента {client_id}")
    base = f"/{REALM}/users/{user_id}/role-mappings/clients/{client['id']}"
    if not any(r["name"] == role for r in admin.call("GET", base)):
        rep_ = admin.call("GET", f"/{REALM}/clients/{client['id']}/roles/{role}")
        admin.call("POST", base, [rep_])


def ensure_open_webui_access(admin: Admin) -> None:
    """Роли и claims клиента open-webui (ADR-0018).

    - роли клиента user и admin; user входит в роли realm по умолчанию: доступ к Open WebUI есть
      у каждого, кто вошёл в realm (доступ к realm ограничивает Entra, «Assignment required»);
    - claim roles (роли клиента open-webui) и antec_uid в токенах и userinfo клиента open-webui;
    - срок access token клиента 15 минут (OPEN_WEBUI_ACCESS_LIFESPAN).
    Администратор Open WebUI определяется ролью admin при каждом входе, а не порядком входа.
    """
    client = admin.find_client(REALM, OPEN_WEBUI_CLIENT)
    if client is None:
        raise KeycloakError(f"в realm {REALM} нет клиента {OPEN_WEBUI_CLIENT}")
    cid = client["id"]
    attrs = cast(Json, client.get("attributes") or {})
    if attrs.get("access.token.lifespan") != OPEN_WEBUI_ACCESS_LIFESPAN:
        log(f"срок access token клиента {OPEN_WEBUI_CLIENT}: {OPEN_WEBUI_ACCESS_LIFESPAN} с")
        client["attributes"] = {**attrs, "access.token.lifespan": OPEN_WEBUI_ACCESS_LIFESPAN}
        admin.call("PUT", f"/{REALM}/clients/{cid}", client)
    have_roles = {r["name"] for r in admin.call("GET", f"/{REALM}/clients/{cid}/roles")}
    for role in OPEN_WEBUI_ROLES:
        if role not in have_roles:
            admin.call(
                "POST",
                f"/{REALM}/clients/{cid}/roles",
                {"name": role, "description": f"Open WebUI: {role}"},
            )
    default_roles = f"/{REALM}/roles/default-roles-{REALM}/composites"
    composites = admin.call("GET", f"{default_roles}/clients/{cid}")
    if not any(r["name"] == "user" for r in composites):
        user_role = admin.call("GET", f"/{REALM}/clients/{cid}/roles/user")
        admin.call("POST", default_roles, [user_role])
    wanted = {
        "roles": {
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-client-role-mapper",
            "config": {
                "usermodel.clientRoleMapping.clientId": OPEN_WEBUI_CLIENT,
                "claim.name": "roles",
                "multivalued": "true",
                "jsonType.label": "String",
                "id.token.claim": "true",
                "access.token.claim": "true",
                "userinfo.token.claim": "true",
                "introspection.token.claim": "true",
            },
        },
        UID_ATTRIBUTE: {
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-attribute-mapper",
            "config": {
                "user.attribute": UID_ATTRIBUTE,
                "claim.name": UID_ATTRIBUTE,
                "jsonType.label": "String",
                "id.token.claim": "true",
                "access.token.claim": "true",
                "userinfo.token.claim": "true",
                "introspection.token.claim": "true",
            },
        },
    }
    mappers = {
        m["name"]: m for m in admin.call("GET", f"/{REALM}/clients/{cid}/protocol-mappers/models")
    }
    for name, spec in wanted.items():
        current = mappers.get(name)
        if current is None:
            admin.call(
                "POST", f"/{REALM}/clients/{cid}/protocol-mappers/models", {"name": name, **spec}
            )
        elif current.get("config") != spec["config"]:
            admin.call(
                "PUT",
                f"/{REALM}/clients/{cid}/protocol-mappers/models/{current['id']}",
                {**current, **spec},
            )


def ensure_grafana_access(admin: Admin) -> None:
    """Роли клиента grafana и claim roles в userinfo (ADR-0013, п. 5).

    Ролей по умолчанию нет: viewer и admin назначаются явно (админ-консоль Keycloak, Clients ->
    grafana -> Roles). Grafana берёт роль из claim roles при каждом входе (role_attribute_strict).
    """
    client = admin.find_client(REALM, GRAFANA_CLIENT)
    if client is None:
        raise KeycloakError(f"в realm {REALM} нет клиента {GRAFANA_CLIENT}")
    cid = client["id"]
    have_roles = {r["name"] for r in admin.call("GET", f"/{REALM}/clients/{cid}/roles")}
    for role in GRAFANA_ROLES:
        if role not in have_roles:
            admin.call(
                "POST",
                f"/{REALM}/clients/{cid}/roles",
                {"name": role, "description": f"Grafana: {role}"},
            )
    # roles: роли клиента grafana; antec_uid: логин пользователя в Grafana (как у Open WebUI).
    wanted = {
        "roles": {
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-client-role-mapper",
            "config": {
                "usermodel.clientRoleMapping.clientId": GRAFANA_CLIENT,
                "claim.name": "roles",
                "multivalued": "true",
                "jsonType.label": "String",
                "id.token.claim": "true",
                "access.token.claim": "true",
                "userinfo.token.claim": "true",
                "introspection.token.claim": "true",
            },
        },
        UID_ATTRIBUTE: {
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-attribute-mapper",
            "config": {
                "user.attribute": UID_ATTRIBUTE,
                "claim.name": UID_ATTRIBUTE,
                "jsonType.label": "String",
                "id.token.claim": "true",
                "access.token.claim": "true",
                "userinfo.token.claim": "true",
                "introspection.token.claim": "true",
            },
        },
    }
    mappers = {
        m["name"]: m for m in admin.call("GET", f"/{REALM}/clients/{cid}/protocol-mappers/models")
    }
    for name, spec in wanted.items():
        current = mappers.get(name)
        if current is None:
            admin.call(
                "POST", f"/{REALM}/clients/{cid}/protocol-mappers/models", {"name": name, **spec}
            )
        elif current.get("config") != spec["config"]:
            admin.call(
                "PUT",
                f"/{REALM}/clients/{cid}/protocol-mappers/models/{current['id']}",
                {**current, **spec},
            )


def ensure_platform_roles(admin: Admin) -> None:
    """Роли Platform API и их доставка в токен (ADR-0009).

    - роли клиента platform-api (PLATFORM_ROLES); platform-user в ролях realm по умолчанию;
    - client scope platform-api: маппер ролей клиента platform-api в claim platform_roles и
      scope mapping этих ролей: у клиентов fullScopeAllowed выключен, без scope mapping роли
      чужого клиента в токен не попадают.
    """
    client = admin.find_client(REALM, PLATFORM_CLIENT)
    if client is None:
        raise KeycloakError(f"в realm {REALM} нет клиента {PLATFORM_CLIENT}")
    cid = client["id"]
    have = {r["name"] for r in admin.call("GET", f"/{REALM}/clients/{cid}/roles")}
    for role in PLATFORM_ROLES:
        if role not in have:
            log(f"роль клиента {PLATFORM_CLIENT}: {role}")
            admin.call(
                "POST",
                f"/{REALM}/clients/{cid}/roles",
                {"name": role, "description": f"Platform API: {role} (ADR-0009)"},
            )
    roles = {r: admin.call("GET", f"/{REALM}/clients/{cid}/roles/{r}") for r in PLATFORM_ROLES}
    default_roles = f"/{REALM}/roles/default-roles-{REALM}/composites"
    composites = admin.call("GET", f"{default_roles}/clients/{cid}")
    if not any(r["name"] == PLATFORM_DEFAULT_ROLE for r in composites):
        admin.call("POST", default_roles, [roles[PLATFORM_DEFAULT_ROLE]])

    scope = next(
        (
            sc
            for sc in admin.call("GET", f"/{REALM}/client-scopes")
            if sc["name"] == PLATFORM_CLIENT
        ),
        None,
    )
    if scope is None:
        raise KeycloakError(f"в realm {REALM} нет client scope {PLATFORM_CLIENT}")
    sid = scope["id"]
    mapped = admin.call("GET", f"/{REALM}/client-scopes/{sid}/scope-mappings/clients/{cid}")
    missing = [roles[r] for r in PLATFORM_ROLES if not any(m["name"] == r for m in mapped)]
    if missing:
        admin.call("POST", f"/{REALM}/client-scopes/{sid}/scope-mappings/clients/{cid}", missing)
    wanted_config: dict[str, str] = {
        "usermodel.clientRoleMapping.clientId": PLATFORM_CLIENT,
        "claim.name": PLATFORM_ROLES_CLAIM,
        "multivalued": "true",
        "jsonType.label": "String",
        "id.token.claim": "false",
        "access.token.claim": "true",
        "userinfo.token.claim": "false",
        "introspection.token.claim": "true",
    }
    wanted = {
        "protocol": "openid-connect",
        "protocolMapper": "oidc-usermodel-client-role-mapper",
        "config": wanted_config,
    }
    mappers = {
        m["name"]: m
        for m in admin.call("GET", f"/{REALM}/client-scopes/{sid}/protocol-mappers/models")
    }
    current = mappers.get(PLATFORM_ROLES_CLAIM)
    if current is None:
        admin.call(
            "POST",
            f"/{REALM}/client-scopes/{sid}/protocol-mappers/models",
            {"name": PLATFORM_ROLES_CLAIM, **wanted},
        )
    elif any(current.get("config", {}).get(k) != v for k, v in wanted_config.items()):
        # Сравниваются только наши ключи: Keycloak может добавлять в config свои.
        admin.call(
            "PUT",
            f"/{REALM}/client-scopes/{sid}/protocol-mappers/models/{current['id']}",
            {**current, **wanted},
        )


def client_secret_map(secrets_in: Json) -> dict[str, str]:
    return {
        "platform-api": _secret(secrets_in, "platform_api_client_secret"),
        "open-webui": _secret(secrets_in, "open_webui_client_secret"),
        "grafana": _secret(secrets_in, "grafana_client_secret"),
    }


def ensure_client_secrets(admin: Admin, secrets_in: Json) -> None:
    """Секреты клиентов берутся из OpenBao; расхождение исправляется."""
    for client_id, secret in client_secret_map(secrets_in).items():
        client = admin.find_client(REALM, client_id)
        if client is None:
            raise KeycloakError(f"в realm {REALM} нет клиента {client_id}; up.sh --recreate-realm")
        if client.get("secret") != secret:
            log(f"обновление секрета клиента {client_id} из OpenBao")
            client["secret"] = secret
            admin.call("PUT", f"/{REALM}/clients/{client['id']}", client)


def ensure_user_profile(admin: Admin) -> None:
    """Атрибуты Entra видны и меняются только администратором и брокером.

    Права приводятся к заданным при каждом запуске, а не только при создании атрибута:
    значения попадают в claims токена, и право пользователя их менять недопустимо.
    """
    profile = cast(Json, admin.call("GET", f"/{REALM}/users/profile"))
    attributes = cast(list[Json], profile.setdefault("attributes", []))
    by_name = {a["name"]: a for a in attributes}
    changed = False
    for name in PROFILE_ATTRIBUTES:
        attr = by_name.get(name)
        if attr is None:
            attributes.append(
                {
                    "name": name,
                    "displayName": name,
                    "permissions": dict(PROFILE_PERMISSIONS),
                    "multivalued": False,
                }
            )
            changed = True
        elif attr.get("permissions") != PROFILE_PERMISSIONS or attr.get("multivalued"):
            log(f"realm {REALM}: права атрибута {name} приведены к admin-only")
            attr["permissions"] = dict(PROFILE_PERMISSIONS)
            attr["multivalued"] = False
            changed = True
    if changed:
        admin.call("PUT", f"/{REALM}/users/profile", profile)


def render_idp(entra: Json) -> Json:
    tenant = str(entra.get("tenant_id", ""))
    client_id = str(entra.get("client_id", ""))
    for name, value in (("tenant_id", tenant), ("client_id", client_id)):
        if not value or not all(c.isalnum() or c == "-" for c in value):
            raise KeycloakError(f"antec/entra:{name} должен быть GUID")
    text = (KEYCLOAK_DIR / "idp-entra.json").read_text()
    text = text.replace("${ENTRA_TENANT_ID}", tenant).replace("${ENTRA_CLIENT_ID}", client_id)
    return cast(Json, json.loads(text))


def ensure_idp(admin: Admin, entra: Json | None) -> None:
    if not entra:
        log("Entra ID не настроен (scripts/set-entra.sh): вход только локальными пользователями")
        return
    spec = render_idp(entra)
    mappers = cast(list[Json], spec.pop("mappers"))
    status, current, _ = _request(
        "GET",
        f"{BASE_URL}/admin/realms/{REALM}/identity-provider/instances/{IDP_ALIAS}",
        token=admin.token,
    )
    if status == 404:
        log("создание брокера Entra ID")
        admin.call("POST", f"/{REALM}/identity-provider/instances", spec)
    elif status == 200 and isinstance(current, dict):
        spec["internalId"] = current.get("internalId")
        admin.call("PUT", f"/{REALM}/identity-provider/instances/{IDP_ALIAS}", spec)
    else:
        raise KeycloakError(f"брокер {IDP_ALIAS}: HTTP {status}")
    base = f"/{REALM}/identity-provider/instances/{IDP_ALIAS}/mappers"
    existing = {m["name"]: m for m in admin.call("GET", base)}
    for mapper in mappers:
        mapper = {**mapper, "identityProviderAlias": IDP_ALIAS}
        if mapper["name"] in existing:
            mapper["id"] = existing[mapper["name"]]["id"]
            admin.call("PUT", f"{base}/{mapper['id']}", mapper)
        else:
            admin.call("POST", base, mapper)


def cmd_open_webui_identities(secrets_in: Json) -> None:
    """Данные для сверки аккаунтов Open WebUI (scripts/up.sh), JSON на stdout.

    admins: antec_uid пользователей с ролью admin клиента open-webui (напрямую);
    uid_by_id: id пользователя Keycloak -> antec_uid (перенос аккаунтов, созданных по sub).
    """
    admin = connect(secrets_in)
    client = admin.find_client(REALM, OPEN_WEBUI_CLIENT)
    if client is None:
        raise KeycloakError(f"в realm {REALM} нет клиента {OPEN_WEBUI_CLIENT}")
    holders = cast(
        list[Json],
        admin.call("GET", f"/{REALM}/clients/{client['id']}/roles/admin/users?max=1000"),
    )
    uid_by_id: dict[str, str] = {}
    for user in realm_users(admin):
        uid = (cast(Json, user.get("attributes") or {}).get(UID_ATTRIBUTE) or [None])[0]
        if uid:
            uid_by_id[cast(str, user["id"])] = cast(str, uid)
    admins = sorted(uid_by_id[h["id"]] for h in holders if h["id"] in uid_by_id)
    print(json.dumps({"admins": admins, "uid_by_id": uid_by_id}))


# Как в схеме манифеста (tools/instance): только mcp-*, клиенты ядра аудиторией быть не могут.
_AUDIENCE_RE = re.compile(r"mcp-[a-z0-9][a-z0-9-]{0,59}")


def ensure_mcp_audiences(admin: Admin, audiences: list[str]) -> None:
    """Клиенты-аудитории MCP-серверов инсталляции (ADR-0010, п. 6; вопрос ADR-0008).

    Для каждой аудитории из mcp_servers манифеста: клиент без потоков (сам токены не получает),
    client scope с маппером аудитории и этот scope как optional у platform-api: Platform API
    получает токен с aud = аудитории обменом (RFC 8693, ADR-0007). Существующие объекты не
    меняются; аудитории, убранные из манифеста, не удаляются (их токены не выпускаются без
    scope у platform-api, который тоже остаётся: удаление вручную).
    """
    platform = admin.find_client(REALM, PLATFORM_CLIENT)
    if platform is None:
        raise KeycloakError(f"в realm {REALM} нет клиента {PLATFORM_CLIENT}")
    scopes = {sc["name"]: sc for sc in admin.call("GET", f"/{REALM}/client-scopes")}
    optional = {
        sc["name"]
        for sc in admin.call("GET", f"/{REALM}/clients/{platform['id']}/optional-client-scopes")
    }
    for audience in audiences:
        if not _AUDIENCE_RE.fullmatch(audience):
            raise KeycloakError(f"недопустимая аудитория MCP-сервера (нужно mcp-*): {audience!r}")
        if admin.find_client(REALM, audience) is None:
            log(f"клиент-аудитория MCP-сервера {audience}")
            admin.call(
                "POST",
                f"/{REALM}/clients",
                {
                    "clientId": audience,
                    "name": f"MCP {audience}",
                    "description": "Аудитория MCP-сервера инсталляции, токены не получает",
                    "enabled": True,
                    "protocol": "openid-connect",
                    "publicClient": False,
                    "clientAuthenticatorType": "client-secret",
                    "standardFlowEnabled": False,
                    "implicitFlowEnabled": False,
                    "directAccessGrantsEnabled": False,
                    "serviceAccountsEnabled": False,
                    "fullScopeAllowed": False,
                    "defaultClientScopes": [],
                    "optionalClientScopes": [],
                },
            )
        if audience not in scopes:
            log(f"client scope {audience} (аудитория для обмена токена)")
            admin.call(
                "POST",
                f"/{REALM}/client-scopes",
                {
                    "name": audience,
                    "description": f"Аудитория MCP-сервера {audience}: токен обменом (RFC 8693)",
                    "protocol": "openid-connect",
                    "attributes": {
                        "include.in.token.scope": "true",
                        "display.on.consent.screen": "false",
                    },
                    "protocolMappers": [
                        {
                            "name": f"aud-{audience}",
                            "protocol": "openid-connect",
                            "protocolMapper": "oidc-audience-mapper",
                            "config": {
                                "included.client.audience": audience,
                                "access.token.claim": "true",
                                "introspection.token.claim": "true",
                                "id.token.claim": "false",
                            },
                        }
                    ],
                },
            )
            scopes = {sc["name"]: sc for sc in admin.call("GET", f"/{REALM}/client-scopes")}
        if audience not in optional:
            log(f"{PLATFORM_CLIENT}: optional client scope {audience}")
            admin.call(
                "PUT",
                f"/{REALM}/clients/{platform['id']}/optional-client-scopes/{scopes[audience]['id']}",
            )


def cmd_apply(secrets_in: Json, recreate: bool) -> None:
    admin = ensure_master(connect(secrets_in), secrets_in)
    if recreate and admin.realm_exists(REALM):
        log(f"удаление realm {REALM} (--recreate-realm)")
        log(
            "пользователи realm будут созданы заново с новыми sub; Open WebUI находит их по "
            f"{UID_ATTRIBUTE} (Entra: oid, локальные: логин), но приложения, привязанные к sub, "
            "увидят новых пользователей"
        )
        admin.call("DELETE", f"/{REALM}")
    ensure_realm(admin, secrets_in)
    ensure_mcp_audiences(admin, cast(list[str], secrets_in.get("mcp_audiences") or []))
    ensure_idp(admin, cast(Json | None, secrets_in.get("entra")))
    log(f"готово: {BASE_URL.replace('127.0.0.1', 'localhost')}/realms/{REALM}/account")


def _browser_login(open_browser: bool) -> Json:
    """Вход через браузер (клиент antec-smoke, PKCE S256): ответ token endpoint."""
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = secrets.token_urlsafe(16)
    redirect = f"http://127.0.0.1:{SMOKE_REDIRECT_PORT}/callback"
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] == state and "code" in query:
                result["code"] = query["code"][0]
            else:
                result["error"] = query.get("error", ["нет кода"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Готово, окно можно закрыть.".encode())
            done.set()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", SMOKE_REDIRECT_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = (
        f"http://localhost:8080/realms/{REALM}/protocol/openid-connect/auth?"
        + urllib.parse.urlencode(
            {
                "client_id": "antec-smoke",
                "response_type": "code",
                "scope": "openid",
                "redirect_uri": redirect,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    print(f"Откройте для входа: {url}", file=sys.stderr, flush=True)
    if open_browser:
        webbrowser.open(url)
    if not done.wait(timeout=900):
        server.shutdown()
        raise KeycloakError("вход не завершён за 15 минут")
    server.shutdown()
    if "code" not in result:
        raise KeycloakError(f"вход не удался: {result.get('error')}")
    status, payload = token_request(
        REALM,
        {
            "grant_type": "authorization_code",
            "client_id": "antec-smoke",
            "code": result["code"],
            "redirect_uri": redirect,
            "code_verifier": verifier,
        },
    )
    if status != 200 or not isinstance(payload, dict):
        raise KeycloakError(f"обмен кода: HTTP {status}: {payload}")
    return cast(Json, payload)


def _show(title: str, claims: Json) -> None:
    keys = (
        "iss",
        "sub",
        "aud",
        "azp",
        "preferred_username",
        "idp",
        "entra_oid",
        "entra_tid",
        "scope",
        "act",
        "exp",
        "iat",
    )
    print(
        f"{title}: " + json.dumps({k: claims[k] for k in keys if k in claims}, ensure_ascii=False)
    )


def cmd_smoke(secrets_in: Json, open_browser: bool) -> None:
    """Вход пользователя и обмен его токена на токен для mcp-m365 (RFC 8693, V2)."""
    subject = cast(str, _browser_login(open_browser)["access_token"])
    sub_claims = jwt_claims(subject)
    _show("токен пользователя (antec-smoke)", sub_claims)
    status, payload = token_request(
        REALM,
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "client_id": "platform-api",
            "client_secret": _secret(secrets_in, "platform_api_client_secret"),
            "subject_token": subject,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": "mcp-m365",
            "scope": "mcp-m365",
        },
    )
    if status != 200 or not isinstance(payload, dict):
        raise KeycloakError(f"token exchange: HTTP {status}: {payload}")
    exchanged = jwt_claims(cast(str, payload["access_token"]))
    _show("токен после обмена (platform-api -> mcp-m365)", exchanged)
    aud = exchanged.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    checks = {
        "sub сохранён": exchanged.get("sub") == sub_claims.get("sub"),
        "aud = mcp-m365": auds == ["mcp-m365"],
        "azp = platform-api": exchanged.get("azp") == "platform-api",
        "срок не больше исходного": int(exchanged["exp"]) <= int(sub_claims["exp"]),
    }
    # Обмен на аудиторию, которой нет в optional scopes platform-api, должен быть отклонён:
    # Platform API не может выпустить токен пользователя для произвольного сервиса.
    status, _ = token_request(
        REALM,
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "client_id": "platform-api",
            "client_secret": _secret(secrets_in, "platform_api_client_secret"),
            "subject_token": subject,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": "open-webui",
        },
    )
    checks["обмен на чужую аудиторию отклонён"] = status in (400, 403)
    for name, ok in checks.items():
        print(f"  [{'ok' if ok else 'FAIL'}] {name}")
    if not all(checks.values()):
        raise KeycloakError("проверка обмена токенов не пройдена")


def cmd_user_token(open_browser: bool) -> None:
    """Access token пользователя (клиент antec-smoke) на stdout для scripts/platform-smoke.sh.

    Токен живёт 5 минут и годится только для Platform API на стенде (aud platform-api,
    azp antec-smoke); в терминал он не печатается, скрипт передаёт его через конвейер.
    """
    if sys.stdout.isatty():
        raise KeycloakError("user-token печатает токен только в конвейер, не в терминал")
    tokens = _browser_login(open_browser)
    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str):
        raise KeycloakError("Keycloak не выдал refresh token клиенту antec-smoke")
    # Refresh token должен жить дольше старого простоя (30 минут) и обновляться: иначе Open WebUI
    # теряет токен Keycloak раньше своей сессии (8 часов). Срок = min(простой, остаток максимума).
    status, refreshed = token_request(
        REALM,
        {
            "grant_type": "refresh_token",
            "client_id": "antec-smoke",
            "refresh_token": refresh_token,
        },
    )
    ttl = refreshed.get("refresh_expires_in") if isinstance(refreshed, dict) else None
    ok = status == 200 and isinstance(ttl, int) and ttl > 1800
    print(
        f"  [{'ok' if ok else 'FAIL'}] обновление токена, срок refresh token {ttl} с",
        file=sys.stderr,
    )
    if not ok or not isinstance(refreshed, dict):
        raise KeycloakError(
            "обновление токена не прошло или срок refresh token мал; если сессии Keycloak в"
            " браузере почти 10 часов (максимум), выйдите из Keycloak и запустите проверку снова"
        )
    sys.stdout.write(cast(str, refreshed["access_token"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    apply_p = sub.add_parser("apply", help="настроить master и realm antec")
    apply_p.add_argument("--recreate-realm", action="store_true")
    sub.add_parser("open-webui-identities", help="администраторы и antec_uid для Open WebUI")
    smoke_p = sub.add_parser("smoke", help="проверить вход и обмен токенов")
    smoke_p.add_argument(
        "--no-open", action="store_true", help="не открывать браузер, только напечатать URL"
    )
    token_p = sub.add_parser("user-token", help="вход через браузер, access token на stdout")
    token_p.add_argument("--no-open", action="store_true", help="только напечатать URL")
    args = parser.parse_args()
    try:
        secrets_in = json.load(sys.stdin)
        if not isinstance(secrets_in, dict):
            raise KeycloakError("на stdin ожидается JSON-объект с секретами")
        if args.cmd == "apply":
            cmd_apply(secrets_in, args.recreate_realm)
        elif args.cmd == "open-webui-identities":
            cmd_open_webui_identities(secrets_in)
        elif args.cmd == "user-token":
            cmd_user_token(not args.no_open)
        else:
            cmd_smoke(secrets_in, not args.no_open)
    except (KeycloakError, ValueError, OSError) as exc:
        print(f"\033[31m[antec] ошибка:\033[0m keycloak: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
