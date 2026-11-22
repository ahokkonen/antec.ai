# Токен secrets-render для Platform API: только чтение antec/platform-api (пароль роли
# platform_app и ключ LiteLLM platform-api). Пароль роли копируется скриптом up.sh.
path "antec/data/platform-api" {
  capabilities = ["read"]
}
