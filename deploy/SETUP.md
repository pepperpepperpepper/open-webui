# Open WebUI on chat.uh-oh.wtf (server notes)

## What’s already prepared

- App + Poetry venv: `/home/pepper/apps/open-webui`
- Env file (includes `WEBUI_SECRET_KEY`): `/home/pepper/apps/open-webui/.env`
- Data dir (uploads, cache, etc): `/home/pepper/data/open-webui`
- Static assets dir (favicons, swagger-ui, fonts): `/home/pepper/data/open-webui/static`
- Persistence DB (Postgres): `open_webui` on `127.0.0.1:5432` (role `open_webui`)
- s6 service template: `/home/pepper/apps/open-webui/deploy/s6/open-webui`
- nginx vhost template: `/home/pepper/apps/open-webui/deploy/nginx.chat.uh-oh.wtf.conf`

## Start Open WebUI manually (no root)

```sh
cd /home/pepper/apps/open-webui
set -a; . ./.env; set +a
./.venv/bin/open-webui serve --host 127.0.0.1 --port 8080
```

## Postgres persistence

Open WebUI uses `DATABASE_URL` from `/home/pepper/apps/open-webui/.env`.

- Current value: `postgresql://open_webui@127.0.0.1:5432/open_webui`
- Old SQLite DB (backup): `/home/pepper/data/open-webui/webui.db.bak-*`

To (re)run the SQLite → Postgres copy:

```sh
cd /home/pepper/apps/open-webui
sudo s6-svc -d /service/open-webui
set -a; . ./.env; set +a
./.venv/bin/python deploy/migrate_sqlite_to_postgres.py --wipe-destination
sudo s6-svc -u /service/open-webui
```

## Enable via s6 (root)

```sh
cp -a /home/pepper/apps/open-webui/deploy/s6/open-webui /service/open-webui
```

To control it (root):

```sh
s6-svc -u /service/open-webui   # up
s6-svc -d /service/open-webui   # down
s6-svc -r /service/open-webui   # restart
```

Logs:

- `/home/pepper/logs/open-webui/current` (and rotated files next to it)

## Enable nginx vhost (root)

```sh
cp /home/pepper/apps/open-webui/deploy/nginx.chat.uh-oh.wtf.conf /etc/nginx/sites-available/chat.uh-oh.wtf
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
2. Add these to `/home/pepper/apps/open-webui/.env` (quote values), then restart:

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
2. On your laptop: `ssh -L 8080:127.0.0.1:8080 pepper@uh-oh.wtf`
3. Visit `http://localhost:8080` and create the first user (becomes admin).
4. Confirm signups are disabled, then enable the nginx vhost.

### Remote providers

Add your provider keys to `/home/pepper/apps/open-webui/.env` (or via the WebUI), then restart the service:

- `sudo s6-svc -r /service/open-webui`

### Provider prefixes (Groq/Cerebras visibility)

Open WebUI de-duplicates OpenAI-compatible models by `id` across all `OPENAI_API_BASE_URLS`. If you use OpenRouter *and* direct providers, overlapping model IDs can make a provider’s models appear “missing”.

This server sets OpenAI `api_configs` prefixes so Groq/Cerebras models stay visible and searchable:

- Groq: `groq.<model_id>`
- Cerebras: `cerebras.<model_id>`

This config lives in Postgres (`config.data.openai.api_configs`) and should survive upgrades, but you can re-apply it at any time:

```sh
cd /home/pepper/apps/open-webui
set -a; . ./.env; set +a
./.venv/bin/python deploy/ensure_openai_provider_prefixes.py
sudo s6-svc -r /service/open-webui
```

### Default model

This server pins the default chat model to Cerebras GLM-4.6:

```sh
cd /home/pepper/apps/open-webui
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
cd /home/pepper/apps/open-webui
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
cd /home/pepper/apps/open-webui
./deploy/upgrade_open_webui_from_tag.sh v0.6.43-pepper.1
```

Private fork: set a token first (or pass `--token`):

```sh
export GITHUB_TOKEN='...'
cd /home/pepper/apps/open-webui
./deploy/upgrade_open_webui_from_tag.sh v0.6.43-pepper.1
```

3) Or, upgrade from a wheel you already have locally:

