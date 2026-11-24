# Токен secrets-render для platform-worker (ADR-0012): только пароль роли platform_worker.
path "antec/data/platform-worker" {
  capabilities = ["read"]
}
