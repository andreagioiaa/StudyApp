"""Test del motore di ripetizione spaziata e delle flashcard."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from study_manager.database import Database
from study_manager.services.spaced_repetition import (
    DEFAULT_EASE_FACTOR,
    RATING_AGAIN,
    RATING_EASY,
    RATING_GOOD,
    RATING_HARD,
    format_interval_days,
    preview_schedule,
    schedule_card,
)


NOW = datetime(2026, 1, 10, 12, 0, 0)


def new_card() -> dict:
    return {
        "state": "new",
        "interval_days": 0,
        "ease_factor": DEFAULT_EASE_FACTOR,
        "repetitions": 0,
        "lapses": 0,
        "learning_step": 0,
        "last_reviewed_at": "",
    }


class SchedulerTests(unittest.TestCase):
    def test_new_card_learning_steps_and_graduation(self) -> None:
        first = schedule_card(new_card(), RATING_GOOD, NOW)
        self.assertEqual(first["state"], "learning")
        self.assertEqual(first["learning_step"], 1)
        self.assertEqual(first["due_at"], "2026-01-10 12:10:00")
        graduated = schedule_card({**new_card(), **first}, RATING_GOOD, NOW)
        self.assertEqual(graduated["state"], "review")
        self.assertEqual(graduated["interval_days"], 1.0)
        self.assertEqual(graduated["repetitions"], 1)
        self.assertEqual(graduated["due_at"], "2026-01-11 12:00:00")

    def test_new_card_easy_graduates_to_four_days(self) -> None:
        result = schedule_card(new_card(), RATING_EASY, NOW)
        self.assertEqual(result["state"], "review")
        self.assertEqual(result["interval_days"], 4.0)
        self.assertEqual(result["due_at"], "2026-01-14 12:00:00")
        self.assertEqual(result["repetitions"], 1)

    def test_review_intervals_follow_ease_factor(self) -> None:
        card = {
            **new_card(),
            "state": "review",
            "interval_days": 10.0,
            "ease_factor": 2.5,
            "repetitions": 3,
            "last_reviewed_at": "2026-01-01 12:00:00",
        }
        good = schedule_card(card, RATING_GOOD, NOW)
        self.assertEqual(good["state"], "review")
        self.assertEqual(good["interval_days"], 25.0)
        self.assertEqual(good["elapsed_seconds"], 9 * 86400)
        hard = schedule_card(card, RATING_HARD, NOW)
        self.assertEqual(hard["interval_days"], 12.0)
        self.assertAlmostEqual(hard["ease_factor"], 2.35)
        easy = schedule_card(card, RATING_EASY, NOW)
        self.assertEqual(easy["interval_days"], 34.0)
        self.assertAlmostEqual(easy["ease_factor"], 2.65)

    def test_again_lapses_and_relearning(self) -> None:
        card = {
            **new_card(),
            "state": "review",
            "interval_days": 20.0,
            "ease_factor": 2.5,
            "repetitions": 5,
            "last_reviewed_at": "2026-01-01 12:00:00",
        }
        again = schedule_card(card, RATING_AGAIN, NOW)
        self.assertEqual(again["state"], "relearning")
        self.assertEqual(again["lapses"], 1)
        self.assertEqual(again["learning_step"], 0)
        self.assertAlmostEqual(again["ease_factor"], 2.3)
        self.assertEqual(again["due_at"], "2026-01-10 12:10:00")
        self.assertEqual(again["interval_days"], 4.0)
        graduated = schedule_card({**card, **again}, RATING_GOOD, NOW)
        self.assertEqual(graduated["state"], "review")
        self.assertEqual(graduated["repetitions"], 6)
        self.assertGreaterEqual(graduated["interval_days"], 1.0)

    def test_ease_factor_is_clamped(self) -> None:
        card = {
            **new_card(),
            "state": "review",
            "interval_days": 10.0,
            "ease_factor": 1.3,
        }
        result = schedule_card(card, RATING_AGAIN, NOW)
        self.assertGreaterEqual(result["ease_factor"], 1.3)
        easy = schedule_card(card, RATING_EASY, NOW)
        self.assertLessEqual(easy["ease_factor"], 5.0)

    def test_preview_and_interval_formatting(self) -> None:
        preview = preview_schedule(new_card(), RATING_AGAIN, NOW)
        self.assertEqual(preview["due_at"], "2026-01-10 12:01:00")
        self.assertEqual(format_interval_days(1 / 1440), "1 min")
        self.assertEqual(format_interval_days(10), "10 g")
        self.assertEqual(format_interval_days(45), "1,5 mesi")
        with self.assertRaises(ValueError):
            schedule_card(new_card(), 9, NOW)


class FlashcardDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.db")

    def tearDown(self) -> None:
        self.db.close_all()
        self.temp.cleanup()

    def test_lifecycle_and_stats(self) -> None:
        card_id = self.db.add_flashcard(
            "Biologia",
            "Che cos'e' la fotosintesi?",
            "Il processo con cui le piante producono energia.",
            "Usa luce, acqua e CO2.",
        )
        card = self.db.get_flashcard(card_id)
        self.assertIsNotNone(card)
        assert card is not None
        self.assertEqual(card["state"], "new")
        stats = self.db.flashcard_stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["new"], 1)
        self.assertEqual(stats["due"], 1)
        self.assertEqual(self.db.list_subject_tags(), ["Biologia"])

        updated, schedule = self.db.review_flashcard(card_id, RATING_GOOD)
        self.assertEqual(updated["state"], "learning")
        self.assertEqual(schedule["state"], "learning")
        self.assertEqual(self.db.flashcard_stats()["reviewed_today"], 1)
        reviews = self.db.list_flashcard_reviews(card_id)
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["rating"], RATING_GOOD)

        self.db.reset_flashcard(card_id)
        reset = self.db.get_flashcard(card_id)
        assert reset is not None
        self.assertEqual(reset["state"], "new")
        self.assertEqual(reset["repetitions"], 0)
        self.assertEqual(self.db.list_flashcard_reviews(card_id), [])

        self.db.delete_flashcard(card_id)
        self.assertIsNone(self.db.get_flashcard(card_id))
        self.assertEqual(self.db.flashcard_stats()["total"], 0)

    def test_filters_search_and_subject_scope(self) -> None:
        first = self.db.add_flashcard("Storia", "Roma", "Capitale d'Italia")
        second = self.db.add_flashcard("Fisica", "Forza", "Massa per accelerazione")
        self.db.add_flashcard("Storia", "Grecia", "Atene")
        self.assertEqual(len(self.db.list_flashcards(subject="Storia")), 2)
        self.assertEqual(len(self.db.list_flashcards(search="accelerazione")), 1)
        self.assertEqual(len(self.db.list_flashcards(state="new")), 3)
        self.db.review_flashcard(second, RATING_EASY)
        self.assertEqual(len(self.db.list_flashcards(state="review")), 1)
        self.assertEqual(self.db.flashcard_stats("Storia")["total"], 2)
        self.assertEqual(len(self.db.due_flashcards()), 2)
        self.assertIsNotNone(self.db.get_flashcard(first))

    def test_update_and_validation(self) -> None:
        card_id = self.db.add_flashcard("Chimica", "H2O", "Acqua")
        self.db.update_flashcard(card_id, "Chimica", "CO2", "Anidride carbonica", "Nota")
        card = self.db.get_flashcard(card_id)
        assert card is not None
        self.assertEqual(card["front"], "CO2")
        self.assertEqual(card["notes"], "Nota")
        with self.assertRaises(ValueError):
            self.db.add_flashcard("Chimica", " ", "Risposta")
        with self.assertRaises(ValueError):
            self.db.update_flashcard(card_id, "Chimica", "Fronte", "")


if __name__ == "__main__":
    unittest.main()
