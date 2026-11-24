"""Точка входа `antec-platform`: serve (сервис), worker (фоновые задачи), migrate (миграции БД)."""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from antec_platform import logs

log = logging.getLogger("antec_platform")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="antec-platform")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="запустить Platform API")
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104 - внутри контейнера
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("migrate", help="применить миграции БД (роль владельца схемы)")
    sub.add_parser("worker", help="фоновый сервис: истечение ожиданий, потерянные вызовы")
    args = parser.parse_args(argv)

    if args.command == "migrate":
        from antec_platform.migrate import upgrade
        from antec_platform.settings import DatabaseSettings

        logs.configure("INFO")
        upgrade(DatabaseSettings())  # пароль из файла секрета
        return 0

    if args.command == "worker":
        import asyncio

        from antec_platform.settings import WorkerSettings
        from antec_platform.worker import serve as serve_worker

        worker_settings = WorkerSettings()  # пароль из файла секрета
        logs.configure(worker_settings.log_level)
        asyncio.run(serve_worker(worker_settings))
        return 0

    from antec_platform.main import build_app
    from antec_platform.settings import Settings

    settings = Settings()  # обязательные поля из окружения и файлов
    logs.configure(settings.log_level)
    log.info("Platform API: модели %s, клиенты %s", settings.models, settings.allowed_clients)
    uvicorn.run(
        build_app(settings),
        host=args.host,
        port=args.port,
        log_config=None,
        access_log=False,
        server_header=False,
        proxy_headers=False,
        timeout_graceful_shutdown=30,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
