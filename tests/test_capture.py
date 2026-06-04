from __future__ import annotations

import asyncio
import unittest
from datetime import date
from unittest.mock import AsyncMock

from subway_delay.capture import (
    SEOULMETRO_BOTTOM_MARKERS,
    SEOULMETRO_TABLE_SELECTOR,
    PlaywrightCaptureService,
    initial_navigation_wait_until,
    submit_navigation_wait_until,
)
from subway_delay.config import TargetConfig


def build_target(
    *,
    target_id: str,
    selection_mode: str,
    initial_wait_until: str = "domcontentloaded",
    submit_wait_until: str = "networkidle",
) -> TargetConfig:
    return TargetConfig(
        id=target_id,
        name=target_id,
        url="https://example.com",
        enabled=True,
        selection_mode=selection_mode,
        capture_selector="#main",
        wait_selector="#main",
        submit_selector="button",
        initial_wait_until=initial_wait_until,
        submit_wait_until=submit_wait_until,
    )


class RecordingLocator:
    def __init__(self, page: "RecordingPage", selector: str) -> None:
        self.page = page
        self.selector = selector

    async def evaluate_all(self, _script: str):
        self.page.calls.append(("evaluate_all", self.selector))
        return self.page.options

    async def click(self) -> None:
        self.page.calls.append(("click", self.selector))


class RecordingNavigationContext:
    def __init__(self, page: "RecordingPage", wait_until: str, timeout: int | None) -> None:
        self.page = page
        self.wait_until = wait_until
        self.timeout = timeout

    async def __aenter__(self) -> "RecordingNavigationContext":
        self.page.calls.append(("navigation_enter", self.wait_until, self.timeout))
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.page.calls.append(("navigation_exit", self.wait_until, self.timeout))
        return False


