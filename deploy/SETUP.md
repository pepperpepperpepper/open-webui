# Open WebUI on chat.uh-oh.wtf (server notes)

## What’s already prepared

- App + Poetry venv: `/var/www/open-webui`
- Env file (includes `WEBUI_SECRET_KEY`): `/var/www/open-webui/.env`
- Data dir (uploads, cache, etc): `/home/arch/data/open-webui`
- Static assets dir (favicons, swagger-ui, fonts): `/home/arch/data/open-webui/static`
- Persistence DB (Postgres): `open_webui` on `127.0.0.1:5432` (role `open_webui`)
- s6 service template: `/var/www/open-webui/deploy/s6/open-webui`
- nginx vhost template: `/var/www/open-webui/deploy/nginx.chat.uh-oh.wtf.conf`

## Start Open WebUI manually (no root)

```sh
cd /var/www/open-webui
set -a; . ./.env; set +a
./.venv/bin/open-webui serve --host 127.0.0.1 --port 8080
```

## Postgres persistence

Open WebUI uses `DATABASE_URL` from `/var/www/open-webui/.env`.

- Current value: `postgresql://open_webui@127.0.0.1:5432/open_webui`
- Old SQLite DB (backup): `/home/arch/data/open-webui/webui.db.bak-*`

To (re)run the SQLite → Postgres copy:

```sh
cd /var/www/open-webui
sudo s6-svc -d /service/open-webui
set -a; . ./.env; set +a
./.venv/bin/python deploy/migrate_sqlite_to_postgres.py --wipe-destination
sudo s6-svc -u /service/open-webui
```

## Enable via s6 (root)

```sh
cp -a /var/www/open-webui/deploy/s6/open-webui /service/open-webui
```

To control it (root):

```sh
s6-svc -u /service/open-webui   # up
s6-svc -d /service/open-webui   # down
s6-svc -r /service/open-webui   # restart
```

Logs:

- `/home/arch/logs/open-webui/current` (and rotated files next to it)

## Memory pressure runbook

This host has had recurring slowdowns where memory pressure climbs into swap before one obvious app process stands out. Use the dedicated runbook in `deploy/MEMORY_PRESSURE.md`.

Quick commands:

```sh
cd /var/www/open-webui
./deploy/capture_memory_pressure.sh --reason manual
./deploy/watch_memory_pressure.sh
```

The watcher can also be enabled under s6 from `deploy/s6/open-webui-pressure-watch`:

```sh
sudo cp -a /var/www/open-webui/deploy/s6/open-webui-pressure-watch /service/open-webui-pressure-watch
sudo s6-svscanctl -a /service
```

## Enable nginx vhost (root)

```sh
cp /var/www/open-webui/deploy/nginx.chat.uh-oh.wtf.conf /etc/nginx/sites-available/chat.uh-oh.wtf
ln -sf /etc/nginx/sites-available/chat.uh-oh.wtf /etc/nginx/sites-enabled/chat.uh-oh.wtf
nginx -t
systemctl reload nginx
```

## First login

- Visit `https://chat.uh-oh.wtf`
- The **first** user to sign up becomes **admin**
- Immediately after that, ensure signups are disabled (Admin Settings → Authentication → “Enable New Sign Ups”).

## Google login (OAuth)

Open WebUI has a built-in Google OIDC provider. Once configured, the login page will show a “Google” button.

1. Create a Google OAuth Client (Web application) and set the **Authorized redirect URI** to:
   - `https://chat.uh-oh.wtf/oauth/google/login/callback`
2. Add these to `/var/www/open-webui/.env` (quote values), then restart:

