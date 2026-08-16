import os
import queue
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, redirect, render_template, request, url_for

app = Flask(__name__)

DOWNLOAD_DIR = Path(
    os.environ.get("DOWNLOAD_DIR", "/mnt/nas/downloads")
).resolve()

TEMP_DOWNLOAD_DIR = Path(
    os.environ.get(
        "TEMP_DOWNLOAD_DIR",
        "/var/tmp/ytdlp-web",
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


def build_command(job: dict) -> list[str]:
    command = [
        YTDLP_BIN,
        "--newline",
        "--progress",
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
        f"temp:{TEMP_DOWNLOAD_DIR}",
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

                job["status"] = "running"
                job["worker_number"] = worker_number
                job["started_at"] = utc_now()

            DOWNLOAD_DIR.mkdir(
                parents=True,
                exist_ok=True,
            )
            TEMP_DOWNLOAD_DIR.mkdir(
                parents=True,
                exist_ok=True,
            )

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
                    current_job["finished_at"] = utc_now()
                    current_job["process_id"] = None

        finally:
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


start_workers()


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
        jobs=recent_jobs,
        download_dir=str(DOWNLOAD_DIR),
        max_concurrent_downloads=MAX_CONCURRENT_DOWNLOADS,
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
                jobs=recent_jobs,
                download_dir=str(DOWNLOAD_DIR),
                max_concurrent_downloads=MAX_CONCURRENT_DOWNLOADS,
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
        "log": [],
    }

    with jobs_lock:
        jobs[job_id] = job

    job_queue.put(job_id)

    return redirect(
        url_for(
            "job_page",
            job_id=job_id,
        )
    )


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
            "max_concurrent_downloads":
                MAX_CONCURRENT_DOWNLOADS,
            "jobs": result,
        }
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=100,
        threaded=True,
    )
