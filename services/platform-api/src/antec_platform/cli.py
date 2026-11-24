"""Точка входа `antec-platform`: serve (сервис), worker (фон), migrate (БД), subject (GDPR)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid

import uvicorn

from antec_platform import logs

log = logging.getLogger("antec_platform")


async def _subject(args: argparse.Namespace) -> int:
    from antec_platform import subject
    from antec_platform.db import create_engine
    from antec_platform.settings import SubjectSettings

    settings = SubjectSettings()  # пароль из файла секрета
    engine = create_engine(settings, subject.ACTOR)
    open_webui_engine = None
    if settings.open_webui_database and not getattr(args, "skip_open_webui", False):
        open_webui_engine = create_engine(settings, subject.ACTOR, settings.open_webui_database)
    result: object
    try:
        if args.subject_command == "find":
            result = await subject.find(engine, sys.stdin.readline())
        else:
            command = subject.export if args.subject_command == "export" else subject.erase
            result = await command(
                engine,
                args.user,
                open_webui_engine=open_webui_engine,
                antec_uid=args.open_webui_uid,
            )
    except subject.SubjectError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
        if open_webui_engine is not None:
            await open_webui_engine.dispose()
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, default=str)
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="antec-platform")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="запустить Platform API")
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104 - внутри контейнера
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("migrate", help="применить миграции БД (роль владельца схемы)")
    sub.add_parser("worker", help="фоновый сервис: истечение ожиданий, потерянные вызовы")
    subject_parser = sub.add_parser("subject", help="права субъекта данных (ADR-0012, п. 7)")
    subject_sub = subject_parser.add_subparsers(dest="subject_command", required=True)
    subject_sub.add_parser("find", help="пользователь по email со stdin")
    for name, text in (("export", "выгрузка JSON в stdout"), ("erase", "удаление данных")):
        cmd = subject_sub.add_parser(name, help=text)
        cmd.add_argument("--user", type=uuid.UUID, required=True, help="id пользователя")
        group = cmd.add_mutually_exclusive_group()
        group.add_argument("--open-webui-uid", help="antec_uid локального пользователя")
        group.add_argument("--skip-open-webui", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "migrate":
        from antec_platform.migrate import upgrade
        from antec_platform.settings import DatabaseSettings

        logs.configure("INFO")
        upgrade(DatabaseSettings())  # пароль из файла секрета
        return 0

    if args.command == "subject":
        import asyncio

        logs.configure("INFO")
        return asyncio.run(_subject(args))

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
