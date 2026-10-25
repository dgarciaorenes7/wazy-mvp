#!/usr/bin/env bash
# Start the Wazy MVP. Needs Python 3.9+ and nothing else.
#   ./run.sh           this computer only (http://127.0.0.1:8765/demo)
#   ./run.sh --phone   also opens an ngrok tunnel so a phone can scan the QR code
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. On a Mac: xcode-select --install" >&2
  exit 1
fi

if [[ "${1:-}" == "--phone" ]]; then
  if ! command -v ngrok >/dev/null 2>&1; then
    echo "ngrok is not installed. On a Mac: brew install ngrok, then ngrok config add-authtoken <token>" >&2
    exit 1
  fi
  ngrok http "${WAZY_PORT:-8765}" --log=stdout >/dev/null 2>&1 &
  NGROK_PID=$!
  trap 'kill "$NGROK_PID" 2>/dev/null || true' EXIT
fi

python3 -m wazy
