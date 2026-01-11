#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import sqlalchemy as sa


DEFAULT_PREFIXES: dict[str, str] = {
    "https://api.groq.com/openai/v1": "groq",
    "https://api.cerebras.ai/v1": "cerebras",
    "https://api.fireworks.ai/inference/v1": "fireworks",
}


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
    if value is None:
        out: dict[str, Any] = {}
        parent[key] = out
        return out
    # Unexpected type (don’t clobber); replace with a dict.
    out = {}
    parent[key] = out
    return out


def _ensure_openai_prefixes(
    config_data: dict[str, Any],
    *,
    url_to_prefix: dict[str, str],
    force: bool,
) -> bool:
    changed = False

    openai = _ensure_dict(config_data, "openai")
    api_configs = _ensure_dict(openai, "api_configs")

    api_base_urls = openai.get("api_base_urls")
    if not isinstance(api_base_urls, list):
        api_base_urls = []

    for base_url, prefix in sorted(url_to_prefix.items()):
        keys: list[str] = []
        if base_url in api_base_urls:
            keys.append(str(api_base_urls.index(base_url)))
        keys.append(base_url)

        for k in keys:
            entry = api_configs.get(k)
            if not isinstance(entry, dict):
                entry = {}

            existing = entry.get("prefix_id")
            if existing == prefix:
                continue

            if existing and not force:
                continue

            entry["prefix_id"] = prefix
            api_configs[k] = entry
            changed = True

    return changed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Ensure Open WebUI OpenAI provider prefixes (openai.api_configs.*.prefix_id) "
            "exist in the DB config row, so multiple OpenAI-compatible providers don't de-duplicate."
        )
    )
    parser.add_argument(
        "--postgres-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="SQLAlchemy Postgres URL (defaults to env DATABASE_URL).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing prefix_id values (default: only set missing).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute changes without writing to the database.",
    )
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="URL=PREFIX",
        help="Add/override a provider mapping (repeatable). Example: --map https://api.x.ai/v1=grok",
    )
    args = parser.parse_args(argv)

    if not args.postgres_url:
        print("[provider-prefixes] DATABASE_URL not set; skipping.")
        return 0

    if not args.postgres_url.startswith("postgres"):
        print("[provider-prefixes] DATABASE_URL is not Postgres; skipping.")
        return 0

    url_to_prefix = dict(DEFAULT_PREFIXES)
    for raw in args.map:
        if "=" not in raw:
            raise SystemExit(f"Invalid --map value (expected URL=PREFIX): {raw!r}")
        url, prefix = raw.split("=", 1)
        url = url.strip()
        prefix = prefix.strip()
        if not url or not prefix:
            raise SystemExit(f"Invalid --map value (empty URL or PREFIX): {raw!r}")
        url_to_prefix[url] = prefix

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
            print("[provider-prefixes] No config row found; skipping.")
            return 0

        config_id = row.id
        config_data = _coerce_json_object(row.data)

        changed = _ensure_openai_prefixes(
            config_data, url_to_prefix=url_to_prefix, force=args.force
        )
        if not changed:
            print("[provider-prefixes] Already set.")
            return 0

        if args.dry_run:
            print("[provider-prefixes] Would update DB config (dry-run).")
            return 0

        conn.execute(
            config_table.update()
            .where(config_table.c.id == config_id)
            .values(data=config_data, updated_at=sa.func.now())
        )

    print("[provider-prefixes] Updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(os.sys.argv[1:]))
