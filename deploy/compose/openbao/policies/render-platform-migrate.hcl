# Токен secrets-render для миграций Platform API: только чтение antec/platform-migrate
# (пароль владельца схемы platform). Сервис platform-api этот пароль не получает.
path "antec/data/platform-migrate" {
  capabilities = ["read"]
}
