"""Test dei servizi audio, dei nomi file e dei suoni generati."""

from __future__ import annotations

import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from study_manager import paths
from study_manager.services.audio_service import (
    AudioController,
    generate_focus_sound,
    generate_focus_sounds,
)
from study_manager.services.youtube_audio import (
    copy_title,
    find_existing_track,
    is_youtube_url,
    sanitize_filename,
)


class YouTubeNameTests(unittest.TestCase):
    def test_youtube_url_validation(self) -> None:
        valid = (
            "https://www.youtube.com/watch?v=abc123",
            "https://youtu.be/abc123",
            "https://youtube.com/shorts/abc123",
            "https://music.youtube.com/watch?v=abc123",
        )
        invalid = (
            "",
            "https://example.com/watch?v=abc123",
            "file:///C:/video.mp4",
            "https://youtu.be/",
            "youtube.com/watch?v=abc123",
        )
        for url in valid:
            self.assertTrue(is_youtube_url(url), url)
        for url in invalid:
            self.assertFalse(is_youtube_url(url), url)

    def test_filename_is_windows_safe(self) -> None:
        self.assertEqual(
            sanitize_filename('  Video: "prova" / test?  '),
            "Video prova test",
        )
        self.assertEqual(sanitize_filename("CON"), "_CON")
        self.assertLessEqual(len(sanitize_filename("x" * 300)), 120)

    def test_duplicate_and_copy_names(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / "Lezione.mp3"
            first.write_bytes(b"audio")
            self.assertEqual(find_existing_track(root, "Lezione"), first)
            self.assertEqual(copy_title(root, "Lezione"), "Lezione (copia 2)")
            (root / "Lezione (copia 2).ogg").write_bytes(b"audio")
            self.assertEqual(copy_title(root, "Lezione"), "Lezione (copia 3)")

    def test_find_existing_ignores_other_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertIsNone(find_existing_track(root, "Assente"))


class AudioPathTests(unittest.TestCase):
    def test_source_sounds_folder_is_beside_app(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app_root = root / "app"
            app_root.mkdir()
            data = root / "data"
            with patch.dict(
                os.environ,
                {"STUDYFLOW_DATA_DIR": "", "STUDYFLOW_SOUNDS_DIR": ""},
            ), patch.object(paths, "project_root", return_value=app_root):
                self.assertEqual(paths.sounds_dir(data), app_root / "sounds")

    def test_frozen_sounds_folder_is_beside_exe(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            exe = root / "StudyFlow.exe"
            exe.write_bytes(b"exe")
            data = root / "data"
            with patch.dict(
                os.environ,
                {"STUDYFLOW_DATA_DIR": "", "STUDYFLOW_SOUNDS_DIR": ""},
            ), patch.object(paths, "is_frozen", return_value=True), patch.object(
                paths, "executable_path", return_value=exe
            ):
                self.assertEqual(paths.sounds_dir(data), root / "sounds")

    def test_unwritable_app_folder_falls_back_to_data(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / "data"
            with patch.dict(
                os.environ,
                {"STUDYFLOW_DATA_DIR": "", "STUDYFLOW_SOUNDS_DIR": ""},
            ), patch.object(paths, "_is_writable", return_value=False):
                self.assertEqual(paths.sounds_dir(data), data / "sounds")


class AudioServiceTests(unittest.TestCase):
    def test_focus_sound_generation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "Rumore bianco.wav"
            generate_focus_sound(path, "white", duration=1.0, sample_rate=8000)
            self.assertTrue(path.is_file())
            with wave.open(str(path), "rb") as source:
                self.assertEqual(source.getnchannels(), 1)
                self.assertEqual(source.getsampwidth(), 2)
                self.assertEqual(source.getframerate(), 8000)
                self.assertGreater(source.getnframes(), 0)
            created = generate_focus_sounds(root, duration=0.2, sample_rate=8000)
            self.assertEqual(len(created), 3)
            self.assertEqual(len([p for p in root.glob("*.wav")]), 4)

    def test_playback_controls_with_dummy_driver(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "test.wav"
            generate_focus_sound(path, "white", duration=1.0, sample_rate=8000)
            with patch.dict(
                os.environ,
                {
                    "SDL_AUDIODRIVER": "dummy",
                    "PYGAME_HIDE_SUPPORT_PROMPT": "1",
                },
            ):
                controller = AudioController(root)
                try:
                    self.assertEqual([track["name"] for track in controller.list_tracks()], ["test.wav"])
                    controller.set_volume(0.25)
                    ok, error = controller.play("test.wav", loop=True)
                    self.assertTrue(ok, error)
                    self.assertEqual(controller.status()["current"], "test.wav")
                    self.assertTrue(controller.pause())
                    self.assertTrue(controller.status()["paused"])
                    self.assertTrue(controller.resume())
                    controller.stop()
                    self.assertFalse(controller.status()["current"])
                finally:
                    controller.shutdown()


if __name__ == "__main__":
    unittest.main()
