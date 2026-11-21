# Токен secrets-render для LiteLLM: только чтение секретов LiteLLM и ключей провайдеров.
# Пароль роли litellm копируется в antec/litellm:db_password скриптом up.sh,
# чтобы этот токен не видел пароли остальных ролей PostgreSQL.
path "antec/data/litellm" {
  capabilities = ["read"]
}

path "antec/data/llm-providers" {
  capabilities = ["read"]
}
