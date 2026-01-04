#!/bin/sh
set -eu

ROOT="/home/pepper/apps/open-webui"
DEPLOY_DIR="$ROOT/deploy"

usage() {
  cat >&2 <<'EOF'
Usage:
  pin_open_webui_wheel.sh /path/to/open_webui-*.whl [--tag <tag>] [--no-lock] [--no-install]

What it does:
  - Copies the wheel into /home/pepper/apps/open-webui/deploy/vendor/
  - Pins apps/open-webui/deploy/pyproject.toml to that local wheel (no git build, no npm build)
  - Optionally updates poetry.lock and reinstalls the venv

Notes:
  - This is designed for low-RAM servers: it never runs the Open WebUI frontend build locally.
  - If your fork wheel keeps the upstream version number (e.g. still "0.6.43"), Poetry may not reinstall it.
    In that case, we force-reinstall the wheel with pip at the end (without touching deps).
EOF
}

if [ $# -lt 1 ]; then
  usage
  exit 2
fi

WHEEL_SRC="$1"
shift

TAG=""
DO_LOCK=1
DO_INSTALL=1

while [ $# -gt 0 ]; do
  case "$1" in
    --tag)
      TAG="${2:-}"
      [ -n "$TAG" ] || { echo "error: --tag requires a value" >&2; exit 2; }
      shift 2
      ;;
    --no-lock)
      DO_LOCK=0
      shift
      ;;
    --no-install)
      DO_INSTALL=0
      shift
      ;;
    -*)
      echo "error: unknown flag: $1" >&2
      usage
      exit 2
      ;;
    *)
      echo "error: unexpected argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [ ! -f "$WHEEL_SRC" ]; then
  echo "error: wheel not found: $WHEEL_SRC" >&2
  exit 1
fi

cd "$ROOT"

mkdir -p "$DEPLOY_DIR/vendor"
WHEEL_NAME="$(basename "$WHEEL_SRC")"
WHEEL_DEST="deploy/vendor/$WHEEL_NAME"
cp -f "$WHEEL_SRC" "$WHEEL_DEST"

SHA256="$(
  python3 - "$WHEEL_DEST" <<'PY'
import hashlib
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
h = hashlib.sha256()
with path.open("rb") as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b""):
        h.update(chunk)
print(h.hexdigest())
PY
)"

python3 - "$WHEEL_DEST" <<'PY'
import pathlib
import re
import sys

wheel_rel = sys.argv[1]
pyproject = pathlib.Path("deploy/pyproject.toml")
text = pyproject.read_text(encoding="utf-8")

pattern = r"(?m)^open-webui\s*=\s*.*$"
replacement = f'open-webui = {{ path = "{wheel_rel}" }}'
new_text, n = re.subn(pattern, replacement, text)
if n != 1:
    raise SystemExit(f"error: expected to replace 1 open-webui dependency line, replaced {n}")
pyproject.write_text(new_text, encoding="utf-8")
PY

if [ "$DO_LOCK" -eq 1 ]; then
  (cd "$DEPLOY_DIR" && poetry lock)
fi

if [ "$DO_INSTALL" -eq 1 ]; then
  (cd "$DEPLOY_DIR" && poetry install)
  (cd "$DEPLOY_DIR" && poetry run pip install --no-deps --force-reinstall "$ROOT/$WHEEL_DEST")
fi

python3 - "$WHEEL_DEST" "$SHA256" "$TAG" <<'PY'
import json
import os
import sys
import time

wheel_path, sha256, tag = sys.argv[1], sys.argv[2], sys.argv[3]

pin = {
    "wheel_path": wheel_path,
    "wheel_sha256": sha256,
    "tag": tag or None,
    "pinned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}

os.makedirs("deploy", exist_ok=True)
with open("deploy/open_webui_wheel_pin.json", "w", encoding="utf-8") as f:
    json.dump(pin, f, indent=2, sort_keys=True)
    f.write("\n")
PY

echo "Pinned open-webui wheel:"
echo "  $WHEEL_DEST"
echo "  sha256=$SHA256"
if [ -n "$TAG" ]; then
  echo "  tag=$TAG"
fi
