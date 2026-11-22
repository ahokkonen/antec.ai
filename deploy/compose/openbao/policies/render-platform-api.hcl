# Токен secrets-render для Platform API: только чтение antec/platform-api (пароль роли
# platform_app, ключ LiteLLM platform-api, секрет клиента platform-api в Keycloak для обмена
# токена). Пароль роли и секрет клиента копируются скриптом up.sh.
path "antec/data/platform-api" {
  capabilities = ["read"]
}
