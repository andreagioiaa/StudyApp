"""Test della funzione "Rimuovi testo" del modulo PDF e Slide."""

from __future__ import annotations

import unittest

from study_manager.services.pdf_tools.document import parse_markdown
from study_manager.services.pdf_tools.text_removal import (
    RemovalPatternError,
    RemovalRule,
    apply_rules,
    count_matches,
    remove_text,
    rules_from_json,
    rules_to_json,
)

SAMPLE = """=== SLIDE 1 ===
# Introduzione
Università di Udine - Ingegneria del Software
Il ciclo di vita del software, Università di Udine.

=== SLIDE 2 ===
## Requisiti
- Università di Udine
- Requisiti funzionali
UNIVERSITÀ DI
UDINE - Corso 2026
"""


class TextRemovalTests(unittest.TestCase):
    def test_removes_all_occurrences_case_insensitive_and_across_lines(self):
        rule = RemovalRule("Università di Udine")
        self.assertEqual(count_matches(SAMPLE, rule), 4)
        result, count = remove_text(SAMPLE, rule)
        self.assertEqual(count, 4)
        self.assertNotIn("Udine", result)
        self.assertNotIn("UDINE", result)

    def test_tidy_cleans_spaces_punctuation_and_empty_bullets(self):
        result, _ = remove_text(SAMPLE, RemovalRule("Università di Udine"))
        self.assertIn("\nIngegneria del Software\n", result)
        self.assertIn("Il ciclo di vita del software.\n", result)
        self.assertNotIn("\n- \n", result)
        self.assertNotIn("\n-\n", result)
        self.assertIn("- Requisiti funzionali", result)
        self.assertIn("\nCorso 2026", result)
        self.assertEqual(len(parse_markdown(result)), 2)

    def test_slide_markers_are_never_removed(self):
        result, count = remove_text(SAMPLE, RemovalRule("SLIDE"))
        self.assertEqual(count, 0)
        self.assertEqual(result, SAMPLE)
        self.assertEqual(len(parse_markdown(result)), 2)

    def test_match_case_and_whole_word(self):
        text = "Il software e i software-house: SOFTWARE e softwares."
        self.assertEqual(count_matches(text, RemovalRule("software", match_case=True)), 3)
        self.assertEqual(count_matches(text, RemovalRule("software", whole_word=True)), 3)
        self.assertEqual(count_matches(text, RemovalRule("software", match_case=True, whole_word=True)), 2)

    def test_regex(self):
        text = "Pag. 3\nTesto utile\nPag. 14\n"
        result, count = remove_text(text, RemovalRule(r"^Pag\. \d+$", regex=True))
        self.assertEqual(count, 2)
        self.assertEqual(result.strip(), "Testo utile")

    def test_invalid_regex_and_empty_pattern(self):
        with self.assertRaises(RemovalPatternError):
            count_matches("abc", RemovalRule("(", regex=True))
        with self.assertRaises(RemovalPatternError):
            count_matches("abc", RemovalRule("   "))
        self.assertEqual(remove_text("abc", RemovalRule("x*", regex=True)), ("abc", 0))

    def test_without_tidy_is_plain_replace_all(self):
        text = "a ©2026 b\n©2026\nc"
        result, count = remove_text(text, RemovalRule("©2026", tidy=False))
        self.assertEqual(count, 2)
        self.assertEqual(result, "a  b\n\nc")

    def test_preserves_indentation_of_nested_bullets(self):
        text = "- Punto\n  - [Prof. Rossi] Sotto punto\n"
        result, _ = remove_text(text, RemovalRule("[Prof. Rossi]"))
        self.assertEqual(result, "- Punto\n  - Sotto punto\n")

    def test_separators_around_removed_text(self):
        cases = {
            "Titolo - Prof. Rossi": "Titolo",
            "Prof. Rossi | Lezione 3": "Lezione 3",
            "Vedi Prof. Rossi, poi esercizi": "Vedi, poi esercizi",
            "## Prof. Rossi: Lezione": "## Lezione",
            "- Prof. Rossi - nota": "- nota",
        }
        for source, expected in cases.items():
            self.assertEqual(remove_text(source, RemovalRule("Prof. Rossi"))[0], expected, source)

    def test_strict_spaces(self):
        text = "Corso di\nReti"
        self.assertEqual(count_matches(text, RemovalRule("Corso di Reti", flexible_spaces=False)), 0)
        self.assertEqual(count_matches(text, RemovalRule("Corso di Reti")), 1)

    def test_rules_roundtrip_and_apply(self):
        rules = [RemovalRule("Università di Udine"), RemovalRule(r"Corso \d+", regex=True, match_case=True)]
        restored = rules_from_json(rules_to_json(rules))
        self.assertEqual([rule.to_dict() for rule in restored], [rule.to_dict() for rule in rules])
        result, total = apply_rules(SAMPLE, restored + [RemovalRule("(", regex=True)])
        self.assertEqual(total, 5)
        self.assertNotIn("Corso 2026", result)
        self.assertEqual(rules_from_json("non json"), [])
        self.assertEqual(rules_from_json(""), [])


if __name__ == "__main__":
    unittest.main()
