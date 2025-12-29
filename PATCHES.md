# `pepperpepperpepper/open-webui` — patches & release process

This repo is a fork used for the private Open WebUI deployment at `https://chat.uh-oh.wtf`.

The goal is reproducible server deploys where “updates” are:
**merge upstream → tag in the fork → build a wheel in CI → bump the pinned wheel on the server → restart**.

## Versioning (fork tags)

We tag fork releases as:

- `v<upstream_version>-pepper.<n>`

Examples:

- `v0.6.43-pepper.1`
- `v0.6.43-pepper.2`

## What’s different from upstream

Keep this list short and concrete. Each entry should answer:
- what changed,
- why it’s needed,
- and where it lives (frontend/backend).

Current patches:
- *(none yet — this fork currently tracks upstream `v0.6.43`)*

## Deployment ownership boundary

- **Deployment/integration wiring** (s6, nginx, env, provider keys, server-specific scripts) lives outside this repo
  (on the server under `/home/pepper/apps/open-webui/deploy/`).
- **Product patches** (anything that changes Open WebUI behavior/UI itself) should live in this fork.

## Update workflow

1. Merge upstream into the fork (merge or rebase, your preference).
2. Apply/adjust patches and update this file.
3. Create a new tag (example: `v0.6.43-pepper.1`).
4. CI builds a wheel from that tag and attaches it to the GitHub Release.
5. Server deploy: bump the pinned wheel URL in `/home/pepper/apps/open-webui/pyproject.toml` and restart Open WebUI.

## Rollback

Re-pin the server to the previous wheel URL and restart.

