#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Optional

import sqlalchemy as sa


def _coerce_json_object(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        loaded = json.loads(value)
        if isinstance(loaded, dict):
            return loaded
    raise TypeError(f"Expected JSON object, got {type(value).__name__}")


def _ensure_dict(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    parent[key] = out
    return out


def _parse_csv(value: Optional[str]) -> Optional[list[str]]:
    if value is None:
        return None
    items = [item.strip() for item in value.split(",")]
    return [item for item in items if item]


def _ensure_user_defaults(
    user_settings: dict[str, Any],
    *,
    models: Optional[list[str]],
    pinned_models: Optional[list[str]],
    force: bool,
) -> bool:
    changed = False
    ui = _ensure_dict(user_settings, "ui")

    if models is not None:
        current = ui.get("models")
        if force or not current:
            if current != models:
                ui["models"] = models
                changed = True

    if pinned_models is not None:
        current = ui.get("pinnedModels")
        if force or not current:
            if current != pinned_models:
                ui["pinnedModels"] = pinned_models
                changed = True

    return changed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Ensure Open WebUI per-user default model settings in the DB user row."
    )
    parser.add_argument(
        "--postgres-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="SQLAlchemy Postgres URL (defaults to env DATABASE_URL).",
    )
    parser.add_argument(
        "--email",
        required=True,
        help="User email to update (e.g. peppersclothescult@gmail.com).",
    )
    parser.add_argument(
        "--models",
        default=None,
        help='Comma-separated model IDs for user.settings.ui.models (e.g. "cerebras.zai-glm-4.6").',
    )
    parser.add_argument(
        "--pinned-models",
        default=None,
        help='Comma-separated model IDs for user.settings.ui.pinnedModels (e.g. "cerebras.zai-glm-4.6").',
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing values (default: only set if missing/empty).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute changes without writing to the database.",
    )
    args = parser.parse_args(argv)

    if not args.postgres_url:
        print("[user-default-models] DATABASE_URL not set; skipping.")
        return 0

    if not args.postgres_url.startswith("postgres"):
        print("[user-default-models] DATABASE_URL is not Postgres; skipping.")
        return 0

    models = _parse_csv(args.models)
    pinned_models = _parse_csv(args.pinned_models)

    if models is None and pinned_models is None:
        print("[user-default-models] No changes requested; skipping.")
        return 0

    engine = sa.create_engine(args.postgres_url)
    md = sa.MetaData()
    user_table = sa.Table("user", md, autoload_with=engine)

    with engine.begin() as conn:
        row = conn.execute(
            sa.select(user_table.c.id, user_table.c.email, user_table.c.settings).where(
                user_table.c.email == args.email
            )
        ).first()

        if row is None:
            print(f"[user-default-models] No user found for {args.email}; skipping.")
            return 0

        user_id = row.id
        settings = _coerce_json_object(row.settings)

        changed = _ensure_user_defaults(
            settings, models=models, pinned_models=pinned_models, force=args.force
        )
        if not changed:
            print("[user-default-models] Already set.")
            return 0

        if args.dry_run:
            print("[user-default-models] Would update user settings (dry-run).")
            return 0

        now_ms = int(time.time() * 1000)
        conn.execute(
            user_table.update()
            .where(user_table.c.id == user_id)
            .values(settings=settings, updated_at=now_ms)
        )

    print("[user-default-models] Updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(os.sys.argv[1:]))

