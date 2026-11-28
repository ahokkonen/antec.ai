# Токен secrets-render для Grafana (ADR-0013): чтение antec/grafana (секрет клиента grafana,
# пароль администратора, пароль роли platform_metrics) и antec/smtp (необязательно, set-smtp.sh).
path "antec/data/grafana" {
  capabilities = ["read"]
}
path "antec/data/smtp" {
  capabilities = ["read"]
}
