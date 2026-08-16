#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/pi-ytdlp-web"
SERVICE_NAME="ytdlp-web"
SERVICE_USER="${SUDO_USER:-pi}"
ARCHITECTURE="$(uname -m)"

sudo apt update

sudo apt install -y \
  python3 \
  python3-venv \
  ffmpeg \
  curl \
  unzip

case "$ARCHITECTURE" in
  aarch64|arm64)
    YTDLP_URL="https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux_aarch64"
    ;;
  x86_64|amd64)
    YTDLP_URL="https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux"
    ;;
  *)
    echo "Unsupported prebuilt yt-dlp architecture: $ARCHITECTURE"
    echo "Installing yt-dlp inside the Python virtual environment instead."
    YTDLP_URL=""
    ;;
esac

# Stop the existing instance before replacing application files. This is safe
# on a first install, where the service does not exist yet.
sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true

sudo mkdir -p "$APP_DIR"

sudo cp app.py "$APP_DIR/app.py"
sudo cp requirements.txt "$APP_DIR/requirements.txt"
sudo rm -rf "$APP_DIR/templates"
sudo cp -r templates "$APP_DIR/templates"

sudo chown -R \
  "$SERVICE_USER:$SERVICE_USER" \
  "$APP_DIR"

sudo mkdir -p /var/tmp/ytdlp-web
sudo chown \
  "$SERVICE_USER:$SERVICE_USER" \
  /var/tmp/ytdlp-web

if [ ! -d "$APP_DIR/.venv" ]; then
  sudo -u "$SERVICE_USER" \
    python3 -m venv "$APP_DIR/.venv"
fi

sudo -u "$SERVICE_USER" \
  "$APP_DIR/.venv/bin/pip" \
  install --upgrade pip

sudo -u "$SERVICE_USER" \
  "$APP_DIR/.venv/bin/pip" \
  install -r "$APP_DIR/requirements.txt"

if [ -n "$YTDLP_URL" ]; then
  sudo curl -L \
    "$YTDLP_URL" \
    -o /usr/local/bin/yt-dlp

  sudo chmod 0755 \
    /usr/local/bin/yt-dlp
else
  sudo -u "$SERVICE_USER" \
    "$APP_DIR/.venv/bin/pip" \
    install --upgrade yt-dlp

  sudo ln -sf \
    "$APP_DIR/.venv/bin/yt-dlp" \
    /usr/local/bin/yt-dlp
fi

sudo cp \
  ytdlp-web.service \
  "/etc/systemd/system/$SERVICE_NAME.service"

sudo sed -i \
  "s/^User=.*/User=$SERVICE_USER/; s/^Group=.*/Group=$SERVICE_USER/" \
  "/etc/systemd/system/$SERVICE_NAME.service"

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo
echo "Service status:"
sudo systemctl \
  --no-pager \
  --full \
  status "$SERVICE_NAME" || true

echo
echo "Open:"
echo "http://$(hostname -I | awk '{print $1}'):100"
