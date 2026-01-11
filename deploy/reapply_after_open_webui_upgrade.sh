#!/bin/sh
set -eu

cd /home/pepper/apps/open-webui

# Load secrets + config (this file is intentionally a shell script, not a strict .env)
set -a
. /home/pepper/apps/open-webui/.env
set +a

OPEN_WEBUI_VERSION="$(
  /home/pepper/apps/open-webui/.venv/bin/python - <<'PY'
import importlib.metadata as m

print(m.version("open-webui"))
PY
)"

case "$OPEN_WEBUI_VERSION" in
  *+pepper.*)
    echo "[info] open-webui=$OPEN_WEBUI_VERSION: skipping apply_local_patches.py (patches baked into fork wheel)"
    ;;
  *)
    /home/pepper/apps/open-webui/.venv/bin/python /home/pepper/apps/open-webui/deploy/apply_local_patches.py
    ;;
esac

/home/pepper/apps/open-webui/.venv/bin/python /home/pepper/apps/open-webui/deploy/sync_openai_provider_keys_from_env.py
/home/pepper/apps/open-webui/.venv/bin/python /home/pepper/apps/open-webui/deploy/ensure_openai_provider_prefixes.py
/home/pepper/apps/open-webui/.venv/bin/python /home/pepper/apps/open-webui/deploy/ensure_default_models.py \
  --force \
  --default-model "cerebras.zai-glm-4.7" \
  --pinned-models "cerebras.zai-glm-4.7" \
  --task-model-external "cerebras.zai-glm-4.7"

/home/pepper/apps/open-webui/.venv/bin/python /home/pepper/apps/open-webui/deploy/ensure_user_default_models.py \
  --force \
  --email "peppersclothescult@gmail.com" \
  --models "cerebras.zai-glm-4.7" \
  --pinned-models "cerebras.zai-glm-4.7"

sudo s6-svc -r /service/open-webui
