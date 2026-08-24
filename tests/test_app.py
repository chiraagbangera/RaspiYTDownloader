import importlib
import os
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import patch


TEST_AREA = tempfile.TemporaryDirectory()
TEST_ROOT = Path(TEST_AREA.name)
os.environ["DOWNLOAD_DIR"] = str(TEST_ROOT / "nas" / "Youtube Videos")
os.environ["TEMP_DOWNLOAD_DIR"] = str(TEST_ROOT / "pi-temp")
os.environ["NAS_TEMP_DOWNLOAD_DIR"] = str(
    TEST_ROOT / "nas" / "Youtube Videos" / ".ytdlp-temp"
)
os.environ["YTDLP_BIN"] = str(TEST_ROOT / "yt-dlp")
os.environ["MAX_CONCURRENT_DOWNLOADS"] = "1"
os.environ["LOCAL_FREE_RESERVE_BYTES"] = "100"
os.environ["LOCAL_SPACE_MULTIPLIER"] = "2"

download_app = importlib.import_module("app")


def tearDownModule():
    TEST_AREA.cleanup()


class StorageSelectionTests(unittest.TestCase):
    def tearDown(self):
        download_app.local_space_reservations.clear()

    def test_startup_creates_youtube_videos_folder(self):
        self.assertTrue(download_app.DOWNLOAD_DIR.is_dir())
        self.assertEqual(download_app.DOWNLOAD_DIR.name, "Youtube Videos")

    def test_selected_video_and_audio_sizes_are_added(self):
        info = {
            "requested_downloads": [
                {"filesize": 800},
                {"filesize_approx": 200},
            ]
        }
        self.assertEqual(download_app.media_size_from_info(info), 1000)

    def test_playlist_size_requires_an_estimate_for_every_entry(self):
        complete = {
            "entries": [
                {"filesize": 400},
                {"filesize_approx": 600},
            ]
        }
        incomplete = {
            "entries": [
                {"filesize": 400},
                {"title": "Unknown size"},
            ]
        }
        self.assertEqual(download_app.media_size_from_info(complete), 1000)
        self.assertIsNone(download_app.media_size_from_info(incomplete))

    def test_concurrent_reservations_cannot_claim_the_same_pi_space(self):
        with patch.object(
            download_app.shutil,
            "disk_usage",
            return_value=SimpleNamespace(free=1000),
        ):
            self.assertTrue(download_app.reserve_local_space("first", 300))
            self.assertFalse(download_app.reserve_local_space("second", 200))
        self.assertEqual(download_app.local_space_reservations, {"first": 600})

    def test_unknown_size_uses_nas_instead_of_pi(self):
        with patch.object(download_app.shutil, "disk_usage") as disk_usage:
            directory, mode = download_app.select_working_directory(
                "unknown",
                None,
            )
        self.assertEqual(directory, download_app.NAS_TEMP_DOWNLOAD_DIR)
        self.assertEqual(mode, "nas_direct")
        disk_usage.assert_not_called()

    def test_fitting_download_uses_pi_workspace(self):
        with patch.object(
            download_app.shutil,
            "disk_usage",
            return_value=SimpleNamespace(free=1000),
        ):
            directory, mode = download_app.select_working_directory(
                "small",
                300,
            )
        self.assertEqual(directory, download_app.TEMP_DOWNLOAD_DIR)
        self.assertEqual(mode, "pi_local")

    def test_direct_nas_command_uses_hidden_nas_workspace(self):
        job = {
            "url": "https://www.youtube.com/watch?v=example",
            "playlist": False,
            "working_directory": str(download_app.NAS_TEMP_DOWNLOAD_DIR),
        }
        command = download_app.build_command(job)
        self.assertIn(f"temp:{download_app.NAS_TEMP_DOWNLOAD_DIR}", command)
        self.assertIn(str(download_app.DOWNLOAD_DIR), command)
        self.assertIn("--no-playlist", command)


class WebUiTests(unittest.TestCase):
    def test_index_uses_download_station_layout_and_destination(self):
        response = download_app.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Let the Pi handle it", page)
        self.assertIn("Direct to NAS", page)
        self.assertIn(str(download_app.DOWNLOAD_DIR), page)

    def test_health_check_executes_ytdlp(self):
        Path(download_app.YTDLP_BIN).touch()
        completed = CompletedProcess(
            [download_app.YTDLP_BIN, "--version"],
            0,
            stdout="2026.08.24\n",
            stderr="",
        )
        with patch.object(
            download_app.subprocess,
            "run",
            return_value=completed,
        ) as run:
            ok, version, error = download_app.check_ytdlp_health()
        self.assertTrue(ok)
        self.assertEqual(version, "2026.08.24")
        self.assertIsNone(error)
        run.assert_called_once()

    def test_health_check_reports_broken_executable(self):
        Path(download_app.YTDLP_BIN).touch()
        completed = CompletedProcess(
            [download_app.YTDLP_BIN, "--version"],
            255,
            stdout="",
            stderr="Could not create temporary directory!\n",
        )
        with patch.object(
            download_app.subprocess,
            "run",
            return_value=completed,
        ):
            ok, version, error = download_app.check_ytdlp_health()
        self.assertFalse(ok)
        self.assertIsNone(version)
        self.assertEqual(error, "Could not create temporary directory!")


if __name__ == "__main__":
    unittest.main()
