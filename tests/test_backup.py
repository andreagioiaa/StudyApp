"""Test di esportazione e importazione dei dati."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from study_manager.database import Database
from study_manager.services.backup import (
    BackupError,
    export_backup,
    import_backup,
    inspect_backup,
)


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.transcripts = self.root / "transcripts"
        self.transcripts.mkdir()
        self.db = Database(self.root / "study_manager.db")
        self.paths = {"data": self.root, "transcripts": self.transcripts}

    def tearDown(self) -> None:
        self.db.close_all()
        self.temp.cleanup()

    def _populate(self) -> None:
        self.db.save_subject(
            "Biologia",
            "Mario Rossi",
            "mario.rossi@uni.it",
            9,
            "2026-12-15",
            "Esame orale.",
        )
        self.db.add_material("Dispense", "C:/tmp/dispense.pdf", "Biologia", 8)
        card_id = self.db.add_flashcard(
            "Biologia",
            "Che cos'e' la cellula?",
            "L'unita' fondamentale dei viventi.",
            "Nota di prova.",
        )
        self.db.review_flashcard(card_id, 3)
        transcript = self.transcripts / "lezione.txt"
        transcript.write_text("[00:00] Testo della lezione.\n", encoding="utf-8")
        video_id = self.db.add_video_lesson(
            "Lezione 1", "C:/video/lezione.mp4", "Biologia", "lezione.txt"
        )
        self.db.complete_video_lesson(
            video_id, str(transcript), "lezione.txt", 60, "base-q5_1", "it"
        )

    def test_export_inspect_and_replace_import(self) -> None:
        self._populate()
        target = self.root / "backup.zip"
        result = export_backup(self.db, self.paths, target)
        self.assertTrue(target.is_file())
        self.assertEqual(result["counts"]["flashcards"], 1)
        self.assertEqual(result["counts"]["subjects"], 1)
        self.assertEqual(result["transcripts"], 1)
        with zipfile.ZipFile(target) as archive:
            self.assertIn("manifest.json", archive.namelist())
            self.assertIn("data.json", archive.namelist())
            self.assertIn("transcripts/lezione.txt", archive.namelist())

        manifest, payload = inspect_backup(target)
        self.assertEqual(manifest["format"], "studyflow-backup")
        self.assertEqual(payload["tables"]["flashcards"][0]["front"], "Che cos'e' la cellula?")

        # Modifica i dati attuali: l'importazione deve riportarli al backup.
        self.db.add_flashcard("Storia", "Roma", "Capitale")
        self.db.delete_material(1)
        self.transcripts.joinpath("lezione.txt").unlink()

        restored = import_backup(self.db, self.paths, target)
        self.assertTrue(Path(restored["safety_backup"]).is_file())
        self.assertEqual(len(self.db.list_flashcards()), 1)
        self.assertEqual(len(self.db.list_materials()), 1)
        self.assertEqual(self.db.list_flashcards()[0]["subject"], "Biologia")
        restored_subject = self.db.get_subject("Biologia")
        self.assertIsNotNone(restored_subject)
        assert restored_subject is not None
        self.assertEqual(restored_subject["cfu"], 9)
        self.assertEqual(restored_subject["exam_date"], "2026-12-15")
        self.assertEqual(restored_subject["teacher_name"], "Mario Rossi")
        self.assertTrue((self.transcripts / "lezione.txt").is_file())
        self.assertEqual(self.db.flashcard_stats()["reviewed_today"], 1)

    def test_invalid_backup_is_rejected(self) -> None:
        bad = self.root / "bad.zip"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("manifest.json", "{}")
            archive.writestr("data.json", "{}")
        with self.assertRaises(BackupError):
            inspect_backup(bad)
        with self.assertRaises(BackupError):
            inspect_backup(self.root / "missing.zip")

        future = self.root / "future.zip"
        with zipfile.ZipFile(future, "w") as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "format": "studyflow-backup",
                        "backup_version": 1,
                        "schema_version": 999,
                    }
                ),
            )
            archive.writestr("data.json", json.dumps({"tables": {}}))
        with self.assertRaises(BackupError):
            inspect_backup(future)

    def test_export_adds_zip_extension(self) -> None:
        target = self.root / "senza-estensione"
        result = export_backup(self.db, self.paths, target)
        self.assertTrue(result["path"].endswith(".zip"))
        self.assertTrue(Path(result["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
