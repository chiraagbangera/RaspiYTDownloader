# Raspberry Pi yt-dlp web downloader

A LAN web interface that queues yt-dlp jobs and runs multiple downloads in
parallel.

## Features

- URL input page
- Download queue
- Configurable parallel downloads
- Queue positions
- Per-download live logs
- Playlist option
- MKV output
- Pi-local temporary downloads and merges
- NAS destination
- Starts automatically with systemd

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
/mnt/nas/downloads
```

Change both entries in the service file:

```ini
Environment=DOWNLOAD_DIR=/mnt/nas/downloads
ReadWritePaths=/mnt/nas/downloads /var/tmp/ytdlp-web
```

## Temporary download directory

Downloads, fragments, and ffmpeg merges are written to the Pi's local storage
first. yt-dlp moves only the completed file to the NAS destination.

The default local temporary directory is:

```text
/var/tmp/ytdlp-web
```

To change it, edit both the environment setting and the writable paths in the
service file:

```ini
Environment=TEMP_DOWNLOAD_DIR=/path/on/pi
ReadWritePaths=/mnt/nas/downloads /path/on/pi
```

Make sure the service user can write to that directory, then reload and restart
the service.

## Logs

```bash
sudo journalctl -u ytdlp-web -f
```

## Security

This version has no login. Restrict it to your trusted LAN and do not expose
port 100 through router port forwarding.
