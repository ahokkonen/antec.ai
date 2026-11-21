# Токен secrets-render: только чтение паролей postgres.
path "antec/data/postgres" {
  capabilities = ["read"]
}
