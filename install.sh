#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/pi-ytdlp-web"
SERVICE_NAME="ytdlp-web"
SERVICE_USER="${SUDO_USER:-pi}"

sudo apt update

sudo apt install -y \
  python3 \
  python3-venv \
  ffmpeg

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

# Create the dedicated library folder on the mounted NAS. If the service user
# cannot create it, the app health check will report the mount as unavailable.
sudo -u "$SERVICE_USER" \
  mkdir -p "/mnt/Videos/Youtube Videos"

if [ ! -d "$APP_DIR/.venv" ]; then
  sudo -u "$SERVICE_USER" \
    python3 -m venv "$APP_DIR/.venv"
fi

sudo -u "$SERVICE_USER" \
  "$APP_DIR/.venv/bin/pip" \
  install --upgrade pip

sudo -u "$SERVICE_USER" \
  "$APP_DIR/.venv/bin/pip" \
  install --upgrade -r "$APP_DIR/requirements.txt"

# Use the Python entry point from the app virtual environment. Unlike the
# PyInstaller one-file release, this does not need to unpack itself into a
# temporary directory before every command.
sudo ln -sf \
  "$APP_DIR/.venv/bin/yt-dlp" \
  /usr/local/bin/yt-dlp

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
