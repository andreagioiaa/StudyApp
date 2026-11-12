"""Test del salvataggio temporaneo (bozza) del modulo PDF e Slide."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from study_manager.services.pdf_tools.draft import delete_draft, load_draft, save_draft


DOC = {
    "path": "C:/slide/lezione1.pdf",
    "title": "Lezione 1",
    "source": "=== SLIDE 1 ===\n# Titolo\nTesto modificato $x^2$ àèì",
    "fmt": "markdown",
    "warnings": ["OCR su 2 pagine"],
    "engines": {"pymupdf": 3},
    "error": "",
    "latex": {"numbered": True, "embed": True, "base_level": "section", "root_title": "Lezione 1"},
}


class DraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "sub" / "pdf_draft.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_roundtrip(self) -> None:
        second = dict(DOC, path="C:/slide/lezione2.pdf", fmt="latex", source="\\section{A}")
        saved_at = save_draft(self.path, [DOC, second], current=1, sources=["C:/slide/lezione1.pdf"])
        data = load_draft(self.path)
        self.assertIsNotNone(data)
        self.assertEqual(data["current"], 1)
        self.assertEqual(data["docs"][0], DOC)
        self.assertEqual(data["docs"][1]["fmt"], "latex")
        self.assertEqual(data["sources"], ["C:/slide/lezione1.pdf"])
        self.assertAlmostEqual(data["saved_at"], saved_at)
        self.assertFalse(self.path.with_name("pdf_draft.json.tmp").exists())

    def test_missing_or_corrupt(self) -> None:
        self.assertIsNone(load_draft(self.path))
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{non json", encoding="utf-8")
        self.assertIsNone(load_draft(self.path))
        self.path.write_text(json.dumps({"docs": []}), encoding="utf-8")
        self.assertIsNone(load_draft(self.path))

    def test_invalid_current_falls_back(self) -> None:
        save_draft(self.path, [DOC], current=7)
        self.assertEqual(load_draft(self.path)["current"], 0)

    def test_delete(self) -> None:
        save_draft(self.path, [DOC])
        self.assertTrue(delete_draft(self.path))
        self.assertFalse(self.path.exists())
        self.assertIsNone(load_draft(self.path))
        self.assertFalse(delete_draft(self.path))


if __name__ == "__main__":
    unittest.main()
