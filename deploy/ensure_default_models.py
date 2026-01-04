#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Optional

import sqlalchemy as sa


def _coerce_json_object(value: object) -> dict[str, Any]:
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


def _ensure_defaults(
    config_data: dict[str, Any],
    *,
    default_model: Optional[str],
    pinned_models: Optional[str],
    task_model_external: Optional[str],
    force: bool,
) -> bool:
    changed = False

    if default_model is not None:
        ui = _ensure_dict(config_data, "ui")
        current = ui.get("default_models")
        if force or not current:
            if current != default_model:
                ui["default_models"] = default_model
                changed = True

    if pinned_models is not None:
        ui = _ensure_dict(config_data, "ui")
        current = ui.get("default_pinned_models")
        if force or not current:
            if current != pinned_models:
                ui["default_pinned_models"] = pinned_models
                changed = True

    if task_model_external is not None:
        task = _ensure_dict(config_data, "task")
        model_cfg = _ensure_dict(task, "model")
        current = model_cfg.get("external")
        if force or not current:
            if current != task_model_external:
                model_cfg["external"] = task_model_external
                changed = True

    return changed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Ensure Open WebUI default models in the DB config row."
    )
    parser.add_argument(
        "--postgres-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="SQLAlchemy Postgres URL (defaults to env DATABASE_URL).",
    )
    parser.add_argument(
        "--default-model",
        default=None,
        help='Value for ui.default_models (e.g. "cerebras.zai-glm-4.6").',
    )
    parser.add_argument(
        "--pinned-models",
        default=None,
        help='Value for ui.default_pinned_models (comma-separated).',
    )
    parser.add_argument(
        "--task-model-external",
        default=None,
        help='Value for task.model.external (e.g. "cerebras.zai-glm-4.6").',
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
        print("[default-models] DATABASE_URL not set; skipping.")
        return 0

    if not args.postgres_url.startswith("postgres"):
        print("[default-models] DATABASE_URL is not Postgres; skipping.")
        return 0

    default_model = args.default_model
    pinned_models = args.pinned_models
    task_model_external = args.task_model_external

    if default_model is None and pinned_models is None and task_model_external is None:
        print("[default-models] No changes requested; skipping.")
        return 0

    engine = sa.create_engine(args.postgres_url)
    md = sa.MetaData()
    config_table = sa.Table("config", md, autoload_with=engine)

    with engine.begin() as conn:
        row = conn.execute(
            sa.select(config_table.c.id, config_table.c.data)
            .order_by(
                sa.desc(config_table.c.updated_at).nullslast(),
                sa.desc(config_table.c.id),
            )
            .limit(1)
        ).first()

        if row is None:
            print("[default-models] No config row found; skipping.")
            return 0

        config_id = row.id
        config_data = _coerce_json_object(row.data)

        changed = _ensure_defaults(
            config_data,
            default_model=default_model,
            pinned_models=pinned_models,
            task_model_external=task_model_external,
            force=args.force,
        )
        if not changed:
            print("[default-models] Already set.")
            return 0

        if args.dry_run:
            print("[default-models] Would update DB config (dry-run).")
            return 0

        conn.execute(
            config_table.update()
            .where(config_table.c.id == config_id)
            .values(data=config_data, updated_at=sa.func.now())
        )

    print("[default-models] Updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(os.sys.argv[1:]))

