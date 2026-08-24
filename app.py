import json
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, redirect, render_template, request, url_for

app = Flask(__name__)

APP_NAME = "YouTube Video Downloader"

DOWNLOAD_DIR = Path(
    os.environ.get(
        "DOWNLOAD_DIR",
        "/mnt/Videos/Youtube Videos",
    )
).resolve()

TEMP_DOWNLOAD_DIR = Path(
    os.environ.get(
        "TEMP_DOWNLOAD_DIR",
        "/var/tmp/ytdlp-web",
    )
).resolve()

NAS_TEMP_DOWNLOAD_DIR = Path(
    os.environ.get(
        "NAS_TEMP_DOWNLOAD_DIR",
        str(DOWNLOAD_DIR / ".ytdlp-temp"),
    )
).resolve()

YTDLP_BIN = os.environ.get(
    "YTDLP_BIN",
    "/usr/local/bin/yt-dlp",
)

MAX_CONCURRENT_DOWNLOADS = max(
    1,
    int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2")),
)

MAX_LOG_LINES = max(
    50,
    int(os.environ.get("MAX_LOG_LINES", "500")),
)

LOCAL_FREE_RESERVE_BYTES = max(
    0,
    int(
        os.environ.get(
            "LOCAL_FREE_RESERVE_BYTES",
            str(512 * 1024 * 1024),
        )
    ),
)

# A merge can temporarily keep the video, audio, and completed output at the
# same time. Reserve twice the estimated media size so the Pi is not filled by
# post-processing after the initial download appeared to fit.
LOCAL_SPACE_MULTIPLIER = max(
    1.0,
    float(os.environ.get("LOCAL_SPACE_MULTIPLIER", "2.0")),
)

YTDLP_ESTIMATE_TIMEOUT = max(
    15,
    int(os.environ.get("YTDLP_ESTIMATE_TIMEOUT", "120")),
)

# HDR:
#   AV1 only.
#
# SDR:
#   Prefer H.265 hvc1, then H.265 hev1, then H.264 avc1.
#   AV1 is never selected for SDR.
FORMAT_SELECTOR = (
    "(bestvideo[dynamic_range^=HDR][vcodec^=av01]+bestaudio)"
    "/(bestvideo[dynamic_range!^=HDR][vcodec^=hvc1]+bestaudio)"
    "/(bestvideo[dynamic_range!^=HDR][vcodec^=hev1]+bestaudio)"
    "/(bestvideo[dynamic_range!^=HDR][vcodec^=avc1]+bestaudio)"
    "/best[dynamic_range!^=HDR][vcodec^=hvc1]"
    "/best[dynamic_range!^=HDR][vcodec^=hev1]"
    "/best[dynamic_range!^=HDR][vcodec^=avc1]"
)

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
job_queue: queue.Queue[str] = queue.Queue()
local_space_reservations: dict[str, int] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_valid_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.netloc)
        )
    except ValueError:
        return False


def append_log(job_id: str, line: str) -> None:
    with jobs_lock:
        job = jobs.get(job_id)

        if job is None:
            return

        job["log"].append(line.rstrip())

        if len(job["log"]) > MAX_LOG_LINES:
            job["log"] = job["log"][-MAX_LOG_LINES:]


def update_job(job_id: str, **updates) -> None:
    with jobs_lock:
        job = jobs.get(job_id)

        if job is not None:
            job.update(updates)


def get_queue_position(job_id: str) -> int | None:
    """
    Returns a 1-based queue position for queued jobs.

    queue.Queue does not expose a public snapshot API, so we briefly lock
    its internal mutex only to read the current pending IDs.
    """
    with job_queue.mutex:
        pending_ids = list(job_queue.queue)

    try:
        return pending_ids.index(job_id) + 1
    except ValueError:
        return None


def media_size_from_info(info: object) -> int | None:
    """Return the selected media size from a yt-dlp JSON response."""
    if not isinstance(info, dict):
        return None

    entries = info.get("entries")
    if isinstance(entries, list):
        if not entries:
            return None
        entry_sizes = [media_size_from_info(entry) for entry in entries]
        if any(size is None for size in entry_sizes):
            return None
        return sum(size for size in entry_sizes if size is not None)

    selected_formats = (
        info.get("requested_downloads")
        or info.get("requested_formats")
    )
    if isinstance(selected_formats, list) and selected_formats:
        sizes: list[int] = []
        for selected_format in selected_formats:
            if not isinstance(selected_format, dict):
                return None
            size = (
                selected_format.get("filesize")
                or selected_format.get("filesize_approx")
            )
            if not isinstance(size, (int, float)) or size <= 0:
                return None
            sizes.append(int(size))
        return sum(sizes)

    size = info.get("filesize") or info.get("filesize_approx")
    if isinstance(size, (int, float)) and size > 0:
        return int(size)
    return None


