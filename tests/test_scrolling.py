"""Test della conversione del delta rotella in pixel di scorrimento."""

from __future__ import annotations

import unittest

from study_manager.ui.scrolling import WHEEL_STEP_PIXELS, wheel_scroll_units


class WheelScrollTests(unittest.TestCase):
    def test_standard_notch_moves_three_lines(self) -> None:
        self.assertEqual(WHEEL_STEP_PIXELS, 60)
        self.assertEqual(wheel_scroll_units(120), -60)
        self.assertEqual(wheel_scroll_units(-120), 60)

    def test_multiple_notches(self) -> None:
        self.assertEqual(wheel_scroll_units(240), -120)
        self.assertEqual(wheel_scroll_units(-360), 180)

    def test_high_resolution_deltas_never_vanish(self) -> None:
        self.assertEqual(wheel_scroll_units(15), -8)
        self.assertEqual(wheel_scroll_units(-15), 8)
        self.assertEqual(wheel_scroll_units(1), -1)
        self.assertEqual(wheel_scroll_units(-1), 1)

    def test_invalid_and_empty_deltas(self) -> None:
        self.assertEqual(wheel_scroll_units(0), 0)
        self.assertEqual(wheel_scroll_units(None), 0)
        self.assertEqual(wheel_scroll_units("abc"), 0)

    def test_custom_step(self) -> None:
        self.assertEqual(wheel_scroll_units(120, 90), -90)
        self.assertEqual(wheel_scroll_units(-60, 90), 45)


if __name__ == "__main__":
    unittest.main()
