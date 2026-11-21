# Токен secrets-render для Keycloak: только чтение секретов Keycloak и Entra.
# Пароль роли keycloak копируется в antec/keycloak:db_password скриптом up.sh,
# чтобы этот токен не видел пароли остальных ролей PostgreSQL.
path "antec/data/keycloak" {
  capabilities = ["read"]
}

path "antec/data/entra" {
  capabilities = ["read"]
}