def estimate_download_size(job: dict) -> tuple[int | None, str | None]:
    """Ask yt-dlp for the selected formats without downloading media."""
    command = [
        YTDLP_BIN,
        "--dump-single-json",
        "--simulate",
        "--no-warnings",
        "--no-progress",
        "--format",
        FORMAT_SELECTOR,
    ]
    if not job["playlist"]:
        command.append("--no-playlist")
    command.append(job["url"])

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=YTDLP_ESTIMATE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        append_log(job["id"], f"Could not estimate download size: {exc}")
        return None, None

    if result.returncode != 0:
        error_line = result.stderr.strip().splitlines()
        detail = error_line[-1] if error_line else "yt-dlp probe failed"
        append_log(job["id"], f"Could not estimate download size: {detail}")
        return None, None

    try:
        info = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        append_log(job["id"], f"Could not read download size estimate: {exc}")
        return None, None

    title = info.get("title") if isinstance(info, dict) else None
    return media_size_from_info(info), title


def reserve_local_space(job_id: str, estimated_bytes: int | None) -> bool:
    """Atomically reserve enough Pi space for a download and its merge."""
    if estimated_bytes is None:
        return False

    required_bytes = math.ceil(estimated_bytes * LOCAL_SPACE_MULTIPLIER)
    try:
        free_bytes = shutil.disk_usage(TEMP_DOWNLOAD_DIR).free
    except OSError:
        return False

    with jobs_lock:
        already_reserved = sum(local_space_reservations.values())
        available_bytes = max(0, free_bytes - already_reserved)
        if required_bytes + LOCAL_FREE_RESERVE_BYTES > available_bytes:
            return False
        local_space_reservations[job_id] = required_bytes
    return True


def release_local_space(job_id: str) -> None:
    with jobs_lock:
        local_space_reservations.pop(job_id, None)


def select_working_directory(
    job_id: str,
    estimated_bytes: int | None,
) -> tuple[Path, str]:
    if reserve_local_space(job_id, estimated_bytes):
        return TEMP_DOWNLOAD_DIR, "pi_local"
    return NAS_TEMP_DOWNLOAD_DIR, "nas_direct"


def download_message(job: dict) -> str:
    if job.get("storage_mode") == "nas_direct":
        return "Downloading directly to NAS storage"
    return "Downloading to Pi temporary storage"


def build_command(job: dict) -> list[str]:
    working_directory = Path(
        job.get("working_directory") or TEMP_DOWNLOAD_DIR
    )
    command = [
        YTDLP_BIN,
        "--newline",
        "--print",
        "before_dl:__YTDLP_TITLE__|%(title)s",
        "--print",
        "after_move:__YTDLP_OUTPUT__|%(filepath)s",
        "--progress",
        "--progress-template",
        (
            "download:__YTDLP_PROGRESS__|"
            "%(progress._percent_str)s|"
            "%(progress._downloaded_bytes_str)s|"
            "%(progress._total_bytes_str)s|"
            "%(progress._speed_str)s|"
            "%(progress._eta_str)s"
        ),
        "--continue",
        "--no-overwrites",
        "--restrict-filenames",
        "--merge-output-format",
        "mkv",
        "--format",
        FORMAT_SELECTOR,
        "--paths",
        str(DOWNLOAD_DIR),
        "--paths",
        f"temp:{working_directory}",
        "--output",
        "%(title).180B [%(id)s].%(ext)s",
    ]

    if not job["playlist"]:
        command.append("--no-playlist")

    command.append(job["url"])
    return command


