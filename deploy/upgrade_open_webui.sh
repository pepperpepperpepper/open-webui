#!/bin/sh
set -eu

if [ $# -lt 1 ]; then
  cat >&2 <<'EOF'
Usage:
  upgrade_open_webui.sh /path/to/open_webui-*.whl [--tag <tag>]

This is a convenience wrapper that:
  1) Pins the Poetry env to the given wheel (no frontend build on this server)
  2) Re-applies local deployment tweaks and restarts the service

EOF
  exit 2
fi

WHEEL="$1"
shift

TAG_ARGS=""
if [ "${1:-}" = "--tag" ]; then
  TAG="${2:-}"
  [ -n "$TAG" ] || { echo "error: --tag requires a value" >&2; exit 2; }
  TAG_ARGS="--tag $TAG"
fi

cd /home/pepper/apps/open-webui

S6_SERVICES="open-webui open-webui-livekit-server open-webui-livekit-portal open-webui-livekit-agent"
S6_UP_BEFORE=""

for svc in $S6_SERVICES; do
  if sudo test -d "/service/$svc"; then
    if [ "$(sudo s6-svstat -o up "/service/$svc")" = "true" ]; then
      S6_UP_BEFORE="$S6_UP_BEFORE $svc"
    fi

    echo "[info] Stopping s6 service: $svc"
    sudo s6-svc -d "/service/$svc" || true
  fi
done

./deploy/pin_open_webui_wheel.sh "$WHEEL" $TAG_ARGS
./deploy/reapply_after_open_webui_upgrade.sh

for svc in $S6_SERVICES; do
  if sudo test -d "/service/$svc" && sudo test -d "/home/pepper/apps/open-webui/deploy/s6/$svc"; then
    echo "[info] Syncing s6 template: $svc"
    sudo cp -a "/home/pepper/apps/open-webui/deploy/s6/$svc/." "/service/$svc/" || true
  fi
done

for svc in $S6_UP_BEFORE; do
  if sudo test -d "/service/$svc"; then
    echo "[info] Starting s6 service: $svc"
    sudo s6-svc -u "/service/$svc" || true
  fi
done