class RecordingPage:
    def __init__(self, options: list[dict[str, str]] | None = None) -> None:
        self.options = options or []
        self.calls: list[tuple] = []

    def locator(self, selector: str) -> RecordingLocator:
        self.calls.append(("locator", selector))
        return RecordingLocator(self, selector)

    async def select_option(self, selector: str, value: str) -> None:
        self.calls.append(("select_option", selector, value))

    def expect_navigation(self, *, wait_until: str, timeout: int | None = None) -> RecordingNavigationContext:
        self.calls.append(("expect_navigation", wait_until, timeout))
        return RecordingNavigationContext(self, wait_until, timeout)

    async def wait_for_selector(
        self,
        selector: str,
        state: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.calls.append(("wait_for_selector", selector, state, timeout))

    async def wait_for_function(
        self,
        expression: str,
        *,
        arg=None,
        timeout: int | None = None,
    ) -> None:
        self.calls.append(("wait_for_function", expression, arg, timeout))


class CaptureNavigationTests(unittest.TestCase):
    def test_configured_initial_wait_until_is_used(self) -> None:
        target = build_target(
            target_id="korail",
            selection_mode="korail_select",
            initial_wait_until="commit",
        )

        self.assertEqual(initial_navigation_wait_until(target), "commit")

    def test_other_targets_use_domcontentloaded_on_initial_load(self) -> None:
        target = build_target(target_id="korail", selection_mode="korail_select")

        self.assertEqual(initial_navigation_wait_until(target), "domcontentloaded")

    def test_configured_submit_wait_until_is_used(self) -> None:
        target = build_target(
            target_id="korail",
            selection_mode="korail_select",
            submit_wait_until="commit",
        )

        self.assertEqual(submit_navigation_wait_until(target), "commit")

    def test_default_submit_wait_until_is_networkidle(self) -> None:
        target = build_target(target_id="korail", selection_mode="korail_select")

        self.assertEqual(submit_navigation_wait_until(target), "networkidle")


class KorailCaptureTests(unittest.TestCase):
    def test_capture_korail_submits_with_configured_wait_until(self) -> None:
        target = TargetConfig(
            id="korail",
            name="코레일",
            url="https://example.com",
            enabled=True,
            selection_mode="korail_select",
            capture_selector="div.container",
            wait_selector='select[name="indate"]',
            submit_selector='button[type="submit"]',
            initial_wait_until="commit",
            submit_wait_until="commit",
        )
        page = RecordingPage()
        service = PlaywrightCaptureService(timeout_ms=2468)

        asyncio.run(service._capture_korail(page, target, capture_date=date(2026, 6, 3)))

        self.assertEqual(
            page.calls,
            [
                ("select_option", 'select[name="indate"]', "2026-06-03"),
                ("expect_navigation", "commit", 2468),
                ("navigation_enter", "commit", 2468),
                ("locator", 'button[type="submit"]'),
                ("click", 'button[type="submit"]'),
                ("navigation_exit", "commit", 2468),
                ("wait_for_selector", 'select[name="indate"]', "attached", 2468),
            ],
        )


class SeoulMetroCaptureTests(unittest.TestCase):
    def test_capture_seoulmetro_submits_then_waits_for_ready_state(self) -> None:
        target = TargetConfig(
            id="seoulmetro",
            name="서울교통공사",
            url="https://example.com",
            enabled=True,
            selection_mode="seoulmetro_select",
            capture_selector="#contents",
            wait_selector="#view_date",
            submit_selector="a[href*='document.searchForm.submit']",
            initial_wait_until="commit",
            submit_wait_until="commit",
        )
        page = RecordingPage(
            options=[
                {"value": "0", "text": "금일 (2026-05-29)"},
                {"value": "1", "text": "1일전 (2026-05-28)"},
            ]
        )
        service = PlaywrightCaptureService(timeout_ms=3210)
        service._wait_for_seoulmetro_capture_ready = AsyncMock(
            side_effect=lambda page_arg, target_arg: page_arg.calls.append(
                ("wait_ready", target_arg.capture_selector)
            )
        )

        asyncio.run(service._capture_seoulmetro(page, target, capture_date=date(2026, 5, 28)))

        self.assertEqual(
            page.calls,
            [
                ("locator", "#view_date option"),
                ("evaluate_all", "#view_date option"),
                ("select_option", "#view_date", "1"),
                ("expect_navigation", "commit", 3210),
                ("navigation_enter", "commit", 3210),
                ("locator", "a[href*='document.searchForm.submit']"),
                ("click", "a[href*='document.searchForm.submit']"),
                ("navigation_exit", "commit", 3210),
                ("wait_ready", "#contents"),
            ],
        )

    def test_wait_for_seoulmetro_capture_ready_waits_for_bottom_markers_then_height(self) -> None:
        target = TargetConfig(
            id="seoulmetro",
            name="서울교통공사",
            url="https://example.com",
            enabled=True,
            selection_mode="seoulmetro_select",
            capture_selector="#contents",
            wait_selector="#view_date",
            submit_selector="a[href*='document.searchForm.submit']",
            initial_wait_until="commit",
            submit_wait_until="commit",
        )
        page = RecordingPage()
        service = PlaywrightCaptureService(timeout_ms=4321)
        service._wait_for_seoulmetro_bottom_markers = AsyncMock(
            side_effect=lambda page_arg, selector: page_arg.calls.append(("bottom_markers", selector))
        )
        service._wait_for_stable_height = AsyncMock(
            side_effect=lambda page_arg, selector: page_arg.calls.append(("stable_height", selector))
        )

        asyncio.run(service._wait_for_seoulmetro_capture_ready(page, target))

        self.assertEqual(
            page.calls,
            [
                ("wait_for_selector", "#view_date", "attached", 4321),
                ("wait_for_selector", SEOULMETRO_TABLE_SELECTOR, "attached", 4321),
                ("bottom_markers", "#contents"),
                ("stable_height", "#contents"),
            ],
        )

    def test_wait_for_seoulmetro_bottom_markers_requires_line_and_notice_text(self) -> None:
        page = RecordingPage()
        service = PlaywrightCaptureService(timeout_ms=5432)

        asyncio.run(service._wait_for_seoulmetro_bottom_markers(page, "#contents"))

        self.assertEqual(len(page.calls), 1)
        call_name, expression, arg, timeout = page.calls[0]
        self.assertEqual(call_name, "wait_for_function")
        self.assertIn("lineText", expression)
        self.assertEqual(
            arg,
            {
                "selector": "#contents",
                "lineText": SEOULMETRO_BOTTOM_MARKERS[0],
                "noticeText": SEOULMETRO_BOTTOM_MARKERS[1],
            },
        )
        self.assertEqual(timeout, 5432)


if __name__ == "__main__":
    unittest.main()
