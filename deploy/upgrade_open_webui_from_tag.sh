#!/bin/sh
set -eu

ROOT="/home/pepper/apps/open-webui"
REPO_DEFAULT="pepperpepperpepper/open-webui"
OUT_DIR_DEFAULT="/home/pepper/tmp/wheels"

usage() {
  cat >&2 <<'EOF'
Usage:
  upgrade_open_webui_from_tag.sh <tag> [--repo <owner/repo>] [--out-dir <dir>] [--token <github_token>]

Example:
  upgrade_open_webui_from_tag.sh v0.6.43-pepper.1

What it does:
  - Derives the wheel filename from the pepper tag:
      tag:     v0.6.43-pepper.1
      version: 0.6.43+pepper.1
      wheel:   open_webui-0.6.43+pepper.1-py3-none-any.whl
  - Downloads the wheel from the GitHub Release asset for that tag
  - Pins + restarts Open WebUI via deploy/upgrade_open_webui.sh

Notes:
  - Private forks: set GITHUB_TOKEN (or pass --token) so the script can download
    the release asset via the GitHub API.
  - If you cannot download from GitHub (no token / no network), copy the wheel
    onto the server and run:
      apps/open-webui/deploy/upgrade_open_webui.sh /path/to/open_webui-*.whl --tag <tag>
EOF
}

if [ $# -lt 1 ]; then
  usage
  exit 2
fi

TAG="$1"
shift

REPO="$REPO_DEFAULT"
OUT_DIR="$OUT_DIR_DEFAULT"
TOKEN="${GITHUB_TOKEN:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)
      REPO="${2:-}"
      [ -n "$REPO" ] || { echo "error: --repo requires a value" >&2; exit 2; }
      shift 2
      ;;
    --out-dir)
      OUT_DIR="${2:-}"
      [ -n "$OUT_DIR" ] || { echo "error: --out-dir requires a value" >&2; exit 2; }
      shift 2
      ;;
    --token)
      TOKEN="${2:-}"
      [ -n "$TOKEN" ] || { echo "error: --token requires a value" >&2; exit 2; }
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown arg: $1" >&2
      usage
      exit 2
      ;;
  esac
done

TAG_NO_V="${TAG#v}"
BASE="${TAG_NO_V%-pepper.*}"
N="${TAG_NO_V##*-pepper.}"

case "$TAG" in
  v*-pepper.*) : ;;
  *)
    echo "error: expected tag like v0.6.43-pepper.1, got: $TAG" >&2
    exit 2
    ;;
esac

case "$N" in
  ""|*[!0-9]*)
    echo "error: pepper release number must be digits, got: $N (tag=$TAG)" >&2
    exit 2
    ;;
esac

VERSION="${BASE}+pepper.${N}"
WHEEL="open_webui-${VERSION}-py3-none-any.whl"

mkdir -p "$OUT_DIR"
DEST="$OUT_DIR/$WHEEL"

echo "[info] Downloading wheel:"
echo "  repo=$REPO"
echo "  tag=$TAG"
echo "  dest=$DEST"

if [ -n "$TOKEN" ]; then
  echo "  via=github-api (authenticated)"
  RELEASE_API_URL="https://api.github.com/repos/${REPO}/releases/tags/${TAG}"
  ASSET_ID="$(
    curl -fsSL \
      -H "Authorization: Bearer ${TOKEN}" \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      "$RELEASE_API_URL" \
    | jq -r --arg name "$WHEEL" '.assets[]? | select(.name==$name) | .id' \
    | head -n 1
  )"

  if [ -z "$ASSET_ID" ] || [ "$ASSET_ID" = "null" ]; then
    echo "error: could not find asset '$WHEEL' on GitHub Release tag '$TAG' (repo=$REPO)" >&2
    exit 1
  fi

  curl -fSL \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Accept: application/octet-stream" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    -o "$DEST" \
    "https://api.github.com/repos/${REPO}/releases/assets/${ASSET_ID}"
else
  echo "  via=https (unauthenticated)"
  WHEEL_URL_NAME="$(printf '%s' "$WHEEL" | sed 's/+/%2B/g')"
  URL="https://github.com/${REPO}/releases/download/${TAG}/${WHEEL_URL_NAME}"
  echo "  url=$URL"
  curl -fSL -o "$DEST" "$URL"
fi

cd "$ROOT"
./deploy/upgrade_open_webui.sh "$DEST" --tag "$TAG"
