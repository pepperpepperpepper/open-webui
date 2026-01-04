#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
from typing import Any

import sqlalchemy as sa


def _coerce_json_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        loaded = json.loads(value)
        if isinstance(loaded, dict):
            return loaded
    raise TypeError(f"Expected JSON object, got {type(value).__name__}")


def _normalize_base_url(url: str) -> str:
    return url.strip().rstrip("/")


def _parse_semicolon_list(raw: str) -> list[str]:
    raw = raw or ""
    parts = [p.strip() for p in raw.split(";")]
    return [p for p in parts if p]


def _sha8(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _ensure_dict(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if isinstance(value, dict):
        return value
    if value is None:
        out: dict[str, Any] = {}
        parent[key] = out
        return out
    out = {}
    parent[key] = out
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Sync Open WebUI OpenAI-compatible provider keys in the DB config (openai.api_keys) "
            "from environment variables OPENAI_API_BASE_URLS/OPENAI_API_KEYS."
        )
    )
    parser.add_argument(
        "--postgres-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="SQLAlchemy Postgres URL (defaults to env DATABASE_URL).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute changes without writing to the database.",
    )
    parser.add_argument(
        "--append-missing-urls",
        action="store_true",
        help="Append env URLs not present in DB (default: only update existing URLs).",
    )
    args = parser.parse_args(argv)

    if not args.postgres_url:
        print("[provider-keys] DATABASE_URL not set; skipping.")
        return 0

    if not args.postgres_url.startswith("postgres"):
        print("[provider-keys] DATABASE_URL is not Postgres; skipping.")
        return 0

    env_urls = [_normalize_base_url(u) for u in _parse_semicolon_list(os.environ.get("OPENAI_API_BASE_URLS", ""))]
    env_keys = _parse_semicolon_list(os.environ.get("OPENAI_API_KEYS", ""))
    if len(env_keys) < len(env_urls):
        env_keys += [""] * (len(env_urls) - len(env_keys))

    env_url_to_key: dict[str, str] = {}
    for url, key in zip(env_urls, env_keys):
        if url and key:
            env_url_to_key[url] = key

    if not env_url_to_key:
        print("[provider-keys] No OPENAI_API_KEYS present in env; skipping.")
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
            print("[provider-keys] No config row found; skipping.")
            return 0

        config_id = row.id
        config_data = _coerce_json_object(row.data)

        openai = _ensure_dict(config_data, "openai")
        db_urls_raw = openai.get("api_base_urls") or []
        db_keys_raw = openai.get("api_keys") or []

        if not isinstance(db_urls_raw, list) or not all(isinstance(u, str) for u in db_urls_raw):
            print("[provider-keys] DB openai.api_base_urls is not a string list; skipping.")
            return 0

        db_urls = [_normalize_base_url(u) for u in db_urls_raw]
        db_keys = [str(k) for k in db_keys_raw] if isinstance(db_keys_raw, list) else []

        if len(db_keys) < len(db_urls):
            db_keys += [""] * (len(db_urls) - len(db_keys))
        elif len(db_keys) > len(db_urls):
            db_keys = db_keys[: len(db_urls)]

        changed = False
        updated: list[tuple[str, str, str]] = []  # (url, old_sha8, new_sha8)

        db_url_set = set(db_urls)
        for i, url in enumerate(db_urls):
            env_key = env_url_to_key.get(url)
            if not env_key:
                continue
            if db_keys[i] == env_key:
                continue
            old_sha = _sha8(db_keys[i]) if db_keys[i] else ""
            new_sha = _sha8(env_key)
            db_keys[i] = env_key
            changed = True
            updated.append((url, old_sha, new_sha))

        if args.append_missing_urls:
            for url, key in sorted(env_url_to_key.items()):
                if url in db_url_set:
                    continue
                db_urls.append(url)
                db_keys.append(key)
                changed = True
                updated.append((url, "", _sha8(key)))

        if not changed:
            print("[provider-keys] Already synced.")
            return 0

        openai["api_base_urls"] = db_urls
        openai["api_keys"] = db_keys

        if args.dry_run:
            print("[provider-keys] Would update DB config (dry-run).")
            for url, old_sha, new_sha in updated:
                print(f"[provider-keys] {url}: {old_sha or '(none)'} -> {new_sha}")
            return 0

        conn.execute(
            config_table.update()
            .where(config_table.c.id == config_id)
            .values(data=config_data, updated_at=sa.func.now())
        )

    print("[provider-keys] Updated DB config keys.")
    for url, old_sha, new_sha in updated:
        print(f"[provider-keys] {url}: {old_sha or '(none)'} -> {new_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(os.sys.argv[1:]))

