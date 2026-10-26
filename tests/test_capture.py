from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from dataclasses import dataclass, replace
from datetime import date
from email.message import Message
from http.client import RemoteDisconnected
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.error import HTTPError

from subway_delay.capture import (
    GOTO_RETRY_DELAY_SECONDS,
    KORAIL_DATE_SELECTOR,
    KORAIL_TABLE_SELECTOR,
    SEOULMETRO_BOTTOM_MARKERS,
    SEOULMETRO_TABLE_SELECTOR,
    PlaywrightCaptureService,
    SelectOption,
    SeoulMetroHtmlResult,
    build_korail_form_data,
    build_seoulmetro_form_data,
    extract_select_options,
    fetch_korail_html_pair,
    fetch_seoulmetro_html_pair,
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


class RecordingRoute:
    def __init__(self, page: "RecordingPage", url: str) -> None:
        self.page = page
        self.url = url

    async def fulfill(
        self,
        *,
        status: int,
        headers: dict[str, str] | None = None,
        body: str | None = None,
    ) -> None:
        self.page.calls.append(("fulfill", self.url, status, headers, body))


@dataclass
class FakeNavigationResponse:
    status: int = 200
    status_text: str = "OK"
    url: str = "https://example.com"


class RecordingPage:
    def __init__(
        self,
        options: list[dict[str, str | bool]] | None = None,
        goto_side_effects: list[Exception | FakeNavigationResponse | None] | None = None,
    ) -> None:
        self.options = options or []
        self.url = "about:blank"
        self.goto_side_effects = goto_side_effects or []
        self.calls: list[tuple] = []
        self.route_handlers: list[tuple[str, object]] = []

    async def goto(
        self, url: str, *, wait_until: str, timeout: int | None = None
    ) -> FakeNavigationResponse | None:
        self.calls.append(("goto", url, wait_until, timeout))
        self.url = url
        for route_url, handler in list(self.route_handlers):
            if route_url == url:
                await handler(RecordingRoute(self, url))
                break
        if self.goto_side_effects:
            side_effect = self.goto_side_effects.pop(0)
            if isinstance(side_effect, Exception):
                raise side_effect
            return side_effect
        return None

    async def set_content(self, html: str, *, wait_until: str) -> None:
        self.calls.append(("set_content", html, wait_until))

    def locator(self, selector: str) -> RecordingLocator:
        self.calls.append(("locator", selector))
        return RecordingLocator(self, selector)

    async def route(self, url: str, handler) -> None:
        self.calls.append(("route", url))
        self.route_handlers.append((url, handler))

    async def unroute(self, url: str, handler=None) -> None:
        self.calls.append(("unroute", url))
        self.route_handlers = [
            (route_url, route_handler)
            for route_url, route_handler in self.route_handlers
            if not (route_url == url and (handler is None or route_handler is handler))
        ]

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


class FakeResponse:
    def __init__(
        self, body: str, charset: str = "utf-8", *, url: str = "https://example.com", status: int = 200
    ) -> None:
        self._body = body.encode(charset)
        self.url = url
        self.status = status
        self.closed = False
        self.headers = Message()
        self.headers["Content-Type"] = f"text/html; charset={charset}"

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.closed = True
        return False

    def geturl(self) -> str:
        if self.closed:
            raise RuntimeError("URL must be read before closing response")
        return self.url

    def read(self) -> bytes:
        return self._body


class FakeOpener:
    def __init__(
        self,
        responses: list[FakeResponse | Exception],
        redirects: list[list[tuple[int, str]]] | None = None,
    ) -> None:
        self.responses = responses
        self.redirects = redirects or []
        self.handlers = []
        self.calls: list[tuple[object, float | None]] = []
        self.addheaders: list[tuple[str, str]] = []

    def open(self, request, timeout: float | None = None):
        self.calls.append((request, timeout))
        call_index = len(self.calls) - 1
        if call_index < len(self.redirects):
            for status, url in self.redirects[call_index]:
                request = self.handlers[0].redirect_request(request, None, status, "Redirect", {}, url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)


def seoulmetro_html(selected_value: str | None = "3") -> str:
    today_selected = " selected" if selected_value == "0" else ""
    target_selected = " selected" if selected_value == "3" else ""
    return (
        '<html><head></head><body><select id="view_date" name="view_date">'
        f'<option value="0"{today_selected}>Today (2026-10-26)</option>'
        f'<option value="3"{target_selected}>3 days ago (2026-10-23)</option>'
        '</select></body></html>'
    )


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

    def test_build_seoulmetro_form_data_encodes_view_date(self) -> None:
        self.assertEqual(build_seoulmetro_form_data("14"), b"view_date=14")

    def test_extract_select_options_reads_target_select(self) -> None:
        html = """
        <html><body>
            <select id="other">
                <option value="x">ignore</option>
            </select>
            <select name="view_date" id="view_date">
                <option value="0">금일 (2026-07-15)</option>
                <option value="1">1일전 (2026-07-14)</option>
            </select>
        </body></html>
        """

        options = extract_select_options(html, "view_date")
        self.assertEqual(
            [(option.value, option.text) for option in options],
            [("0", "금일 (2026-07-15)"), ("1", "1일전 (2026-07-14)")],
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


class BrowserNavigationTests(unittest.TestCase):
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

    def test_initial_goto_accepts_non_error_and_none_responses(self) -> None:
        target = build_target(target_id="ui-line", selection_mode="ui_line_tab")
        service = PlaywrightCaptureService(timeout_ms=1357)
        for status in (200, 204, 302, 399, None):
            with self.subTest(status=status):
                response = FakeNavigationResponse(status=status) if status is not None else None
                page = RecordingPage(goto_side_effects=[response])

                asyncio.run(service._goto_target_page(page, target))

                self.assertEqual(page.calls, [("goto", target.url, "domcontentloaded", 1357)])

    def test_initial_http_errors_report_status_and_urls_without_retry(self) -> None:
        service = PlaywrightCaptureService(timeout_ms=1357)
        for scheme in ("http", "https"):
            for status, status_text in ((403, "Forbidden"), (404, "Not Found"), (503, "")):
                with self.subTest(scheme=scheme, status=status):
                    target = replace(
                        build_target(target_id="ui-line", selection_mode="ui_line_tab"),
                        url=f"{scheme}://example.com/certificates",
                    )
                    response = FakeNavigationResponse(
                        status=status, status_text=status_text, url="https://example.com/maintenance"
                    )
                    page = RecordingPage(goto_side_effects=[response])
                    with patch("subway_delay.capture.asyncio.sleep", new_callable=AsyncMock) as sleep:
                        with self.assertRaises(RuntimeError) as raised:
                            asyncio.run(service._goto_target_page(page, target))

                    status_description = f"HTTP {status}" + (f" {status_text}" if status_text else "")
                    self.assertEqual(
                        str(raised.exception),
                        f"Initial navigation failed for ui-line: {status_description}; "
                        f"requested_url={target.url}; response_url={response.url}",
                    )
                    self.assertEqual(page.calls, [("goto", target.url, "domcontentloaded", 1357)])
                    sleep.assert_not_awaited()

    def test_retry_and_https_fallback_check_http_errors_without_third_attempt(self) -> None:
        for scheme in ("http", "https"):
            with self.subTest(scheme=scheme):
                target = replace(
                    build_target(target_id="ui-line", selection_mode="ui_line_tab"),
                    url=f"{scheme}://example.com",
                )
                response = FakeNavigationResponse(status=503, status_text="Service Unavailable")
                page = RecordingPage(goto_side_effects=[TimeoutError("timeout"), response])
                service = PlaywrightCaptureService(timeout_ms=1357)

                with patch("subway_delay.capture.asyncio.sleep", new_callable=AsyncMock) as sleep:
                    with self.assertRaises(RuntimeError) as raised:
                        asyncio.run(service._goto_target_page(page, target))

                self.assertIn("HTTP 503 Service Unavailable", str(raised.exception))
                self.assertIn("requested_url=https://example.com", str(raised.exception))
                self.assertEqual(
                    page.calls,
                    [
                        ("goto", target.url, "domcontentloaded", 1357),
                        ("goto", "https://example.com", "domcontentloaded", 1357),
                    ],
                )
                if scheme == "http":
                    sleep.assert_not_awaited()
                else:
                    sleep.assert_awaited_once_with(GOTO_RETRY_DELAY_SECONDS)

    def test_second_navigation_timeout_is_propagated_without_third_attempt(self) -> None:
        for scheme in ("http", "https"):
            with self.subTest(scheme=scheme):
                target = replace(
                    build_target(target_id="ui-line", selection_mode="ui_line_tab"),
                    url=f"{scheme}://example.com",
                )
                page = RecordingPage(
                    goto_side_effects=[TimeoutError("first timeout"), TimeoutError("second timeout")]
                )
                service = PlaywrightCaptureService()
                with patch("subway_delay.capture.asyncio.sleep", new_callable=AsyncMock):
                    with self.assertRaisesRegex(TimeoutError, "second timeout"):
                        asyncio.run(service._goto_target_page(page, target))
                self.assertEqual(len(page.calls), 2)

    def test_capture_stops_before_date_selection_and_screenshot_on_http_error(self) -> None:
        for status in (403, 404, 503):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp_dir:
                target = build_target(target_id="ui-line", selection_mode="ui_line_tab")
                page = RecordingPage(
                    goto_side_effects=[FakeNavigationResponse(status=status, status_text="")]
                )
                context = AsyncMock()
                context.new_page.return_value = page
                browser = AsyncMock()
                browser.new_context.return_value = context
                playwright = MagicMock()
                playwright.chromium.launch = AsyncMock(return_value=browser)
                manager = MagicMock()
                manager.__aenter__.return_value = playwright
                async_api = ModuleType("playwright.async_api")
                async_api.async_playwright = MagicMock(return_value=manager)
                modules = {"playwright": ModuleType("playwright"), "playwright.async_api": async_api}
                service = PlaywrightCaptureService(timeout_ms=1357)
                destination = Path(temp_dir) / "ui-line.png"

                with patch.dict("sys.modules", modules), patch.object(
                    service, "_apply_date_selection", new_callable=AsyncMock
                ) as select_date:
                    with self.assertRaisesRegex(RuntimeError, f"HTTP {status}"):
                        asyncio.run(
                            service.capture(
                                target=target, capture_date=date(2026, 10, 23), destination=destination
                            )
                        )

                select_date.assert_not_awaited()
                self.assertEqual(page.calls, [("goto", target.url, "domcontentloaded", 1357)])
                self.assertFalse(destination.exists())
                context.close.assert_awaited_once()
                browser.close.assert_awaited_once()


class SeoulMetroCaptureTests(unittest.TestCase):
    def test_fetch_seoulmetro_html_pair_uses_get_then_post_with_matching_option(self) -> None:
        source_url = "http://www.seoulmetro.co.kr/kr/delayProofList.do?menuIdx=543"
        for final_url in (source_url, source_url.replace("http://www.seoulmetro.co.kr", "https://www.seoulmetro.co.kr:444")):
            with self.subTest(final_url=final_url):
                responses = [
                    FakeResponse(seoulmetro_html("0"), url=final_url),
                    FakeResponse(seoulmetro_html("3"), url=final_url),
                ]
                opener = FakeOpener(responses=list(responses))

                result = fetch_seoulmetro_html_pair(
                    url=source_url, capture_date=date(2026, 10, 23), timeout_seconds=3.21, opener=opener,
                )

                self.assertEqual(result, SeoulMetroHtmlResult(seoulmetro_html("0"), seoulmetro_html("3"), final_url))
                self.assertTrue(all(response.closed for response in responses))
                self.assertEqual(len(opener.calls), 2)
                get_request, get_timeout = opener.calls[0]
                post_request, post_timeout = opener.calls[1]
                self.assertEqual(get_request.get_method(), "GET")
                self.assertEqual(get_request.full_url, source_url)
                self.assertIsNone(get_request.data)
                self.assertEqual(get_timeout, 3.21)
                self.assertEqual(post_request.get_method(), "POST")
                self.assertEqual(post_request.full_url, final_url)
                self.assertEqual(post_request.data, b"view_date=3")
                self.assertEqual(post_timeout, 3.21)

    def test_capture_seoulmetro_uses_routed_document_load(self) -> None:
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
        page = RecordingPage(options=[{"value": "3", "text": "3 days ago (2026-10-23)", "selected": True}])
        service = PlaywrightCaptureService(timeout_ms=3210)
        service._wait_for_seoulmetro_capture_ready = AsyncMock(
            side_effect=lambda page_arg, target_arg: page_arg.calls.append(
                ("wait_ready", target_arg.capture_selector)
            )
        )

        final_url = "https://www.seoulmetro.co.kr:444/kr/delayProofList.do?menuIdx=543"
        with patch(
            "subway_delay.capture.asyncio.to_thread",
            new=AsyncMock(
                return_value=SeoulMetroHtmlResult(
                    seoulmetro_html("0"), seoulmetro_html("3"), final_url,
                )
            ),
        ) as fetch:
            asyncio.run(service._capture_seoulmetro(page, target, capture_date=date(2026, 10, 23)))

        fetch.assert_awaited_once_with(
            fetch_seoulmetro_html_pair, url=target.url, capture_date=date(2026, 10, 23),
            timeout_seconds=3.21, target_id=target.id, logger=service.logger,
        )

        self.assertEqual(
            page.calls,
            [
                ("route", final_url),
                (
                    "goto",
                    final_url,
                    "domcontentloaded",
                    3210,
                ),
                (
                    "fulfill",
                    final_url,
                    200,
                    {"Content-Type": "text/html; charset=UTF-8"},
                    seoulmetro_html("3"),
                ),
                ("unroute", final_url),
                ("wait_ready", "#contents"),
                ("locator", "select#view_date option"),
                ("evaluate_all", "select#view_date option"),
            ],
        )
        self.assertNotIn(("set_content",), [call[:1] for call in page.calls])

    def test_selected_attribute_is_boolean_and_scoped_to_target_select(self) -> None:
        html = (
            '<select id="other"><option selected value="ignored">Ignored</option></select>'
            '<select id="view_date"><option value="0">Today</option>'
            '<option value="1" selected="selected">Yesterday</option>'
            '<option value="2" selected>Earlier</option></select>'
        )
        self.assertEqual(extract_select_options(html, "view_date"), [
            SelectOption("0", "Today"), SelectOption("1", "Yesterday", True), SelectOption("2", "Earlier", True),
        ])

    def test_post_html_rejects_missing_or_mismatched_selection(self) -> None:
        invalid_html = [
            "<html>Maintenance</html>",
            seoulmetro_html(None),
            seoulmetro_html("0"),
            seoulmetro_html("3").replace('value="0"', 'value="0" selected'),
            seoulmetro_html("3").replace('value="3"', 'value="2"'),
            seoulmetro_html("3").replace("2026-10-23", "2026-10-230"),
        ]
        for html in invalid_html:
            with self.subTest(html=html):
                opener = FakeOpener([FakeResponse(seoulmetro_html("0")), FakeResponse(html)])
                with self.assertRaises(ValueError) as raised:
                    fetch_seoulmetro_html_pair(
                        url="https://example.com", capture_date=date(2026, 10, 23),
                        timeout_seconds=1, opener=opener,
                    )
                self.assertIn("seoulmetro during POST HTML", str(raised.exception))
                self.assertIn("expected_date=2026-10-23", str(raised.exception))
                self.assertIn("selected_options=", str(raised.exception))
                self.assertEqual(len(opener.calls), 2)

    def test_missing_get_date_does_not_post(self) -> None:
        for html in ("<html>Maintenance</html>", seoulmetro_html("0").replace("2026-10-23", "2026-10-24")):
            with self.subTest(html=html):
                opener = FakeOpener([FakeResponse(html)])
                with self.assertRaisesRegex(ValueError, "during GET: expected_date=2026-10-23"):
                    fetch_seoulmetro_html_pair(
                        url="https://example.com", capture_date=date(2026, 10, 23),
                        timeout_seconds=1, opener=opener,
                    )
                self.assertEqual(len(opener.calls), 1)

    def test_post_redirect_url_is_returned_and_request_diagnostics_are_logged(self) -> None:
        source = "http://example.com/delay"
        get_url = "https://example.com:444/delay"
        post_url = "https://example.com:444/result"
        opener = FakeOpener(
            [FakeResponse(seoulmetro_html("0"), url=get_url), FakeResponse(seoulmetro_html("3"), url=post_url)],
            redirects=[[(302, get_url)], [(302, post_url)]],
        )
        log = logging.getLogger("test.seoulmetro")
        with self.assertLogs(log, level="INFO") as captured:
            result = fetch_seoulmetro_html_pair(
                url=source, capture_date=date(2026, 10, 23), timeout_seconds=1, opener=opener, logger=log,
            )
        self.assertEqual(result.final_url, post_url)
        messages = "\n".join(captured.output)
        self.assertIn(f"from_url={source} to_url={get_url}", messages)
        self.assertIn(f"from_url={get_url} to_url={post_url}", messages)
        self.assertIn(f"stage=POST status=200 response_url={post_url}", messages)
        self.assertIn("method=POST status=302", messages)
        self.assertNotIn("<html>", messages)

    def test_disconnect_reports_stage_and_last_redirect_without_internal_retry(self) -> None:
        for stage in ("GET", "POST"):
            with self.subTest(stage=stage):
                source = "http://example.com/delay"
                redirected = "https://example.com:444/delay"
                last_url = redirected if stage == "GET" else "https://example.com:444/result"
                error = RemoteDisconnected("Remote end closed connection without response")
                responses = [error] if stage == "GET" else [FakeResponse(seoulmetro_html("0"), url=redirected), error]
                redirects = [[(302, last_url)]] if stage == "GET" else [[(302, redirected)], [(302, last_url)]]
                opener = FakeOpener(responses, redirects=redirects)
                with self.assertRaises(RuntimeError) as raised:
                    fetch_seoulmetro_html_pair(
                        url=source, capture_date=date(2026, 10, 23), timeout_seconds=1, opener=opener,
                    )
                self.assertIs(raised.exception.__cause__, error)
                self.assertIn(f"for seoulmetro during {stage}: RemoteDisconnected", str(raised.exception))
                self.assertIn(f"last_url={last_url}", str(raised.exception))
                self.assertEqual(len(opener.calls), 1 if stage == "GET" else 2)

    def test_http_error_preserves_status_and_cause_without_internal_retry(self) -> None:
        url = "https://example.com:444/delay"
        error = HTTPError(url, 503, "Service Unavailable", {}, None)
        opener = FakeOpener([error])
        with self.assertLogs("subway_delay.capture", level="INFO") as captured:
            with self.assertRaises(RuntimeError) as raised:
                fetch_seoulmetro_html_pair(
                    url=url, capture_date=date(2026, 10, 23), timeout_seconds=1, opener=opener,
                )
        self.assertIs(raised.exception.__cause__, error)
        self.assertIn("HTTPError: HTTP Error 503", str(raised.exception))
        self.assertIn(f"stage=GET status=503 response_url={url}", "\n".join(captured.output))
        self.assertEqual(len(opener.calls), 1)

    def test_dom_date_change_stops_capture_before_screenshot(self) -> None:
        for options in ([], [{"value": "0", "text": "Today (2026-10-26)", "selected": True}]):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temp_dir:
                page = RecordingPage(options=options)
                context = AsyncMock()
                context.new_page.return_value = page
                browser = AsyncMock()
                browser.new_context.return_value = context
                playwright = MagicMock()
                playwright.chromium.launch = AsyncMock(return_value=browser)
                manager = MagicMock()
                manager.__aenter__.return_value = playwright
                async_api = ModuleType("playwright.async_api")
                async_api.async_playwright = MagicMock(return_value=manager)
                modules = {"playwright": ModuleType("playwright"), "playwright.async_api": async_api}
                service = PlaywrightCaptureService()
                target = build_target(target_id="seoulmetro", selection_mode="seoulmetro_select")
                result = SeoulMetroHtmlResult(seoulmetro_html("0"), seoulmetro_html("3"), "https://example.com:444")
                destination = Path(temp_dir) / "seoulmetro.png"
                with patch.dict("sys.modules", modules), patch(
                    "subway_delay.capture.asyncio.to_thread", new=AsyncMock(return_value=result)
                ), patch.object(service, "_wait_for_seoulmetro_capture_ready", new_callable=AsyncMock):
                    with self.assertRaisesRegex(ValueError, "during rendered DOM: expected_date=2026-10-23"):
                        asyncio.run(service.capture(
                            target=target, capture_date=date(2026, 10, 23), destination=destination,
                        ))
                self.assertFalse(destination.exists())
                self.assertNotIn(("locator", target.capture_selector), page.calls)
                self.assertFalse(any(call[0] == "wait_for_selector" for call in page.calls))
                context.close.assert_awaited_once()
                browser.close.assert_awaited_once()

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
