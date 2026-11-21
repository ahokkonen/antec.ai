#!/usr/bin/env python3
"""Настройка и проверка Keycloak локального стенда через Admin API (ADR-0007).

Только стандартная библиотека: скрипт запускается на хосте из scripts/up.sh.
Секреты приходят JSON-объектом через stdin, не через argv и не через окружение.

    keycloak.py apply [--recreate-realm]   идемпотентная настройка master и realm antec
    keycloak.py smoke                      вход через браузер и обмен токенов (RFC 8693)

Realm как код: deploy/compose/keycloak/realm-antec.json создаётся, если realm нет.
Изменения в существующем realm файл не применяет: локально realm пересоздаётся
(`up.sh --recreate-realm`), сверка с дрейфом на сервере решается отдельно (ADR-0007).
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import http.server
import json
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
DEFAULT_SA_SCOPES = ("service_account", "web-origins", "acr", "profile", "roles", "basic", "email")
# Атрибуты, которые брокер Entra пишет в профиль пользователя (oid, tid).
PROFILE_ATTRIBUTES = ("entra_oid", "entra_tid")
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
        sa = admin.call("GET", f"/master/clients/{client['id']}/service-account-user")
        role = admin.call("GET", "/master/roles/admin")
        admin.call("POST", f"/master/users/{sa['id']}/role-mappings/realm", [role])
    elif client.get("secret") != automation_secret or client.get("fullScopeAllowed"):
        client["secret"] = automation_secret
        client["fullScopeAllowed"] = False
        admin.call("PUT", f"/master/clients/{client['id']}", client)
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

    if admin.find_user("master", ADMIN_USER) is None:
        log(f"создание администратора {ADMIN_USER} (realm master, TOTP при первом входе)")
        user_id = admin.call(
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
        role = admin.call("GET", "/master/roles/admin")
        admin.call("POST", f"/master/users/{user_id}/role-mappings/realm", [role])

    bootstrap = admin.find_client("master", BOOTSTRAP_CLIENT)
    if bootstrap is not None:
        log(f"удаление временного bootstrap-клиента {BOOTSTRAP_CLIENT}")
        admin.call("DELETE", f"/master/clients/{bootstrap['id']}")
    return admin


def create_realm(admin: Admin, secrets_in: Json) -> None:
    spec = cast(Json, json.loads((KEYCLOAK_DIR / "realm-antec.json").read_text()))
    scopes = cast(list[Json], spec.pop("clientScopes", []))
    clients = cast(list[Json], spec.pop("clients", []))
    log(f"создание realm {REALM} из keycloak/realm-antec.json")
    admin.call("POST", "", spec)
    for scope in scopes:
        admin.call("POST", f"/{REALM}/client-scopes", scope)
    client_secrets = client_secret_map(secrets_in)
    for client in clients:
        client = copy.deepcopy(client)
        if client["clientId"] in client_secrets:
            client["secret"] = client_secrets[client["clientId"]]
        admin.call("POST", f"/{REALM}/clients", client)
    ensure_user_profile(admin)
    log(f"создание аварийного пользователя {BREAKGLASS_USER} (TOTP при первом входе)")
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


def client_secret_map(secrets_in: Json) -> dict[str, str]:
    return {
        "platform-api": _secret(secrets_in, "platform_api_client_secret"),
        "open-webui": _secret(secrets_in, "open_webui_client_secret"),
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
    """Атрибуты Entra видны и меняются только администратором и брокером."""
    profile = cast(Json, admin.call("GET", f"/{REALM}/users/profile"))
    names = {a["name"] for a in profile.get("attributes", [])}
    added = False
    for name in PROFILE_ATTRIBUTES:
        if name not in names:
            profile.setdefault("attributes", []).append(
                {
                    "name": name,
                    "displayName": name,
                    "permissions": {"view": ["admin"], "edit": ["admin"]},
                    "multivalued": False,
                }
            )
            added = True
    if added:
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


def cmd_apply(secrets_in: Json, recreate: bool) -> None:
    admin = ensure_master(connect(secrets_in), secrets_in)
    if recreate and admin.realm_exists(REALM):
        log(f"удаление realm {REALM} (--recreate-realm)")
        admin.call("DELETE", f"/{REALM}")
    if not admin.realm_exists(REALM):
        create_realm(admin, secrets_in)
    else:
        ensure_user_profile(admin)
        ensure_client_secrets(admin, secrets_in)
    ensure_idp(admin, cast(Json | None, secrets_in.get("entra")))
    log(f"готово: {BASE_URL.replace('127.0.0.1', 'localhost')}/realms/{REALM}/account")


def _browser_login(open_browser: bool) -> str:
    """Код авторизации через браузер (клиент antec-smoke, PKCE S256)."""
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
    return cast(str, payload["access_token"])


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
    subject = _browser_login(open_browser)
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    apply_p = sub.add_parser("apply", help="настроить master и realm antec")
    apply_p.add_argument("--recreate-realm", action="store_true")
    smoke_p = sub.add_parser("smoke", help="проверить вход и обмен токенов")
    smoke_p.add_argument(
        "--no-open", action="store_true", help="не открывать браузер, только напечатать URL"
    )
    args = parser.parse_args()
    try:
        secrets_in = json.load(sys.stdin)
        if not isinstance(secrets_in, dict):
            raise KeycloakError("на stdin ожидается JSON-объект с секретами")
        if args.cmd == "apply":
            cmd_apply(secrets_in, args.recreate_realm)
        else:
            cmd_smoke(secrets_in, not args.no_open)
    except (KeycloakError, ValueError, OSError) as exc:
        print(f"\033[31m[antec] ошибка:\033[0m keycloak: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
