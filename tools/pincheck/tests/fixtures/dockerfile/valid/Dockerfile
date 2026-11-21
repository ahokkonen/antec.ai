# syntax=docker/dockerfile:1
# Многостадийная сборка: ссылки на стадии и scratch не являются внешними образами.
FROM --platform=$BUILDPLATFORM quay.io/keycloak/keycloak:26.8.2@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa AS builder
RUN echo build

from builder as second
FROM \
    quay.io/keycloak/keycloak:26.8.2@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
COPY --from=builder /x /x
FROM scratch
COPY --from=second /x /x
