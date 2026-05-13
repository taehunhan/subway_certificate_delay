from __future__ import annotations

import unittest
from datetime import date

from subway_delay.capture import (
    SelectOption,
    gtx_cell_updates,
    metro9_tab_selector,
    option_value_for_date,
)


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

    def test_ui_line_panel_id_uses_matching_date_text(self) -> None:
        panels = [
            SelectOption(value="tab-1", text="2026-05-13"),
            SelectOption(value="tab-2", text="2026-05-12"),
            SelectOption(value="tab-3", text="2026-05-11"),
        ]

        self.assertEqual(option_value_for_date(panels, date(2026, 5, 12)), "tab-2")

    def test_gtx_cell_updates_maps_up_and_down_cells(self) -> None:
        updates = gtx_cell_updates(
            [
                {
                    "updwtDvsnNm": "상행",
                    "timeDvsn1": "5분 지연",
                    "timeDvsn2": "",
                    "timeDvsn3": None,
                },
                {
                    "updwtDvsnNm": "하행",
                    "timeDvsn1": None,
                    "timeDvsn2": "10분 지연",
                    "timeDvsn3": "15분 지연",
                },
            ]
        )

        self.assertEqual(
            updates,
            {
                "up-time1": "5분 지연",
                "up-time2": "-",
                "up-time3": "-",
                "down-time1": "-",
                "down-time2": "10분 지연",
                "down-time3": "15분 지연",
            },
        )


if __name__ == "__main__":
    unittest.main()
