# Токен secrets-render для Open WebUI: только чтение antec/open-webui.
# Пароль роли openwebui, секрет клиента open-webui в Keycloak и виртуальный ключ LiteLLM
# копируются или генерируются в antec/open-webui скриптом up.sh.
path "antec/data/open-webui" {
  capabilities = ["read"]
}
