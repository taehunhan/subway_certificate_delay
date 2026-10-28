from __future__ import annotations

import asyncio
import io
import json
import logging
import subprocess
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

from subway_delay.capture import PlaywrightCaptureService, fetch_seoulmetro_html_pair_curl
from subway_delay.curl_http import CurlSession, valid_direct_url
from test_capture import RecordingPage, build_target


SOURCE = "http://example.com/list?menuIdx=543"
DIRECT = "https://example.com:444/list?menuIdx=543"
INITIAL = '<select id="view_date"><option value="0" selected>2026-10-28</option><option value="1">2026-10-27</option></select>'
SELECTED = '<select id="view_date"><option value="0">2026-10-28</option><option value="1" selected>2026-10-27</option></select>'


class CurlHttpTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.Logger("curl-test")
        self.log = io.StringIO()
        self.logger.addHandler(logging.StreamHandler(self.log))

    def response(self, command, html=SELECTED, *, status=200, final_url=DIRECT, content_type="text/html; charset=utf-8"):
        body_path = Path(command[command.index("--output") + 1])
        body_path.write_bytes(html.encode("utf-8") if isinstance(html, str) else html)
        metadata = {"http_code": status, "url_effective": final_url, "content_type": content_type}
        return subprocess.CompletedProcess(command, 0, json.dumps(metadata).encode(), b"PRIVATE STDERR")

    def test_get_post_share_cookie_file_and_post_to_get_final_url(self):
        calls, paths = [], []
        get_final_url = DIRECT + "&page=1"
        post_final_url = DIRECT + "&result=1"
        def run(command, **kwargs):
            calls.append(command)
            self.assertEqual(command[:2], ["curl", "--disable"])
            self.assertIn("--http1.1", command)
            self.assertIn("--globoff", command)
            for flag in ("--insecure", "-k", "--retry", "--post302", "--request"):
                self.assertNotIn(flag, command)
            self.assertEqual(command[command.index("--proto-redir") + 1], "=https")
            self.assertEqual(command[command.index("--max-redirs") + 1], "5")
            self.assertEqual(kwargs["timeout"], 35)
            self.assertNotIn("shell", kwargs)
            cookie = Path(command[command.index("--cookie") + 1])
            self.assertEqual(str(cookie), command[command.index("--cookie-jar") + 1])
            paths.append(cookie.parent)
            if len(calls) == 1:
                self.assertEqual(command[command.index("--url") + 1], DIRECT)
                self.assertIsNone(kwargs["input"])
                self.assertNotIn("--data-binary", command)
                cookie.write_text("PRIVATE COOKIE")
                return self.response(command, INITIAL, final_url=get_final_url)
            self.assertEqual(cookie.read_text(), "PRIVATE COOKIE")
            self.assertEqual(command[command.index("--url") + 1], get_final_url)
            self.assertEqual(kwargs["input"], b"view_date=1")
            self.assertEqual(command[command.index("--data-binary") + 1], "@-")
            self.assertIn("Content-Type: application/x-www-form-urlencoded", command)
            return self.response(command, SELECTED, final_url=post_final_url)
        with patch("subway_delay.curl_http.subprocess.run", side_effect=run):
            result = fetch_seoulmetro_html_pair_curl(
                url=SOURCE, direct_url=DIRECT, capture_date=date(2026, 10, 27),
                timeout_seconds=30, request_logger=self.logger,
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual(paths[0], paths[1])
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(result.initial_html, INITIAL)
        self.assertEqual(result.selected_html, SELECTED)
        self.assertEqual(result.final_url, post_final_url)
        self.assertIn("curl POST date verified", self.log.getvalue())
        self.assertNotIn("PRIVATE", self.log.getvalue())

    def test_http_error_status_is_reported_and_temp_files_are_removed(self):
        paths = []
        def run(command, **kwargs):
            paths.append(Path(command[command.index("--output") + 1]).parent)
            return self.response(command, "PRIVATE BODY", status=503)
        with patch("subway_delay.curl_http.subprocess.run", side_effect=run) as run_mock:
            with self.assertRaisesRegex(RuntimeError, "HTTP 503") as error:
                fetch_seoulmetro_html_pair_curl(
                    url=SOURCE, direct_url=DIRECT, capture_date=date(2026, 10, 27),
                    timeout_seconds=30, request_logger=self.logger,
                )
        self.assertEqual(run_mock.call_count, 1)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertNotIn("PRIVATE", str(error.exception) + self.log.getvalue())

    def test_request_failures_do_not_retry_or_leak_raw_response(self):
        effects = [
            (subprocess.TimeoutExpired(["curl", "PRIVATE"], 35, stderr=b"PRIVATE"), "process timeout"),
            (FileNotFoundError("PRIVATE"), "FileNotFoundError"),
            (subprocess.CompletedProcess([], 52, b"PRIVATE", b"PRIVATE"), "exit code 52"),
            (subprocess.CompletedProcess([], 60, b"PRIVATE", b"PRIVATE"), "exit code 60"),
            (subprocess.CompletedProcess([], 0, b"PRIVATE", b"PRIVATE"), "invalid curl metadata"),
        ]
        for effect, message in effects:
            with self.subTest(message=message), patch("subway_delay.curl_http.subprocess.run", side_effect=[effect]) as run:
                with self.assertRaisesRegex(RuntimeError, message) as error:
                    with CurlSession(target_url=SOURCE, timeout_seconds=30, logger=self.logger) as session:
                        session.fetch(DIRECT + "&token=PRIVATE")
                self.assertEqual(run.call_count, 1)
                self.assertNotIn("PRIVATE", str(error.exception) + self.log.getvalue())

    def test_server_charset_is_used(self):
        body = "서울교통공사".encode("euc-kr")
        with patch("subway_delay.curl_http.subprocess.run", side_effect=lambda command, **kw: self.response(command, body, content_type="text/html; charset=euc-kr")):
            with CurlSession(target_url=SOURCE, timeout_seconds=30, logger=self.logger) as session:
                self.assertEqual(session.fetch(DIRECT).html, "서울교통공사")

    def test_wrong_post_date_is_rejected_with_no_third_request(self):
        with patch("subway_delay.curl_http.subprocess.run", side_effect=lambda command, **kw: self.response(command, INITIAL)) as run:
            with self.assertRaisesRegex(ValueError, "curl POST HTML"):
                fetch_seoulmetro_html_pair_curl(
                    url=SOURCE, direct_url=DIRECT, capture_date=date(2026, 10, 27),
                    timeout_seconds=30, request_logger=self.logger,
                )
            self.assertEqual(run.call_count, 2)

    def test_missing_requested_date_stops_after_get(self):
        with patch("subway_delay.curl_http.subprocess.run", side_effect=lambda command, **kw: self.response(command, INITIAL)) as run:
            with self.assertRaisesRegex(ValueError, "2026-10-23"):
                fetch_seoulmetro_html_pair_curl(
                    url=SOURCE, direct_url=DIRECT, capture_date=date(2026, 10, 23),
                    timeout_seconds=30, request_logger=self.logger,
                )
            self.assertEqual(run.call_count, 1)

    def test_invalid_destination_is_rejected_before_curl(self):
        for url in (SOURCE, "file:///tmp/file", "https://elsewhere.com/list", "https://example.com/other", "https://user:secret@example.com/list", DIRECT + "#fragment", DIRECT + "\n"):
            with self.subTest(url=url), patch("subway_delay.curl_http.subprocess.run") as run:
                self.assertFalse(valid_direct_url(SOURCE, url))
                with self.assertRaises(ValueError):
                    with CurlSession(target_url=SOURCE, timeout_seconds=30, logger=self.logger) as session:
                        session.fetch(url)
                run.assert_not_called()

    def test_curl_capture_reuses_rendering_and_rechecks_actual_dom_date(self):
        target = build_target(target_id="seoulmetro", selection_mode="seoulmetro_select")
        from dataclasses import replace
        from subway_delay.capture import SeoulMetroHtmlResult
        target = replace(target, url=SOURCE)
        for actual_date in ("2026-10-27", "2026-10-28"):
            with self.subTest(actual_date=actual_date):
                page = RecordingPage(options=[{"value": "1", "text": actual_date, "selected": True}])
                service = PlaywrightCaptureService(seoulmetro_curl_url=DIRECT, request_logger=self.logger)
                service._wait_for_seoulmetro_capture_ready = AsyncMock()
                result = SeoulMetroHtmlResult(INITIAL, SELECTED, DIRECT)
                with patch("subway_delay.capture.fetch_seoulmetro_html_pair_curl", return_value=result) as fetch, patch("subway_delay.capture.fetch_seoulmetro_html_pair") as original:
                    if actual_date == "2026-10-27":
                        asyncio.run(service._capture_seoulmetro(page, target, date(2026, 10, 27)))
                    else:
                        with self.assertRaisesRegex(ValueError, "rendered DOM"):
                            asyncio.run(service._capture_seoulmetro(page, target, date(2026, 10, 27)))
                    original.assert_not_called()
                    fetch.assert_called_once()
                    self.assertEqual(fetch.call_args.kwargs["direct_url"], DIRECT)
                self.assertIn(("route", DIRECT), page.calls)
                service._wait_for_seoulmetro_capture_ready.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
