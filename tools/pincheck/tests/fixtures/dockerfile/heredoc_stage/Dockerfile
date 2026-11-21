# syntax=docker/dockerfile:1
FROM quay.io/keycloak/keycloak:26.8.2@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa AS base
COPY <<EOF2 /marker
FROM scratch AS redis
EOF2
RUN <<-'SCRIPT' bash
	FROM postgres:latest
	SCRIPT
FROM redis