```sh
cd /home/pepper/apps/open-webui
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
- Requires `CARTESIA_API_KEY` in `/home/pepper/.api-keys` and a `Cartesia-Version` (set via `AUDIO_TTS_OPENAI_PARAMS` in `/home/pepper/apps/open-webui/.env`).
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

## LiveKit Voice (Cartesia STT/TTS + Cerebras GLM-4.6)

This repo includes a small “portal” page + a LiveKit Agent proof-of-concept:

- Portal: `deploy/livekit/portal.py` (serves `deploy/livekit/index.html` + mints LiveKit room tokens)
- Agent: `deploy/livekit/agent.py` (Cartesia STT → Cerebras LLM → Cartesia TTS)

This setup supports **either** a LiveKit Cloud URL **or** a **self-hosted** LiveKit server.

### Python deps (already wired into this Poetry project)

The deployment wrapper now includes:

- `livekit-agents` (Cartesia/OpenAI/turn-detector extras)
- `livekit-api`

Install/upgrade the venv:

```sh
cd /home/pepper/apps/open-webui
poetry install
```

### Self-hosted LiveKit server (recommended)

1) Install `livekit-server`:

```sh
sudo install -d -m 0755 /usr/local/bin
sudo curl -fsSL -o /usr/local/bin/livekit-server \
  https://github.com/livekit/livekit/releases/latest/download/livekit-server-linux-amd64
sudo chmod 0755 /usr/local/bin/livekit-server
```

2) Generate a local LiveKit config + credentials (does not print secrets):

```sh
cd /home/pepper/apps/open-webui
./.venv/bin/python deploy/livekit/setup_self_hosted.py
```

This creates:

- `deploy/livekit/livekit.yaml` (LiveKit server config; includes API key/secret)
- `deploy/livekit/livekit.env` (exported env vars for portal + agent)

### Required env vars

The portal + agent read env vars from:

- `/home/pepper/apps/open-webui/.env` (provider keys; already sources `/home/pepper/.api-keys`)
- `/home/pepper/apps/open-webui/deploy/livekit/livekit.env` (LiveKit URL + credentials)

```sh
# LiveKit server
LIVEKIT_URL='wss://YOUR-LIVEKIT-WS-URL'
LIVEKIT_API_KEY='...'
LIVEKIT_API_SECRET='...'

# LiveKit Agent dispatch name (portal + agent must match)
LIVEKIT_AGENT_NAME='owui-voice'

# Cartesia (STT + TTS)
CARTESIA_API_KEY='...'

# Cerebras (LLM)
CEREBRAS_API_KEY='...'

# Optional overrides
# LIVEKIT_LLM_MODEL='zai-glm-4.6'
# LIVEKIT_STT_MODEL='ink-whisper'
# LIVEKIT_STT_LANGUAGE='en'
# LIVEKIT_TTS_MODEL='sonic-2'
# LIVEKIT_TTS_VOICE='f786b574-daa5-4673-aa0c-cbe3e8534c02'
```

### Run manually (no root)

Terminal 1 (portal on `127.0.0.1:8091`):

```sh
cd /home/pepper/apps/open-webui
set -a; . ./.env; . ./deploy/livekit/livekit.env; set +a
./.venv/bin/python deploy/livekit/portal.py
```

Terminal 2 (agent):

```sh
cd /home/pepper/apps/open-webui
set -a; . ./.env; . ./deploy/livekit/livekit.env; set +a
./.venv/bin/python deploy/livekit/agent.py start
```

Pre-download optional voice models (VAD / turn detector) to avoid first-call latency:

```sh
./.venv/bin/python deploy/livekit/agent.py download-files
```

### nginx routing (recommended)

Update the nginx vhost to route `https://chat.uh-oh.wtf/livekit/` to the portal on `127.0.0.1:8091`.
The template in `deploy/nginx.chat.uh-oh.wtf.conf` includes ready-to-copy blocks for:

- `location ^~ /livekit/ { ... }` (portal)
- `location ^~ /lk/ { ... }` (self-hosted LiveKit server)

### s6 services (recommended)

Templates are provided:

- `deploy/s6/open-webui-livekit-portal`
- `deploy/s6/open-webui-livekit-agent`
- `deploy/s6/open-webui-livekit-server`
