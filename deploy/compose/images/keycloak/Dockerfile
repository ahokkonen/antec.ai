# Keycloak 26.8.2, оптимизированная сборка для antec.ai (ADR-0007).
# База закреплена по digest из policy/allowed-versions.yaml (components.keycloak).
# Сборочные опции фиксируются здесь, чтобы контейнер запускался через `start --optimized`:
# без пересборки при старте и с корневой ФС только для чтения.
FROM quay.io/keycloak/keycloak:26.8.2@sha256:7e66a96e4739239070aeaf72e4c5decf0f7f682cfc725b9f7ea2b336eaba38b3

# - db=postgres: отдельная БД keycloak на базовом слое (ADR-0017);
# - vault=file: секреты realm (клиентский секрет Entra) читаются из файлов, а не из БД;
# - health-enabled: /health/ready на порту управления 9000 для healthcheck;
# - features-disabled: выключены функции, которые не нужны и расширяют поверхность атаки:
#   impersonation (вход администратора под пользователем), identity-brokering-api (V1
#   устарел; V2 включается отдельным решением на шаге MCP M365), twitter-broker (устарел).
# Preview-функции не включаются (ADR-0007): только поддерживаемые по умолчанию.
RUN /opt/keycloak/bin/kc.sh build \
      --db=postgres \
      --vault=file \
      --health-enabled=true \
      --metrics-enabled=false \
      --features-disabled=impersonation,identity-brokering-api,twitter-broker

LABEL org.opencontainers.image.title="antec-keycloak" \
      org.opencontainers.image.description="Keycloak 26.8.2, оптимизированная сборка для antec.ai" \
      org.opencontainers.image.version="26.8.2"
