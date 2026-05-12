from __future__ import annotations

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from subway_delay.dates import parse_manual_date, resolve_capture_date


class ResolveCaptureDateTests(unittest.TestCase):
    def test_tuesday_uses_previous_day(self) -> None:
        now = datetime(2026, 5, 12, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        self.assertEqual(
            resolve_capture_date("Asia/Seoul", now=now),
            date(2026, 5, 11),
        )

    def test_friday_uses_previous_day(self) -> None:
        now = datetime(2026, 5, 15, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        self.assertEqual(
            resolve_capture_date("Asia/Seoul", now=now),
            date(2026, 5, 14),
        )

    def test_monday_uses_previous_friday(self) -> None:
        now = datetime(2026, 5, 11, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        self.assertEqual(
            resolve_capture_date("Asia/Seoul", now=now),
            date(2026, 5, 8),
        )

    def test_year_boundary_is_handled(self) -> None:
        now = datetime(2027, 1, 4, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        self.assertEqual(
            resolve_capture_date("Asia/Seoul", now=now),
            date(2027, 1, 1),
        )

    def test_manual_date_override_is_returned(self) -> None:
        explicit = date(2026, 5, 1)
        self.assertEqual(
            resolve_capture_date("Asia/Seoul", explicit_date=explicit),
            explicit,
        )


class ParseManualDateTests(unittest.TestCase):
    def test_none_returns_none(self) -> None:
        self.assertIsNone(parse_manual_date(None))

    def test_valid_date_parses(self) -> None:
        self.assertEqual(parse_manual_date("2026-05-12"), date(2026, 5, 12))

    def test_invalid_date_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_manual_date("20260512")


if __name__ == "__main__":
    unittest.main()
