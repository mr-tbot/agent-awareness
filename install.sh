#!/usr/bin/env bash
# agent-awareness installer (Linux + systemd).
#   ./install.sh              install to ~/.local/bin and add the hooks
#   ./install.sh --no-hooks   install the binary only
#   ./install.sh --prefix DIR install somewhere else
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="$HOME/.local/bin"
HOOKS=1

while [ $# -gt 0 ]; do
  case "$1" in
    --no-hooks) HOOKS=0 ;;
    --prefix) shift; PREFIX="${1:?--prefix needs a directory}" ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' \
  || { echo "python3 3.8+ is required (found $PYV)" >&2; exit 1; }

mkdir -p "$PREFIX"
ln -sfn "$ROOT/aw.py" "$PREFIX/aw"
echo "[ok] aw -> $PREFIX/aw"

case ":$PATH:" in
  *":$PREFIX:"*) ;;
  *) echo "[warn] $PREFIX is not on your PATH. Add this to your shell profile:"
     echo "         export PATH=\"\$PATH:$PREFIX\"" ;;
esac

if [ "$HOOKS" = 1 ]; then
  "$ROOT/aw.py" install
else
  echo "[skip] hooks not installed (--no-hooks). Run 'aw install' later."
fi

echo
"$ROOT/aw.py" doctor || true