def download_worker(worker_number: int) -> None:
    while True:
        job_id = job_queue.get()

        try:
            with jobs_lock:
                job = jobs.get(job_id)

                if job is None:
                    continue

                job["worker_number"] = worker_number
                job["started_at"] = utc_now()
                job["status"] = "preparing"
                job["message"] = "Checking video size and Pi storage"

            DOWNLOAD_DIR.mkdir(
                parents=True,
                exist_ok=True,
            )
            TEMP_DOWNLOAD_DIR.mkdir(
                parents=True,
                exist_ok=True,
            )

            estimated_bytes, probed_title = estimate_download_size(job)
            working_directory, storage_mode = select_working_directory(
                job_id,
                estimated_bytes,
            )
            use_local_storage = storage_mode == "pi_local"
            working_directory.mkdir(parents=True, exist_ok=True)

            update_job(
                job_id,
                status="downloading",
                message=(
                    "Downloading to Pi temporary storage"
                    if use_local_storage
                    else "Downloading directly to NAS storage"
                ),
                estimated_bytes=estimated_bytes,
                storage_mode=storage_mode,
                working_directory=str(working_directory),
                title=probed_title or job.get("title"),
            )

            with jobs_lock:
                job = jobs.get(job_id)
                if job is None:
                    continue

            command = build_command(job)

            safe_command = command[:-1] + ["<URL>"]
            append_log(
                job_id,
                "$ " + " ".join(safe_command),
            )

            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            with jobs_lock:
                current_job = jobs.get(job_id)

                if current_job is not None:
                    current_job["process_id"] = process.pid

            if process.stdout is None:
                raise RuntimeError(
                    "yt-dlp did not provide an output stream."
                )

            for line in process.stdout:
                append_log(job_id, line)

                stripped = line.strip()

                if stripped.startswith("__YTDLP_PROGRESS__|"):
                    parts = stripped.split("|", 5)
                    percent_text = parts[1] if len(parts) > 1 else ""
                    match = re.search(r"[\d.]+", percent_text)
                    progress = float(match.group()) if match else 0.0
                    update_job(
                        job_id,
                        status="downloading",
                        message=download_message(job),
                        progress=min(100.0, max(0.0, progress)),
                        downloaded_size=parts[2].strip() if len(parts) > 2 else "",
                        total_size=parts[3].strip() if len(parts) > 3 else "",
                        speed=parts[4].strip() if len(parts) > 4 else "",
                        eta=parts[5].strip() if len(parts) > 5 else "",
                    )
                elif stripped.startswith("__YTDLP_OUTPUT__|"):
                    output_path = stripped.split("|", 1)[1]
                    output_file = Path(output_path)
                    update_job(
                        job_id,
                        output_path=output_path,
                        output_bytes=(
                            output_file.stat().st_size
                            if output_file.exists()
                            else None
                        ),
                    )
                elif stripped.startswith("__YTDLP_TITLE__|"):
                    update_job(
                        job_id,
                        title=stripped.split("|", 1)[1],
                    )
                elif stripped.startswith("[MoveFiles]"):
                    update_job(
                        job_id,
                        status="moving",
                        message=(
                            "Moving completed file to NAS"
                            if job.get("storage_mode") == "pi_local"
                            else "Organizing completed file on NAS"
                        ),
                    )
                elif stripped.startswith(("[Merger]", "[VideoRemuxer]", "[Fixup")):
                    update_job(
                        job_id,
                        status="processing",
                        message=(
                            "Merging and processing on the Pi"
                            if job.get("storage_mode") == "pi_local"
                            else "Merging and processing directly on the NAS"
                        ),
                    )

            return_code = process.wait()

            with jobs_lock:
                current_job = jobs.get(job_id)

                if current_job is not None:
                    current_job["return_code"] = return_code
                    current_job["status"] = (
                        "completed"
                        if return_code == 0
                        else "failed"
                    )
                    current_job["message"] = (
                        "Completed"
                        if return_code == 0
                        else f"yt-dlp exited with code {return_code}"
                    )
                    if return_code == 0:
                        current_job["progress"] = 100.0
                    current_job["finished_at"] = utc_now()
                    current_job["process_id"] = None

        except Exception as exc:
            append_log(
                job_id,
                f"Server error: {exc}",
            )

            with jobs_lock:
                current_job = jobs.get(job_id)

                if current_job is not None:
                    current_job["status"] = "failed"
                    current_job["message"] = str(exc)
                    current_job["finished_at"] = utc_now()
                    current_job["process_id"] = None

        finally:
            release_local_space(job_id)
            job_queue.task_done()


def start_workers() -> None:
    for worker_number in range(
        1,
        MAX_CONCURRENT_DOWNLOADS + 1,
    ):
        thread = threading.Thread(
            target=download_worker,
            args=(worker_number,),
            daemon=True,
            name=f"yt-dlp-worker-{worker_number}",
        )

        thread.start()


def prepare_directories() -> None:
    """Create the Pi workspace and the NAS YouTube library on startup."""
    for directory in (TEMP_DOWNLOAD_DIR, DOWNLOAD_DIR):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Keep the status page available so it can explain an unavailable
            # or read-only mount. A queued job will report the exact error.
            pass


prepare_directories()
start_workers()


