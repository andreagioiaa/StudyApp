"""Test delle materie, dei CFU e del countdown per gli esami."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from study_manager.database import Database
from study_manager.services.subjects import (
    cfu_to_hours,
    exam_countdown,
    format_cfu,
    format_exam_date,
    parse_exam_date,
)


NOW = datetime(2026, 5, 20, 15, 30, 0)


class SubjectHelperTests(unittest.TestCase):
    def test_cfu_conversion_and_labels(self) -> None:
        self.assertEqual(cfu_to_hours(6), 150)
        self.assertEqual(cfu_to_hours(0), 0)
        self.assertEqual(cfu_to_hours("9"), 225)
        self.assertEqual(format_cfu(6), "6 CFU")
        self.assertEqual(format_cfu(0), "CFU non indicati")

    def test_exam_date_formats(self) -> None:
        self.assertEqual(parse_exam_date("12/06/2026"), "2026-06-12")
        self.assertEqual(parse_exam_date("12-06-2026"), "2026-06-12")
        self.assertEqual(parse_exam_date("12.06.2026"), "2026-06-12")
        self.assertEqual(parse_exam_date("2026-06-12"), "2026-06-12")
        self.assertEqual(parse_exam_date(""), "")
        self.assertEqual(format_exam_date("2026-06-12"), "12/06/2026")
        with self.assertRaises(ValueError):
            parse_exam_date("31/02/2026")

    def test_exam_countdown_states(self) -> None:
        upcoming = exam_countdown("2026-05-30", NOW)
        self.assertEqual(upcoming["status"], "upcoming")
        self.assertEqual(upcoming["days"], 10)
        self.assertEqual(upcoming["label"], "Mancano 10 giorni")
        self.assertEqual(upcoming["short_label"], "Tra 10 g")
        self.assertEqual(upcoming["date_label"], "30/05/2026")

        tomorrow = exam_countdown("2026-05-21", NOW)
        self.assertTrue(tomorrow["label"].startswith("Domani"))
        self.assertEqual(tomorrow["short_label"], "Domani")

        today = exam_countdown("2026-05-20", NOW)
        self.assertEqual(today["status"], "today")
        self.assertEqual(today["label"], "Esame oggi")

        past = exam_countdown("2026-05-18", NOW)
        self.assertEqual(past["status"], "past")
        self.assertIn("2", past["label"])

        undefined = exam_countdown("", NOW)
        self.assertFalse(undefined["defined"])
        self.assertEqual(undefined["status"], "undefined")


class SubjectDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.db")

    def tearDown(self) -> None:
        self.db.close_all()
        self.temp.cleanup()

    def test_save_update_and_tags(self) -> None:
        subject = self.db.save_subject(
            "Analisi Matematica",
            "Mario Rossi",
            "mario.rossi@uni.it",
            9,
            "15/12/2026",
            "Portare il formulario.",
        )
        self.assertGreater(int(subject["subject_id"]), 0)
        self.assertEqual(subject["cfu"], 9)
        self.assertEqual(subject["hours"], 225)
        self.assertEqual(subject["exam_date"], "2026-12-15")
        self.assertEqual(subject["teacher_name"], "Mario Rossi")
        self.assertEqual(subject["teacher_email"], "mario.rossi@uni.it")
        self.assertEqual(subject["notes"], "Portare il formulario.")
        self.assertEqual(self.db.list_subject_tags(), ["Analisi Matematica"])

        updated = self.db.save_subject(
            "Analisi Matematica",
            "Luisa Bianchi",
            "",
            6,
            "",
            "",
            original_name="Analisi Matematica",
        )
        self.assertEqual(updated["cfu"], 6)
        self.assertEqual(updated["hours"], 150)
        self.assertEqual(updated["teacher_name"], "Luisa Bianchi")
        self.assertEqual(updated["teacher_email"], "")
        self.assertEqual(updated["exam_date"], "")

    def test_derived_subject_and_statistics(self) -> None:
        self.db.add_material("Dispense", "", "Fisica", 10)
        self.db.add_flashcard("Fisica", "Domanda", "Risposta")
        self.db.add_video_lesson("Lezione", "C:/video/lezione.mp4", "Fisica")
        self.db.import_calendar_events(
            [
                {
                    "uid": "evento-1",
                    "title": "Esercitazione",
                    "subject": "Fisica",
                    "start": "2026-05-21 10:00:00",
                    "end": "2026-05-21 12:00:00",
                }
            ]
        )
        session_id = self.db.create_session("Fisica")
        self.db.finalize_session(session_id, 60, True)

        subject = self.db.get_subject("fisica")
        self.assertIsNotNone(subject)
        assert subject is not None
        self.assertEqual(subject["subject_id"], 0)
        self.assertEqual(subject["materials"], 1)
        self.assertEqual(subject["flashcards"], 1)
        self.assertEqual(subject["due_flashcards"], 1)
        self.assertEqual(subject["videos"], 1)
        self.assertEqual(subject["events"], 1)
        self.assertEqual(subject["sessions"], 1)
        self.assertEqual(subject["study_minutes"], 60)

        saved = self.db.save_subject("Fisica", "Docente", "", 6, "")
        self.assertGreater(int(saved["subject_id"]), 0)
        self.assertEqual(saved["materials"], 1)
        self.assertEqual(self.db.list_subject_tags(), ["Fisica"])

    def test_rename_propagates_to_linked_data(self) -> None:
        self.db.add_material("Dispense", "", "Fisica", 10)
        self.db.add_flashcard("Fisica", "Domanda", "Risposta")
        self.db.add_video_lesson("Lezione", "C:/video/lezione.mp4", "Fisica")
        self.db.create_session("Fisica")
        self.db.import_calendar_events(
            [
                {
                    "uid": "evento-1",
                    "title": "Esercitazione",
                    "subject": "Fisica",
                    "start": "2026-05-21 10:00:00",
                    "end": "2026-05-21 12:00:00",
                }
            ]
        )
        self.db.save_subject("Fisica", "Docente", "", 6, "")

        renamed = self.db.save_subject(
            "Fisica Generale",
            "Docente",
            "",
            6,
            "",
            "",
            original_name="Fisica",
        )
        self.assertEqual(renamed["name"], "Fisica Generale")
        self.assertEqual(self.db.list_materials()[0]["subject"], "Fisica Generale")
        self.assertEqual(self.db.list_flashcards()[0]["subject"], "Fisica Generale")
        self.assertEqual(self.db.list_video_lessons()[0]["subject"], "Fisica Generale")
        self.assertEqual(self.db.list_recent_sessions()[0]["subject"], "Fisica Generale")
        events = self.db.get_events("2026-01-01 00:00:00", "2027-01-01 00:00:00")
        self.assertEqual(events[0]["subject"], "Fisica Generale")
        self.assertEqual(self.db.list_subject_tags(), ["Fisica Generale"])

    def test_case_variants_are_merged_and_rename_normalizes_links(self) -> None:
        self.db.add_flashcard("fisica", "Domanda", "Risposta")
        self.db.add_material("Dispense", "", "Fisica", 10)
        subjects = self.db.list_subjects()
        self.assertEqual(len(subjects), 1)
        self.assertEqual(subjects[0]["flashcards"], 1)
        self.assertEqual(subjects[0]["materials"], 1)
        self.assertEqual(len(self.db.list_subject_tags()), 1)

        saved = self.db.save_subject("Fisica", original_name="fisica")
        self.assertEqual(saved["name"], "Fisica")
        self.assertEqual(self.db.list_flashcards()[0]["subject"], "Fisica")
        self.assertEqual(self.db.list_materials()[0]["subject"], "Fisica")
        self.assertEqual(self.db.list_subject_tags(), ["Fisica"])

    def test_materials_are_scoped_to_subject(self) -> None:
        self.db.add_material("Analisi 1", "", "Analisi Matematica", 8)
        self.db.add_material("Analisi 2", "", "analisi matematica", 4)
        self.db.add_material("Fisica", "", "Fisica", 6)
        self.db.add_material("Senza materia", "", "", 2)

        analisi = self.db.list_materials_for_subject("Analisi Matematica")
        self.assertEqual([item["title"] for item in analisi], ["Analisi 1", "Analisi 2"])
        fisica = self.db.list_materials_for_subject("Fisica")
        self.assertEqual(len(fisica), 1)
        self.assertEqual(self.db.list_materials_for_subject("Chimica"), [])
        orphans = self.db.list_materials_without_subject()
        self.assertEqual([item["title"] for item in orphans], ["Senza materia"])
        self.assertEqual(self.db.count_materials_without_subject(), 1)
        self.assertEqual(len(self.db.list_materials()), 4)

    def test_duplicate_rename_and_delete_guard(self) -> None:
        self.db.save_subject("Storia")
        self.db.save_subject("Filosofia")
        with self.assertRaises(ValueError):
            self.db.save_subject("Storia", original_name="Filosofia")

        self.db.save_subject("Metodologia")
        self.db.delete_subject("Metodologia")
        self.assertIsNone(self.db.get_subject("Metodologia"))

        self.db.add_flashcard("Storia", "Domanda", "Risposta")
        with self.assertRaises(ValueError):
            self.db.delete_subject("Storia")


if __name__ == "__main__":
    unittest.main()
