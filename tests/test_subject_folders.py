"""Test delle cartelle per materia e del collegamento dei file generati."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from study_manager.database import Database
from study_manager.services.subject_folders import (
    FLASHCARD_EXPORT_NAME,
    KIND_FLASHCARDS,
    KIND_NOTES,
    KIND_TRANSCRIPTS,
    SUBFOLDERS,
    SubjectFolders,
    folder_name,
    move_file_into,
)


class FolderNameTests(unittest.TestCase):
    def test_invalid_characters_and_reserved_names(self) -> None:
        self.assertEqual(folder_name("Analisi 1: limiti/derivate"), "Analisi 1 limiti derivate")
        self.assertEqual(folder_name("  Fisica.  "), "Fisica")
        self.assertEqual(folder_name("CON"), "_CON")
        self.assertEqual(folder_name("   "), "")


class SubjectFoldersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Materie"
        self.folders = SubjectFolders(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_ensure_creates_subfolders(self) -> None:
        folder = self.folders.ensure("Ingegneria del Software")
        assert folder is not None
        for name in SUBFOLDERS.values():
            self.assertTrue((folder / name).is_dir())
        self.assertIsNone(self.folders.ensure(""))
        self.assertEqual(self.folders.subfolder("Reti", KIND_NOTES), self.root / "Reti" / "Appunti")

    def test_ensure_all_counts_new_folders(self) -> None:
        self.assertEqual(self.folders.ensure_all(["A", "B", ""]), 2)
        self.assertEqual(self.folders.ensure_all(["A", "B"]), 0)

    def test_rename_moves_files(self) -> None:
        transcripts = self.folders.subfolder("Analisi", KIND_TRANSCRIPTS)
        assert transcripts is not None
        (transcripts / "lezione.txt").write_text("ciao", encoding="utf-8")
        old, new = self.folders.rename("Analisi", "Analisi Matematica")
        self.assertEqual(new, self.root / "Analisi Matematica")
        self.assertFalse(old.exists())
        self.assertTrue((new / "Trascrizioni" / "lezione.txt").is_file())

    def test_rename_merges_into_existing_folder(self) -> None:
        a = self.folders.subfolder("A", KIND_NOTES)
        b = self.folders.subfolder("B", KIND_NOTES)
        (a / "x.txt").write_text("a", encoding="utf-8")
        (b / "x.txt").write_text("b", encoding="utf-8")
        self.folders.rename("A", "B")
        self.assertFalse((self.root / "A").exists())
        names = sorted(path.name for path in (self.root / "B" / "Appunti").iterdir())
        self.assertEqual(names, ["x (2).txt", "x.txt"])

    def test_rename_case_only(self) -> None:
        self.folders.ensure("fisica")
        _old, new = self.folders.rename("fisica", "Fisica")
        self.assertTrue(new.is_dir())
        self.assertEqual([p.name for p in self.root.iterdir()], ["Fisica"])

    def test_remove_if_empty(self) -> None:
        self.folders.ensure("Vuota")
        self.assertTrue(self.folders.remove_if_empty("Vuota"))
        notes = self.folders.subfolder("Piena", KIND_NOTES)
        (notes / "a.tex").write_text("x", encoding="utf-8")
        self.assertFalse(self.folders.remove_if_empty("Piena"))

    def test_move_file_into_avoids_overwrite(self) -> None:
        src_dir = Path(self.tmp.name) / "src"
        src_dir.mkdir()
        (src_dir / "t.txt").write_text("1", encoding="utf-8")
        dest = self.folders.subfolder("Reti", KIND_TRANSCRIPTS)
        (dest / "t.txt").write_text("0", encoding="utf-8")
        moved = move_file_into(src_dir / "t.txt", dest)
        self.assertEqual(moved.name, "t (2).txt")
        self.assertEqual(moved.read_text(encoding="utf-8"), "1")

    def test_flashcard_export(self) -> None:
        cards = [{"id": 2, "front": "Q2", "back": "A2", "notes": ""},
                 {"id": 1, "front": "Q1", "back": "A1", "notes": "n"}]
        path = self.folders.export_flashcards("Reti", cards)
        self.assertEqual(path, self.root / "Reti" / SUBFOLDERS[KIND_FLASHCARDS] / FLASHCARD_EXPORT_NAME)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle, delimiter=";"))
        self.assertEqual(rows, [["Fronte", "Retro", "Note"], ["Q1", "A1", "n"], ["Q2", "A2", ""]])
        self.assertIsNone(self.folders.export_flashcards("Reti", []))
        self.assertFalse(path.exists())


class RelocatePathsTests(unittest.TestCase):
    def test_relocate_and_find_material(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")
            try:
                old = Path(tmp) / "Materie" / "Analisi"
                new = Path(tmp) / "Materie" / "Analisi Matematica"
                material_id = db.add_material("Appunti", str(old / "Appunti" / "cap1.tex"), "Analisi", 0, None)
                other_id = db.add_material("Altro", str(Path(tmp) / "Analisi2" / "x.pdf"), "Analisi", 0, None)
                lesson_id = db.add_video_lesson("L1", str(Path(tmp) / "v.mp4"), "Analisi", "l1.txt", "base", "it")
                db.update_video_transcript_path(lesson_id, str(old / "Trascrizioni" / "l1.txt"), "l1.txt")
                self.assertEqual(db.relocate_paths(old, new), 2)
                self.assertEqual(db.get_material(material_id)["file_path"], str(new / "Appunti" / "cap1.tex"))
                self.assertEqual(db.get_material(other_id)["file_path"], str(Path(tmp) / "Analisi2" / "x.pdf"))
                self.assertEqual(
                    db.get_video_lesson(lesson_id)["transcript_path"], str(new / "Trascrizioni" / "l1.txt")
                )
                found = db.find_material_by_path(str(new / "Appunti" / "cap1.tex"))
                self.assertIsNotNone(found)
                self.assertEqual(int(found["id"]), material_id)
                self.assertIsNone(db.find_material_by_path(""))
            finally:
                db.close_all()


if __name__ == "__main__":
    unittest.main()