def check_ytdlp_health() -> tuple[bool, str | None, str | None]:
    if not Path(YTDLP_BIN).exists():
        return False, None, "yt-dlp executable was not found"
    try:
        result = subprocess.run(
            [YTDLP_BIN, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, None, str(exc)

    version = result.stdout.strip().splitlines()
    if result.returncode == 0 and version:
        return True, version[0], None

    error_lines = (result.stderr or result.stdout).strip().splitlines()
    error = error_lines[-1] if error_lines else (
        f"yt-dlp exited with code {result.returncode}"
    )
    return False, None, error


@app.get("/")
def index():
    with jobs_lock:
        recent_jobs = list(jobs.values())[-50:][::-1]

    for job in recent_jobs:
        if job["status"] == "queued":
            job["queue_position"] = get_queue_position(
                job["id"]
            )
        else:
            job["queue_position"] = None

    return render_template(
        "index.html",
        app_name=APP_NAME,
        jobs=recent_jobs,
        download_dir=str(DOWNLOAD_DIR),
        temp_download_dir=str(TEMP_DOWNLOAD_DIR),
        nas_temp_download_dir=str(NAS_TEMP_DOWNLOAD_DIR),
        max_concurrent_downloads=MAX_CONCURRENT_DOWNLOADS,
        local_space_multiplier=LOCAL_SPACE_MULTIPLIER,
    )


@app.post("/download")
def download():
    url = request.form.get("url", "").strip()
    playlist = request.form.get("playlist") == "on"

    if not is_valid_url(url):
        with jobs_lock:
            recent_jobs = list(jobs.values())[-50:][::-1]

        return (
            render_template(
                "index.html",
                app_name=APP_NAME,
                jobs=recent_jobs,
                download_dir=str(DOWNLOAD_DIR),
                temp_download_dir=str(TEMP_DOWNLOAD_DIR),
                nas_temp_download_dir=str(NAS_TEMP_DOWNLOAD_DIR),
                max_concurrent_downloads=MAX_CONCURRENT_DOWNLOADS,
                local_space_multiplier=LOCAL_SPACE_MULTIPLIER,
                error=(
                    "Enter a valid http:// or https:// URL."
                ),
            ),
            400,
        )

    job_id = uuid.uuid4().hex[:12]

    job = {
        "id": job_id,
        "url": url,
        "playlist": playlist,
        "status": "queued",
        "created_at": utc_now(),
        "started_at": None,
        "finished_at": None,
        "return_code": None,
        "worker_number": None,
        "process_id": None,
        "message": "Queued",
        "title": None,
        "progress": 0.0,
        "downloaded_size": "",
        "total_size": "",
        "speed": "",
        "eta": "",
        "output_path": None,
        "output_bytes": None,
        "estimated_bytes": None,
        "storage_mode": None,
        "working_directory": None,
        "log": [],
    }

    with jobs_lock:
        jobs[job_id] = job

    job_queue.put(job_id)

    return redirect(url_for("index", queued=job_id))


@app.get("/jobs/<job_id>")
def job_page(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)

    if job is None:
        return "Job not found", 404

    return render_template(
        "job.html",
        job=job,
    )


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)

        if job is None:
            return jsonify(
                {"error": "Job not found"}
            ), 404

        result = dict(job)
        result["log"] = list(job["log"])

    if result["status"] == "queued":
        result["queue_position"] = get_queue_position(
            job_id
        )
    else:
        result["queue_position"] = None

    return jsonify(result)


@app.get("/api/jobs")
def all_jobs():
    with jobs_lock:
        result = [
            {
                **dict(job),
                "log": list(job["log"]),
            }
            for job in list(jobs.values())[-50:][::-1]
        ]

    for job in result:
        if job["status"] == "queued":
            job["queue_position"] = get_queue_position(
                job["id"]
            )
        else:
            job["queue_position"] = None

    return jsonify(
        {
            "app_name": APP_NAME,
            "max_concurrent_downloads":
                MAX_CONCURRENT_DOWNLOADS,
            "download_dir": str(DOWNLOAD_DIR),
            "temp_download_dir": str(TEMP_DOWNLOAD_DIR),
            "nas_temp_download_dir": str(NAS_TEMP_DOWNLOAD_DIR),
            "jobs": result,
        }
    )


@app.get("/api/health")
def health():
    ytdlp_exists = Path(YTDLP_BIN).exists()
    ytdlp_ok, ytdlp_version, ytdlp_error = check_ytdlp_health()
    download_dir_exists = DOWNLOAD_DIR.exists()
    download_dir_writable = (
        download_dir_exists
        and os.access(DOWNLOAD_DIR, os.W_OK)
    )

    return jsonify(
        {
            "ok": ytdlp_ok and download_dir_writable,
            "yt_dlp": YTDLP_BIN,
            "yt_dlp_exists": ytdlp_exists,
            "yt_dlp_ok": ytdlp_ok,
            "yt_dlp_version": ytdlp_version,
            "yt_dlp_error": ytdlp_error,
            "download_dir": str(DOWNLOAD_DIR),
            "download_dir_exists": download_dir_exists,
            "download_dir_writable": download_dir_writable,
            "temp_download_dir": str(TEMP_DOWNLOAD_DIR),
            "nas_temp_download_dir": str(NAS_TEMP_DOWNLOAD_DIR),
        }
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=100,
        threaded=True,
    )