```sh
GOOGLE_CLIENT_ID='...'
GOOGLE_CLIENT_SECRET='...'
GOOGLE_REDIRECT_URI='https://chat.uh-oh.wtf/oauth/google/login/callback'

# Private mode suggestions (pick one approach):
# - Existing account → allow Google login to attach by email:
# OAUTH_MERGE_ACCOUNTS_BY_EMAIL='True'
# ENABLE_OAUTH_SIGNUP='False'
#
# - Allow OAuth signups but restrict to a Workspace domain:
# ENABLE_OAUTH_SIGNUP='True'
# OAUTH_ALLOWED_DOMAINS='uh-oh.wtf'
#
# - Allow OAuth signups for anyone (open):
# ENABLE_OAUTH_SIGNUP='True'
# DEFAULT_USER_ROLE='user'
```

Restart:

```sh
sudo s6-svc -r /service/open-webui
```

### Bootstrap safety (recommended)

Before exposing `https://chat.uh-oh.wtf` to the public internet, consider creating the first admin user over an SSH tunnel:

1. Enable/start the s6 service, but **don’t** enable the nginx vhost yet.
2. On your laptop: `ssh -L 8080:127.0.0.1:8080 arch@uh-oh.wtf`
3. Visit `http://localhost:8080` and create the first user (becomes admin).
4. Confirm signups are disabled, then enable the nginx vhost.

### Remote providers

Add your provider keys to `/var/www/open-webui/.env` (or via the WebUI), then restart the service:

- `sudo s6-svc -r /service/open-webui`

### Provider prefixes (Groq/Cerebras visibility)

Open WebUI de-duplicates OpenAI-compatible models by `id` across all `OPENAI_API_BASE_URLS`. If you use OpenRouter *and* direct providers, overlapping model IDs can make a provider’s models appear “missing”.

This server sets OpenAI `api_configs` prefixes so Groq/Cerebras models stay visible and searchable:

- Groq: `groq.<model_id>`
- Cerebras: `cerebras.<model_id>`

This config lives in Postgres (`config.data.openai.api_configs`) and should survive upgrades, but you can re-apply it at any time:

```sh
cd /var/www/open-webui
set -a; . ./.env; set +a
./.venv/bin/python deploy/ensure_openai_provider_prefixes.py
sudo s6-svc -r /service/open-webui
```

### Default model

This server pins the default chat model to Cerebras GLM-4.6:

```sh
cd /var/www/open-webui
set -a; . ./.env; set +a
./.venv/bin/python deploy/ensure_default_models.py --force \
  --default-model "cerebras.zai-glm-4.6" \
  --pinned-models "cerebras.zai-glm-4.6" \
  --task-model-external "cerebras.zai-glm-4.6"
sudo s6-svc -r /service/open-webui
```

### Local patches (re-apply after Open WebUI upgrades)

Some fixes/customizations are applied directly to the installed Open WebUI files inside the Poetry venv (they will be overwritten when `open-webui` is upgraded).

After any upgrade (e.g. `poetry update open-webui`), re-apply local patches:

```sh
cd /var/www/open-webui
./deploy/reapply_after_open_webui_upgrade.sh
```

Notes:

- Frontend assets may be cached by your browser; if you don’t see the change, do a hard refresh (`Ctrl+Shift+R`).
- If you are running a `+pepper.*` fork wheel, `reapply_after_open_webui_upgrade.sh` will **skip** local patching (patches are baked into the wheel).

### Fork wheel upgrades (recommended; no frontend build on this server)

This server is too small to reliably build Open WebUI’s frontend (Node/Vite) during installs, so upgrades should be done by installing a **pre-built wheel** from our fork.

1) Build the wheel in CI (tag in the fork), then either:
   - download it directly from the GitHub Release (recommended), or
   - copy the wheel to the server (e.g. `scp`).

2) Upgrade by tag (downloads the wheel + pins + restarts):

```sh
cd /var/www/open-webui
./deploy/upgrade_open_webui_from_tag.sh v0.6.43-pepper.1
```

Private fork: set a token first (or pass `--token`):

```sh
export GITHUB_TOKEN='...'
cd /var/www/open-webui
./deploy/upgrade_open_webui_from_tag.sh v0.6.43-pepper.1
```

