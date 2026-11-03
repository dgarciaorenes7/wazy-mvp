#!/usr/bin/env bash
# Start Wazy. Needs Python 3.9 or newer and nothing else.
#   ./run.sh           this computer only: open http://127.0.0.1:8765/demo
#   ./run.sh --phone   also opens a tunnel, so phones can scan the QR code on the presenter view
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. On a Mac: xcode-select --install" >&2
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "Wazy needs Python 3.9 or newer. This computer has $(python3 --version 2>&1)." >&2
  exit 1
fi

case "${1:-}" in
  "") ;;
  --phone)
    if ! command -v ngrok >/dev/null 2>&1; then
      echo "The tunnel tool is not installed. On a Mac: brew install ngrok, then ngrok config add-authtoken <token>" >&2
      exit 1
    fi
    # The port from the settings (.env included); if the settings are wrong, Wazy explains why just below.
    PORT_NUMBER=$(python3 -c 'from wazy.config import load_config; print(load_config().port)' 2>/dev/null || echo 8765)
    ngrok http "$PORT_NUMBER" --log=stdout >/dev/null 2>&1 &
    TUNNEL_PID=$!
    trap 'kill "$TUNNEL_PID" 2>/dev/null || true' EXIT
    ;;
  *)
    echo "Usage: ./run.sh [--phone]" >&2
    exit 1
    ;;
esac

python3 -m wazy
