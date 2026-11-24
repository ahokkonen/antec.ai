"""Проверка цепочки аудита и контрольных точек (ADR-0012, п. 4). Только стандартная библиотека.

Не использует функции проверяемой БД: хэши считаются заново из полей событий.

    audit_chain.py verify  CHECKPOINTS          строки выгрузки (EXPORT_SQL) на stdin; итог JSON
    audit_chain.py payload CHECKPOINTS HEAD     данные новой точки (JSON одной строкой)
    audit_chain.py last    CHECKPOINTS [SHA]    sha256 последней строки; SHA = ожидаемый (OpenBao)
    audit_chain.py batch   CHECKPOINTS          тело transit/verify: подписи всех точек
    audit_chain.py append  CHECKPOINTS PAYLOAD  подпись со stdin, точка дописывается в файл

Файл точек: JSON на строку {"payload": "<JSON>", "signature": "vault:v1:..."}; payload содержит
sha256 предыдущей строки. Отсутствие файла = точек ещё нет.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

type Json = dict[str, Any]

# Порядок полей = audit.event_bytes (postgres/audit.sql).
FIELDS = (
    "seq",
    "id",
    "occurred_us",
    "actor_user_id",
    "actor_client",
    "action",
    "resource_type",
    "resource_id",
    "outcome",
    "request_id",
    "call_id",
    "details",
)
MIN_RETENTION = dt.timedelta(days=180)
# Время точки с хоста, время события из БД: запас на расхождение часов.
CLOCK_SLACK = dt.timedelta(days=1)


class ChainError(Exception):
    pass


def event_hash(prev_hex: str, row: Json) -> str:
    out = prev_hex
    for name in FIELDS:
        value = row[name]
        out += ";" + ("-" if value is None else f"{len(str(value).encode())}:{value}")
    return hashlib.sha256(out.encode()).hexdigest()


def load(path: Path) -> list[Json]:
    """Точки по порядку; цепочка точек проверяется здесь."""
    if not path.exists():
        return []
    out: list[Json] = []
    prev = ""
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            item = json.loads(line)
            payload = json.loads(item["payload"])
            if payload["prev"] != prev:
                raise ChainError(f"точка {number}: нарушена цепочка контрольных точек")
            out.append({**payload, "raw": item["payload"], "signature": item["signature"]})
        except (ValueError, KeyError, TypeError) as exc:
            raise ChainError(f"точка {number}: не разобрана ({type(exc).__name__})") from exc
        prev = hashlib.sha256(line.encode()).hexdigest()
    return out


def verify(lines: Iterable[str], points: list[Json], now: dt.datetime) -> Json:
    rows = iter(lines)
    try:
        head: Json = json.loads(next(rows))
    except StopIteration:
        raise ChainError("пустая выгрузка аудита") from None
    seq, prev = int(head["base_seq"]), str(head["base_hash"])
    hashes = {seq: prev}
    wanted = {int(p["seq"]) for p in points}
    count = 0
    for line in rows:
        if not line.strip():
            continue
        row: Json = json.loads(line)
        seq += 1
        if int(row["seq"]) != seq:
            raise ChainError(f"пропущено событие {seq}, следующее {row['seq']}")
        if row["prev_hash"] != prev or row["hash"] != event_hash(prev, row):
            raise ChainError(f"событие {seq} ({row['id']}): хэш не совпадает, строка изменена")
        prev = str(row["hash"])
        if seq in wanted:
            hashes[seq] = prev
        count += 1
    if seq != int(head["seq"]) or prev != head["hash"]:
        raise ChainError(f"конец цепочки {seq} не совпадает с chain_head {head['seq']}")
    base = int(head["base_seq"])
    recent_checked = False
    for point in points:
        if point["chain_id"] != head["chain_id"]:
            raise ChainError("контрольная точка другой цепочки: таблица аудита пересоздана")
        at = int(point["seq"])
        if at > seq:
            raise ChainError(f"точка подтверждает событие {at}, а в БД последнее {seq}: обрезано")
        if at >= base and hashes.get(at) != point["hash"]:
            raise ChainError(f"событие {at}: хэш не совпадает с контрольной точкой {point['at']}")
        # События после точки моложе 180 дней удалить по сроку не могли.
        if (
            not recent_checked
            and dt.datetime.fromisoformat(point["at"]) >= now - MIN_RETENTION + CLOCK_SLACK
        ):
            recent_checked = True
            if base > at:
                raise ChainError(f"удалены события моложе 180 дней: {at + 1}..{base}")
    return {
        "chain_id": head["chain_id"],
        "seq": seq,
        "hash": prev,
        "base_seq": base,
        "count": count,
        "checkpoints": len(points),
    }


def payload(points_path: Path, result: Json) -> str:
    lines = points_path.read_text(encoding="utf-8").splitlines() if points_path.exists() else []
    body = {
        "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "chain_id": result["chain_id"],
        "count": result["count"],
        "hash": result["hash"],
        "prev": hashlib.sha256(lines[-1].encode()).hexdigest() if lines else "",
        "seq": result["seq"],
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def last(path: Path, expected: str) -> str:
    """sha256 последней строки. Файл не короче, чем отмечено в OpenBao.

    Отметка пишется после строки, поэтому при обрыве она может отставать на одну строку.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    tail = [hashlib.sha256(line.encode()).hexdigest() for line in lines[-2:]]
    if expected and expected not in tail:
        raise ChainError("файл контрольных точек удалён, обрезан или заменён (отметка в OpenBao)")
    return tail[-1] if tail else ""


def batch(points: list[Json]) -> str:
    return json.dumps(
        {
            "batch_input": [
                {
                    "input": base64.b64encode(p["raw"].encode()).decode(),
                    "signature": p["signature"],
                }
                for p in points
            ]
        }
    )


def append(path: Path, raw: str, signature: str) -> None:
    if not signature.startswith("vault:v"):
        raise ChainError("подпись не получена")
    line = json.dumps({"payload": raw, "signature": signature}) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def main(argv: list[str]) -> int:
    try:
        command, path = argv[0], Path(argv[1])
        if command == "verify":
            result = verify(sys.stdin, load(path), dt.datetime.now(dt.UTC))
            print(json.dumps(result))
        elif command == "payload":
            print(payload(path, json.loads(argv[2])))
        elif command == "last":
            print(last(path, argv[2] if len(argv) > 2 else ""))
        elif command == "batch":
            print(batch(load(path)))
        elif command == "append":
            append(path, argv[2], sys.stdin.read().strip())
        else:
            raise IndexError
    except IndexError:
        print(__doc__, file=sys.stderr)
        return 2
    except ChainError as exc:
        print(f"аудит: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