3) Or, upgrade from a wheel you already have locally:

```sh
cd /var/www/open-webui
./deploy/upgrade_open_webui.sh /path/to/open_webui-*.whl --tag v0.6.43-pepper.1
```

Notes:

- `upgrade_open_webui.sh` will stop any installed s6 services (`open-webui` + LiveKit services) before reinstalling, then start them again afterward.

Rollback: re-run `pin_open_webui_wheel.sh` with the previous wheel, then restart.

### Groq PlayAI TTS

Open WebUI can use Groq’s OpenAI-compatible TTS endpoint:

- Base URL: `https://api.groq.com/openai/v1`
- Models: `playai-tts`, `playai-tts-arabic`
- Voices: `*-PlayAI` (see Groq’s “Text to Speech” docs page for the full list)

Important: the first time you use `playai-tts`, Groq may require your **org admin** to accept PlayAI terms in the Groq Console.

### Cartesia TTS

This deployment is configured to use Cartesia for TTS (voices are fetched from the Cartesia API and shown in the Admin Audio settings).

- Base URL: `https://api.cartesia.ai`
- Models: `sonic`, `sonic-2`, `sonic-turbo`
- Requires `CARTESIA_API_KEY` in `/home/arch/.api-keys` and a `Cartesia-Version` (set via `AUDIO_TTS_OPENAI_PARAMS` in `/var/www/open-webui/.env`).
- Note: Cartesia’s TTS endpoint is `POST /tts/bytes` (Open WebUI’s OpenAI TTS expects `POST /audio/speech`), so the backend must translate requests:
  - On PyPI installs, this is done via `deploy/apply_local_patches.py`.
  - On `+pepper.*` fork wheels, it is built-in.

### Cartesia STT (Ink Whisper)

This deployment can use Cartesia for STT via the OpenAI-compatible transcriptions API (Admin → Audio → STT).

- Engine: `openai`
- Base URL: `https://api.cartesia.ai`
- Model: `ink/ink-whisper` (normalized to Cartesia’s `ink-whisper`)
- Language: locales like `en-US` are normalized to `en` for Cartesia.

### Task “analysis” model (titles/tags/query generation)

Open WebUI uses a separate model for background tasks (title generation, tags, follow-up, query generation, etc.). For remote providers, set `TASK_MODEL_EXTERNAL` (Admin → Tasks).

## LiveKit Voice → moved to its own project (`livekit-voice`)

As of **2026-06-27** the LiveKit voice backend (portal, agent, web demo, server config, TTS
fillers) is **no longer in this repo** — it was extracted into a standalone project:

- **Repo:** `git@git.uh-oh.wtf:livekit-voice.git` — browse <https://git.uh-oh.wtf/livekit-voice.git>
- **Deploy (this host):** `/var/www/livekit-voice` — own venv + env files; s6 services
  **`livekit-voice-{server,portal,agent}`** on **:8191** (portal) / **:7890** (LiveKit WS) /
  **:7891** (RTC TCP) / UDP **50200–50300**.
- **nginx** (this repo's vhost) still routes `https://chat.uh-oh.wtf/livekit/` → portal `:8191`
  and `/lk/` → LiveKit server `:7890`.
- **Open WebUI stays the identity provider:** the portal validates OWUI JWTs with the shared
  **`WEBUI_SECRET_KEY`** (must match this repo's `.env`). The one owui-voice-specific patch still
  in THIS repo is the OAuth app-redirect (`deploy/apply_local_patches.py::_patch_oauth_app_redirect`,
  `?app=1` → `oopswtfvoice://`).

Setup / run / release instructions now live in the `livekit-voice` repo's `README.md`. The old
`open-webui-livekit-*` s6 services and `deploy/livekit/` were retired on 2026-06-27 (see
`deploy/livekit/MOVED.md`).
