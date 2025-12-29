#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "" || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'EOF'
Usage:
  ./scripts/pepper-release.sh <n>

Example:
  ./scripts/pepper-release.sh 1

What it does:
  - Sets package.json version to "<base>+pepper.<n>"
  - Commits the change
  - Creates an annotated git tag "v<base>-pepper.<n>"

Notes:
  - The wheel version comes from package.json (via pyproject.toml hatch version hook).
  - A unique version avoids Poetry treating “same version, different source” as a no-op install.
EOF
  exit 2
fi

pepper_n="$1"
if ! [[ "$pepper_n" =~ ^[0-9]+$ ]]; then
  echo "error: <n> must be an integer (got: $pepper_n)" >&2
  exit 2
fi

if [[ -n "$(git status --porcelain=v1)" ]]; then
  echo "error: working tree is not clean; commit/stash first" >&2
  git status --porcelain=v1 >&2
  exit 1
fi

current_version="$(node -p "require('./package.json').version")"
base_version="${current_version%%+*}"
new_version="${base_version}+pepper.${pepper_n}"
tag="v${base_version}-pepper.${pepper_n}"

echo "Bumping package.json version:"
echo "  $current_version -> $new_version"

python3 - <<'PY' "$new_version"
import pathlib
import re
import sys

new_version = sys.argv[1]
package_path = pathlib.Path("package.json")
text = package_path.read_text(encoding="utf-8")

new_text, n = re.subn(
    r'(?m)^(\t"version"\s*:\s*)"[^"]*"(,?)$',
    rf'\1"{new_version}"\2',
    text,
    count=1,
)
if n != 1:
    raise SystemExit(f"error: expected to replace 1 version line, replaced {n}")

package_path.write_text(new_text, encoding="utf-8")
PY

git add package.json
git commit -m "chore: pepper release ${tag}"
git tag -a "${tag}" -m "${tag}"

echo "Created tag: ${tag}"
echo "Next:"
echo "  git push origin HEAD"
echo "  git push origin ${tag}"
