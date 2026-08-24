# Raspberry Pi yt-dlp web downloader

A LAN web interface that queues yt-dlp jobs and runs multiple downloads in parallel (currently 2 worker threads).

## Features

- URL input page
- Download queue
- Configurable parallel downloads
- Queue positions
- Per-download live logs
- Playlist option
- MKV output
- Automatic Pi-local or direct-to-NAS downloads based on free space
- Dedicated NAS `Youtube Videos` library
- Starts automatically with systemd

yt-dlp is installed inside the app's Python virtual environment. This avoids
the temporary self-extraction required by its PyInstaller one-file release,
which can fail on space-constrained or hardened Raspberry Pi installations.

## Codec policy

- HDR: AV1 only
- SDR: H.265 (`hvc1` or `hev1`) first
- SDR fallback: H.264 (`avc1`)
- SDR AV1 is excluded

## Install

```bash
unzip pi-ytdlp-web-parallel.zip
cd pi-ytdlp-web-parallel
chmod +x install.sh
./install.sh
```

Open:

```text
http://RASPBERRY_PI_IP:100
```

## Deploy from Visual Studio Code

Set up SSH access once, replacing the hostname if necessary:

```bash
ssh-copy-id pi@raspberrypi.local
```

In Visual Studio Code, press `Ctrl+Shift+B` (`Cmd+Shift+B` on macOS), select
**Deploy to Raspberry Pi**, and enter the Pi's SSH destination when prompted.
The included task runs `deploy.sh`, which copies the project with `rsync`, runs
the installer on the Pi, stops the existing service before replacing its files,
and restarts the systemd service afterward.

You can also deploy from a terminal:

```bash
PI_HOST=pi@192.168.1.100 ./deploy.sh
```

Both the computer and Pi must have `rsync` installed. The deploy task may ask
for the Pi user's `sudo` password.

## Parallel download count

Edit:

```text
/etc/systemd/system/ytdlp-web.service
```

Change:

```ini
Environment=MAX_CONCURRENT_DOWNLOADS=2
```

Then run:

```bash
sudo systemctl daemon-reload
sudo systemctl restart ytdlp-web
```

Keep Gunicorn at one process:

```text
--workers 1
```

The queue and job state are currently stored in process memory. Multiple
Gunicorn processes would create multiple independent queues.

## NAS destination

Default:

```text
/mnt/Videos/Youtube Videos
```

The app and installer create this folder automatically on the mounted NAS.
To use another location, change these entries in the service file:

```ini
Environment="DOWNLOAD_DIR=/your/nas/Youtube Videos"
Environment="NAS_TEMP_DOWNLOAD_DIR=/your/nas/Youtube Videos/.ytdlp-temp"
ReadWritePaths=/your/nas /var/tmp/ytdlp-web
```

## Temporary download directory

Before each job, yt-dlp performs a metadata-only probe for the selected video
and audio formats. The app uses Pi-local storage only when there is enough free
space for twice the estimated media size plus a 512 MiB reserve. The extra
headroom covers the separate video/audio streams and ffmpeg merge output.

If that safe reservation cannot be made, or YouTube does not report a usable
size, the download, fragments, and merge run directly in the hidden
`.ytdlp-temp` folder on the NAS. The completed MKV is then placed in
`Youtube Videos`. Concurrent workers share the reservation calculation so two
jobs cannot both claim the same Pi space.

The default local temporary directory is:

```text
/var/tmp/ytdlp-web
```

To change it, edit both the environment setting and the writable paths in the
service file:

```ini
Environment=TEMP_DOWNLOAD_DIR=/path/on/pi
Environment=TMPDIR=/path/on/pi
ReadWritePaths=/mnt/Videos /path/on/pi
```

Make sure the service user can write to that directory, then reload and restart
the service.

The thresholds are configurable in `ytdlp-web.service`:

```ini
Environment=LOCAL_FREE_RESERVE_BYTES=536870912
Environment=LOCAL_SPACE_MULTIPLIER=2.0
```

## Logs

```bash
sudo journalctl -u ytdlp-web -f
```

The health indicator runs `yt-dlp --version`, so a corrupt or unusable yt-dlp
installation is reported instead of appearing healthy merely because the file
exists.

## Security

This version has no login. Restrict it to your trusted LAN and do not expose
port 100 through router port forwarding.
