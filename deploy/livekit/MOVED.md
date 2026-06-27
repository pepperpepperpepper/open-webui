# Moved — the LiveKit voice backend lives in its own project now

On **2026-06-27** the LiveKit voice backend (portal, agent, web demo, server config,
TTS fillers) was extracted out of this repo into a standalone project.

- **Repo:** `git@git.uh-oh.wtf:livekit-voice.git` — browse: <https://git.uh-oh.wtf/livekit-voice.git>
- **Deploy (this host):** `/var/www/livekit-voice` (own venv + env), s6 services
  **`livekit-voice-{server,portal,agent}`** on **:8191** (portal) / **:7890** (LiveKit WS) /
  **:7891** (RTC TCP) / UDP **50200–50300**. nginx still maps `/livekit/`→portal and `/lk/`→server.
- **Identity provider is still Open WebUI:** the portal validates OWUI JWTs via the shared
  **`WEBUI_SECRET_KEY`** (must stay identical to this repo's `.env`). The only owui-voice-specific
  code remaining in THIS repo is the OAuth app-redirect patch
  (`deploy/apply_local_patches.py::_patch_oauth_app_redirect`, `?app=1` → `oopswtfvoice://`).
- The old `open-webui-livekit-{server,portal,agent}` s6 services were retired the same day.

See the livekit-voice repo's `README.md` for deploy details.
