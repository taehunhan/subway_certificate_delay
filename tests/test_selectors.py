from __future__ import annotations

import unittest
from datetime import date

from subway_delay.capture import SelectOption, metro9_tab_selector, option_value_for_date


class SelectorHelperTests(unittest.TestCase):
    def test_option_value_for_date_uses_matching_text(self) -> None:
        options = [
            SelectOption(value="0", text="금일 (2026-05-13)"),
            SelectOption(value="1", text="1일전 (2026-05-12)"),
            SelectOption(value="2", text="2일전 (2026-05-11)"),
        ]

        self.assertEqual(option_value_for_date(options, date(2026, 5, 12)), "1")

    def test_option_value_for_date_raises_when_missing(self) -> None:
        options = [SelectOption(value="0", text="금일 (2026-05-13)")]

        with self.assertRaises(ValueError):
            option_value_for_date(options, date(2026, 5, 12))

    def test_metro9_selector_uses_data_tab_date(self) -> None:
        self.assertEqual(
            metro9_tab_selector(date(2026, 5, 12)),
            'li.button_tab[data-tab="2026-05-12"]',
        )


if __name__ == "__main__":
    unittest.main()
