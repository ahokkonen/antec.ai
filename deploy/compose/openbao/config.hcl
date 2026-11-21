# OpenBao 2.7.2, локальный стенд (ADR-0017).
# Хранилище: встроенный Raft (не PostgreSQL, чтобы не было циклической зависимости).
ui            = false
disable_mlock = true   # рекомендация для Raft; swap в контейнере не используется
log_level     = "info"

storage "raft" {
  path    = "/openbao/file"
  node_id = "openbao-1"
}

# Порт не публикуется на хост: доступ только из внутренних сетей compose и через
# `docker compose exec`. TLS на локальном стенде выключен (ADR-0017); на сервере обязателен.
listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}

api_addr     = "http://openbao:8200"
cluster_addr = "http://openbao:8201"
