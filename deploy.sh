#!/usr/bin/env bash
set -euo pipefail

PI_HOST="${PI_HOST:-pi@raspberrypi.local}"
REMOTE_DIR="${REMOTE_DIR:-/tmp/pi-ytdlp-web-deploy}"

if ! command -v rsync >/dev/null 2>&1; then
  echo "Error: rsync is required on this computer."
  exit 1
fi

echo "Deploying to $PI_HOST..."

rsync -az --delete \
  --exclude=.git \
  --exclude=.idea \
  --exclude=.vscode \
  --exclude=__pycache__ \
  ./ "$PI_HOST:$REMOTE_DIR/"

# Allocate a terminal so sudo can request a password when the Pi is not
# configured for passwordless sudo.
ssh -t "$PI_HOST" \
  "cd '$REMOTE_DIR' && chmod +x install.sh && sudo ./install.sh"

echo "Deployment complete."
