#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime

import sqlalchemy as sa


def _parse_dt(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


def _coerce_value(value: object, dst_column: sa.Column) -> object:
    if value is None:
        return None

    if isinstance(dst_column.type, sa.Boolean):
        if value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("0", "false", "f", "no", "n"):
                return False
            if v in ("1", "true", "t", "yes", "y"):
                return True

    if isinstance(dst_column.type, sa.DateTime):
        return _parse_dt(value)

    # JSON / JSONB (keep generic; dialect types vary)
    if isinstance(dst_column.type, sa.types.JSON) or dst_column.type.__class__.__name__ in (
        "JSON",
        "JSONB",
    ):
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value

    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Copy Open WebUI data from SQLite to PostgreSQL (same schema version)."
    )
    parser.add_argument(
        "--sqlite-path",
        default="/home/pepper/data/open-webui/webui.db",
        help="Path to the existing SQLite DB file.",
    )
    parser.add_argument(
        "--postgres-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="SQLAlchemy URL for Postgres (defaults to env DATABASE_URL).",
    )
    parser.add_argument(
        "--tables",
        default="user,auth,chat,config,tag",
        help="Comma-separated list of tables to copy.",
    )
    parser.add_argument(
        "--wipe-destination",
        action="store_true",
        help="Delete all rows in destination tables before copy.",
    )
    args = parser.parse_args()

    if not args.postgres_url:
        raise SystemExit("--postgres-url is required (or set DATABASE_URL).")

    dst_engine = sa.create_engine(args.postgres_url)

    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    if not tables:
        raise SystemExit("No tables specified.")

    dst_md = sa.MetaData()

    with sqlite3.connect(args.sqlite_path) as src_conn, dst_engine.begin() as dst_conn:
        src_conn.row_factory = sqlite3.Row

        if args.wipe_destination:
            for name in tables:
                dst_table = sa.Table(name, dst_md, autoload_with=dst_engine)
                dst_conn.execute(dst_table.delete())

        for name in tables:
            if '\"' in name:
                raise SystemExit(f'Invalid table name: {name!r}')

            dst_table = sa.Table(name, dst_md, autoload_with=dst_engine)
            src_rows = src_conn.execute(f'SELECT * FROM \"{name}\"').fetchall()

            if not src_rows:
                print(f"{name}: 0 rows (skip)")
                continue

            dst_rows: list[dict[str, object]] = []
            for row in src_rows:
                src_row = dict(row)
                out: dict[str, object] = {}
                for col in dst_table.columns:
                    if col.name in src_row:
                        out[col.name] = _coerce_value(src_row[col.name], col)
                dst_rows.append(out)

            dst_conn.execute(dst_table.insert(), dst_rows)
            print(f"{name}: copied {len(dst_rows)} rows")

    with dst_engine.begin() as dst_conn:
        dst_conn.execute(
            sa.text(
                "select setval('config_id_seq', coalesce((select max(id) from config), 0))"
            )
        )
        dst_conn.execute(
            sa.text(
                "select setval('migratehistory_id_seq', coalesce((select max(id) from migratehistory), 0))"
            )
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
