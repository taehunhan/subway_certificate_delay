from __future__ import annotations

import asyncio
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from subway_delay.capture import (
    GOTO_RETRY_DELAY_SECONDS,
    KORAIL_DATE_SELECTOR,
    KORAIL_TABLE_SELECTOR,
    SEOULMETRO_BOTTOM_MARKERS,
    SEOULMETRO_TABLE_SELECTOR,
    PlaywrightCaptureService,
    build_korail_form_data,
    fetch_korail_html_pair,
    https_fallback_url,
    inject_base_href,
    initial_navigation_wait_until,
    korail_base_href,
    should_retry_with_https_fallback,
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
    def __init__(
        self,
        options: list[dict[str, str]] | None = None,
        goto_side_effects: list[Exception | None] | None = None,
    ) -> None:
        self.options = options or []
        self.goto_side_effects = goto_side_effects or []
        self.calls: list[tuple] = []

    async def goto(self, url: str, *, wait_until: str, timeout: int | None = None) -> None:
        self.calls.append(("goto", url, wait_until, timeout))
        if self.goto_side_effects:
            side_effect = self.goto_side_effects.pop(0)
            if side_effect is not None:
                raise side_effect

    async def set_content(self, html: str, *, wait_until: str) -> None:
        self.calls.append(("set_content", html, wait_until))

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


class TimeoutError(Exception):
    pass


class FakeAPIResponse:
    def __init__(self, body: str, *, ok: bool = True, status: int = 200) -> None:
        self._body = body
        self.ok = ok
        self.status = status

    async def text(self) -> str:
        return self._body


class FakeAPIRequestContext:
    def __init__(self, responses: list[FakeAPIResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple] = []

    async def get(self, url: str, *, timeout: int | None = None):
        self.calls.append(("get", url, timeout))
        return self.responses.pop(0)

    async def post(
        self,
        url: str,
        *,
        data=None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
    ):
        self.calls.append(("post", url, data, headers, timeout))
        return self.responses.pop(0)

    async def dispose(self) -> None:
        self.calls.append(("dispose",))


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

    def test_https_fallback_url_upgrades_http(self) -> None:
        self.assertEqual(
            https_fallback_url("http://www.seoulmetro.co.kr/kr/delayProofList.do?menuIdx=543"),
            "https://www.seoulmetro.co.kr/kr/delayProofList.do?menuIdx=543",
        )

    def test_https_fallback_url_ignores_non_http(self) -> None:
        self.assertIsNone(
            https_fallback_url("https://www.seoulmetro.co.kr/kr/delayProofList.do?menuIdx=543")
        )

    def test_timeout_on_http_is_retryable_with_https_fallback(self) -> None:
        self.assertTrue(
            should_retry_with_https_fallback(
                "http://www.seoulmetro.co.kr/kr/delayProofList.do?menuIdx=543",
                TimeoutError("timed out"),
            )
        )

    def test_non_timeout_error_does_not_trigger_https_fallback(self) -> None:
        self.assertFalse(
            should_retry_with_https_fallback(
                "http://www.seoulmetro.co.kr/kr/delayProofList.do?menuIdx=543",
                RuntimeError("boom"),
            )
        )

    def test_inject_base_href_inserts_tag_before_head_close(self) -> None:
        html = "<html><head><title>Korail</title></head><body></body></html>"

        self.assertEqual(
            inject_base_href(html, "https://info.korail.com/mbs/www/neo/delay/"),
            '<html><head><title>Korail</title><base href="https://info.korail.com/mbs/www/neo/delay/"></head><body></body></html>',
        )

    def test_korail_base_href_returns_directory_url(self) -> None:
        self.assertEqual(
            korail_base_href("https://info.korail.com/mbs/www/neo/delay/delaylist.jsp"),
            "https://info.korail.com/mbs/www/neo/delay/",
        )

    def test_build_korail_form_data_encodes_indate(self) -> None:
        self.assertEqual(
            build_korail_form_data(date(2026, 7, 1)),
            b"indate=2026-07-01",
        )


class KorailCaptureTests(unittest.TestCase):
    def test_fetch_korail_html_pair_uses_get_then_post(self) -> None:
        request_context = FakeAPIRequestContext(
            responses=[
                FakeAPIResponse("<html><head></head><body>initial</body></html>"),
                FakeAPIResponse("<html><head></head><body>selected</body></html>"),
            ]
        )

        initial_html, selected_html = asyncio.run(
            fetch_korail_html_pair(
                request_context=request_context,
                url="https://info.korail.com/mbs/www/neo/delay/delaylist.jsp",
                capture_date=date(2026, 7, 1),
                timeout_ms=12_500,
            )
        )

        self.assertEqual(initial_html, "<html><head></head><body>initial</body></html>")
        self.assertEqual(selected_html, "<html><head></head><body>selected</body></html>")
        self.assertEqual(
            request_context.calls,
            [
                (
                    "get",
                    "https://info.korail.com/mbs/www/neo/delay/delaylist.jsp",
                    12_500,
                ),
                (
                    "post",
                    "https://info.korail.com/mbs/www/neo/delay/delaylist.jsp",
                    b"indate=2026-07-01",
                    {"Content-Type": "application/x-www-form-urlencoded"},
                    12_500,
                ),
            ],
        )

    def test_capture_korail_uses_set_content_without_goto(self) -> None:
        target = TargetConfig(
            id="korail",
            name="코레일",
            url="https://info.korail.com/mbs/www/neo/delay/delaylist.jsp",
            enabled=True,
            selection_mode="korail_select",
            capture_selector="div.container",
            wait_selector=KORAIL_DATE_SELECTOR,
            submit_selector='button[type="submit"]',
            initial_wait_until="commit",
            submit_wait_until="commit",
        )
        page = RecordingPage()
        request_context = FakeAPIRequestContext(
            responses=[
                FakeAPIResponse("<html><head></head><body>initial</body></html>"),
                FakeAPIResponse("<html><head></head><body>selected</body></html>"),
            ]
        )
        service = PlaywrightCaptureService(timeout_ms=2468)
        service._wait_for_korail_capture_ready = AsyncMock(
            side_effect=lambda page_arg, capture_date: page_arg.calls.append(
                ("korail_ready", capture_date.isoformat())
            )
        )

        asyncio.run(
            service._capture_korail(
                page,
                target,
                capture_date=date(2026, 7, 1),
                request_context=request_context,
            )
        )

        self.assertEqual(
            page.calls,
            [
                (
                    "set_content",
                    '<html><head><base href="https://info.korail.com/mbs/www/neo/delay/"></head><body>initial</body></html>',
                    "domcontentloaded",
                ),
                ("wait_for_selector", KORAIL_DATE_SELECTOR, "attached", 2468),
                (
                    "set_content",
                    '<html><head><base href="https://info.korail.com/mbs/www/neo/delay/"></head><body>selected</body></html>',
                    "domcontentloaded",
                ),
                ("korail_ready", "2026-07-01"),
            ],
        )
        self.assertEqual(
            request_context.calls,
            [
                (
                    "get",
                    "https://info.korail.com/mbs/www/neo/delay/delaylist.jsp",
                    2468,
                ),
                (
                    "post",
                    "https://info.korail.com/mbs/www/neo/delay/delaylist.jsp",
                    b"indate=2026-07-01",
                    {"Content-Type": "application/x-www-form-urlencoded"},
                    2468,
                ),
            ],
        )
        self.assertNotIn(("goto",), [call[:1] for call in page.calls])

    def test_wait_for_korail_capture_ready_waits_for_selector_and_selected_date(self) -> None:
        page = RecordingPage()
        service = PlaywrightCaptureService(timeout_ms=2468)

        asyncio.run(service._wait_for_korail_capture_ready(page, date(2026, 7, 1)))

        self.assertEqual(page.calls[0], ("wait_for_selector", KORAIL_DATE_SELECTOR, "attached", 2468))
        self.assertEqual(page.calls[1], ("wait_for_selector", KORAIL_TABLE_SELECTOR, "attached", 2468))
        call_name, expression, arg, timeout = page.calls[2]
        self.assertEqual(call_name, "wait_for_function")
        self.assertIn("select.value === captureDate", expression)
        self.assertEqual(
            arg,
            {
                "selector": KORAIL_DATE_SELECTOR,
                "captureDate": "2026-07-01",
                "tableSelector": KORAIL_TABLE_SELECTOR,
            },
        )
        self.assertEqual(timeout, 2468)

    def test_initial_goto_retries_same_https_url_after_timeout(self) -> None:
        target = TargetConfig(
            id="korail",
            name="코레일",
            url="https://info.korail.com/mbs/www/neo/delay/delaylist.jsp",
            enabled=True,
            selection_mode="korail_select",
            capture_selector="div.container",
            wait_selector='select[name="indate"]',
            submit_selector='button[type="submit"]',
            initial_wait_until="commit",
            submit_wait_until="commit",
        )
        page = RecordingPage(goto_side_effects=[TimeoutError("timeout"), None])
        service = PlaywrightCaptureService(timeout_ms=1357)

        with patch("subway_delay.capture.asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
            asyncio.run(service._goto_target_page(page, target))

        sleep_mock.assert_awaited_once_with(GOTO_RETRY_DELAY_SECONDS)
        self.assertEqual(
            page.calls,
            [
                (
                    "goto",
                    "https://info.korail.com/mbs/www/neo/delay/delaylist.jsp",
                    "commit",
                    1357,
                ),
                (
                    "goto",
                    "https://info.korail.com/mbs/www/neo/delay/delaylist.jsp",
                    "commit",
                    1357,
                ),
            ],
        )

    def test_initial_goto_retries_with_https_after_http_timeout(self) -> None:
        target = TargetConfig(
            id="seoulmetro",
            name="서울교통공사",
            url="http://www.seoulmetro.co.kr/kr/delayProofList.do?menuIdx=543",
            enabled=True,
            selection_mode="seoulmetro_select",
            capture_selector="#contents",
            wait_selector="#view_date",
            submit_selector="a[href*='document.searchForm.submit']",
            initial_wait_until="commit",
            submit_wait_until="commit",
        )
        page = RecordingPage(goto_side_effects=[TimeoutError("timeout"), None])
        service = PlaywrightCaptureService(timeout_ms=1357)

        with patch("subway_delay.capture.asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
            asyncio.run(service._goto_target_page(page, target))

        sleep_mock.assert_not_awaited()
        self.assertEqual(
            page.calls,
            [
                (
                    "goto",
                    "http://www.seoulmetro.co.kr/kr/delayProofList.do?menuIdx=543",
                    "commit",
                    1357,
                ),
                (
                    "goto",
                    "https://www.seoulmetro.co.kr/kr/delayProofList.do?menuIdx=543",
                    "commit",
                    1357,
                ),
            ],
        )

    def test_initial_goto_does_not_retry_on_non_timeout_error(self) -> None:
        target = TargetConfig(
            id="korail",
            name="코레일",
            url="https://info.korail.com/mbs/www/neo/delay/delaylist.jsp",
            enabled=True,
            selection_mode="korail_select",
            capture_selector="div.container",
            wait_selector='select[name="indate"]',
            submit_selector='button[type="submit"]',
            initial_wait_until="commit",
            submit_wait_until="commit",
        )
        page = RecordingPage(goto_side_effects=[RuntimeError("boom")])
        service = PlaywrightCaptureService(timeout_ms=1357)

        with patch("subway_delay.capture.asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
            with self.assertRaises(RuntimeError):
                asyncio.run(service._goto_target_page(page, target))

        sleep_mock.assert_not_awaited()
        self.assertEqual(
            page.calls,
            [
                (
                    "goto",
                    "https://info.korail.com/mbs/www/neo/delay/delaylist.jsp",
                    "commit",
                    1357,
                ),
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
