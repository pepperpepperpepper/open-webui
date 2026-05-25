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
- **Cartesia STT/TTS via OpenAI-compatible settings** (backend):
  - `backend/open_webui/routers/audio.py`
  - Adds Cartesia `/voices` listing (with `Cartesia-Version` header), routes TTS to `/tts/bytes`, and normalizes STT model/language for Cartesia.
- **Serper snippet field compatibility** (backend):
  - `backend/open_webui/retrieval/web/serper.py`
  - Uses `snippet` (fallback `description`) for result excerpts so Web Search context is actually populated.
- **Web Search “always” enforced server-side** (backend):
  - `backend/open_webui/utils/middleware.py`
  - Mirrors the UI “Web Search: Always” setting on the backend so web search doesn’t silently disable if `/api/config` can’t be read as authenticated.
  - Also avoids spamming the UI with “Searching the web → No search query generated” when the query gatekeeper decides search isn’t needed.
- **Truthful web access notice** (backend):
  - `backend/open_webui/utils/middleware.py`
  - Adds a small, idempotent system prompt appendix (only for “can you browse/search?” meta questions) to stop models from claiming they “searched the web” unless web-search sources are actually present in the prompt.
- **Cerebras provider timing logs** (backend):
  - `backend/open_webui/routers/openai.py`
  - Adds high-signal logs for Cerebras requests (start → headers → TTFT → done/error).
- **Voice mode resilience** (frontend):
  - `src/lib/components/chat/MessageInput/CallOverlay.svelte`
  - Uses a timer loop instead of `requestAnimationFrame` so the audio loop isn’t fully paused when the tab is backgrounded.
- **ChatControls ResizeObserver guard** (frontend):
  - `src/lib/components/chat/ChatControls.svelte`
  - Prevents a race where `pane` becomes `null` while the observer callback still runs.
- **Send to LiveKit Voice button** (frontend):
  - `src/lib/components/pepper/SendToLiveKit.svelte`
  - `src/lib/components/chat/MessageInput.svelte`
  - Adds a one-click “Send to LiveKit” button that opens `/livekit/` with `room=<chat>` + `chat_id=<chat>` so voice sessions stay tied to a chat.
- **LiveKit portal deep-linking + chat context import** (deploy):
  - `deploy/livekit/index.html`
  - Supports `/livekit/?room=...&chat_id=...` and auto-loads that chat as context (and auto-sends on connect).
- **Backend lazy-imports for low-RAM startup** (backend):
  - `backend/open_webui/env.py` (`OPEN_WEBUI_SKIP_DOTENV` escape hatch)
  - `backend/open_webui/config.py` (don't import `chromadb` just to read default tenant/db names)
  - `backend/open_webui/retrieval/vector/factory.py` (`LazyVectorDBClient` proxy)
  - `backend/open_webui/storage/provider.py` (lazy boto3/google/azure imports; `LazyStorageProviderProxy`)
  - `backend/open_webui/routers/{files,images,models,tasks}.py` (imports moved into handler bodies; `retrieval.py` dropped at v0.9.5-pepper.1 because it conflicted on every hunk with upstream's async refactor)
  - Defers heavy imports until first call so the backend boots without OOM'ing on a memory-tight host.
- **Safer model-dict access helpers** (backend):
  - `backend/open_webui/utils/models.py` adds `get_model_info`/`get_model_meta`/`get_model_params`/`get_model_capabilities`/`normalize_model_dict`.
  - Used by `backend/open_webui/{main.py, utils/middleware.py, utils/chat.py, routers/tasks.py}` to avoid crashing when `info.meta` or `info.params` is `None`/non-dict.
- **Slim wheel** (packaging):
  - 16 files removed from `backend/open_webui/static/` (favicons, splash, manifest, custom.css, loader.js, logo, user-import.csv).
  - Production deploy sets `STATIC_DIR=/home/arch/data/open-webui/static` which holds the real assets.
- **`OPEN_WEBUI_SKIP_FRONTEND_BUILD` hatch hook** (packaging):
  - `hatch_build.py` short-circuits the npm build step when a prebuilt `build/` is present (and errors loudly if it's missing), so CI can build the frontend once and have hatch reuse it.
- **LiveKit agent: env-driven knobs + wake-word/strip pipeline + Cartesia TTS fixtures** (deploy):
  - `deploy/livekit/agent.py`, `deploy/livekit/portal.py`, `deploy/livekit/index.html`
  - `deploy/livekit/render_phrases_wav.py` + `deploy/livekit/tts_wavs/` + `tests/assets/zwingli_round*/` (regression fixtures for wake-word + prompt-injection-style preambles).
- **Memory-pressure tooling** (deploy):
  - `deploy/MEMORY_PRESSURE.md` runbook
  - `deploy/capture_memory_pressure.sh`, `deploy/watch_memory_pressure.sh`
  - `deploy/s6/open-webui-pressure-watch/` s6 service template

## Deployment ownership boundary

- **Deployment/integration wiring** (s6, nginx, env, provider keys, server-specific scripts) lives outside this repo
  (on the server under `/var/www/open-webui/deploy/`).
- **Product patches** (anything that changes Open WebUI behavior/UI itself) should live in this fork.

## Update workflow

1. Merge upstream into the fork (merge or rebase, your preference).
2. Apply/adjust patches and update this file.
3. Create a new tag (example: `v0.6.43-pepper.1`).
4. CI builds a wheel from that tag and attaches it to the GitHub Release.
5. Server deploy: bump the pinned wheel URL in `/var/www/open-webui/pyproject.toml` and restart Open WebUI.

## Rollback

Re-pin the server to the previous wheel URL and restart.
