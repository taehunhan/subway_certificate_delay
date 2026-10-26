from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from http.client import RemoteDisconnected
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request

from subway_delay import diagnostics as diag


SOURCE = "http://example.com/delay?menuIdx=543"
FINAL = "https://example.com:444/delay?menuIdx=543"
BODY = b'<select id="view_date"></select><table class="extra tbl-type1"></table>'


def manifest(targets=None):
    return {"capture_date": "2026-10-23", "targets": targets if targets is not None else [{"id": "seoulmetro", "success": False}]}


def redirected(client, *, final=FINAL, error=None, status=200):
    return diag.ProbeResult(
        client, SOURCE, final, http_status=status, error_type=error,
        redirects=[{"status": 302, "from_url": SOURCE, "to_url": final}],
        view_date=True, tbl_type1=True,
    )


class DiagnosticGateTests(unittest.TestCase):
    def test_only_valid_final_seoulmetro_failure_enables_requests(self) -> None:
        cases = [
            (manifest(), True),
            (manifest([{"id": "seoulmetro", "success": True}, {"id": "ui-line", "success": False}]), False),
            (manifest([{"id": "ui-line", "success": False}]), False),
            (manifest([]), False),
            (manifest([{"id": "seoulmetro", "success": "false"}]), False),
            (manifest([{"id": "seoulmetro", "success": 0}]), False),
            (manifest([{"id": "seoulmetro", "success": False}] * 2), False),
            ({"capture_date": "bad", "targets": []}, False),
            ({"capture_date": "2026-10-23", "targets": None}, False),
            ([], False),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            for payload, expected in cases:
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload))
                    self.assertEqual(diag.should_diagnose(path)[0], expected)
            path.write_text("not-json")
            self.assertFalse(diag.should_diagnose(path)[0])
            self.assertFalse(diag.should_diagnose(Path(directory) / "missing")[0])

    def test_skip_writes_reports_without_loading_config_or_running_processes(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            with patch.object(diag, "load_config") as config, patch.object(diag.subprocess, "run") as run:
                code = diag.run_diagnostics(capture_result=root / "missing", config_path=root / "config", output_dir=root / "report")
            self.assertEqual(code, 0)
            config.assert_not_called()
            run.assert_not_called()
            report = json.loads((root / "report/report.json").read_text())
            self.assertTrue(report["skipped"])
            self.assertEqual(report["probes"], [])
            self.assertIn("missing", report["reason"])

    def test_disabled_or_missing_target_skips_network_probes(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            capture = root / "capture.json"
            capture.write_text(json.dumps(manifest()))
            with patch.object(diag, "load_config", return_value=SimpleNamespace(enabled_targets=[])), patch.object(
                diag, "_environment", return_value={}
            ), patch.object(diag, "probe_urllib") as urllib, patch.object(diag, "probe_curl") as curl:
                code = diag.run_diagnostics(capture_result=capture, config_path=root / "config", output_dir=root / "report")
            self.assertEqual(code, 0)
            urllib.assert_not_called()
            curl.assert_not_called()
            report = json.loads((root / "report/report.json").read_text())
            self.assertTrue(report["skipped"])
            self.assertIn("No enabled", report["reason"])

    def test_report_storage_failure_is_not_silently_successful(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(Path, "write_text", side_effect=OSError("storage failed")):
                with self.assertRaises(OSError):
                    diag.run_diagnostics(capture_result=root / "missing", config_path=root / "config", output_dir=root / "report")


class UrllibProbeTests(unittest.TestCase):
    def test_worker_preserves_redirect_before_disconnect_without_leaking_error_text(self) -> None:
        def build(*handlers):
            handler = handlers[1]
            opener = MagicMock()

            def disconnect(request, *, timeout):
                self.assertEqual(timeout, 30)
                handler.redirect_request(request, None, 302, "Found", {}, FINAL)
                raise RemoteDisconnected("SECRET FROM SERVER")

            opener.open.side_effect = disconnect
            return opener

        with patch.object(diag, "build_opener", side_effect=build), redirect_stdout(io.StringIO()) as stdout:
            diag.urllib_worker(SOURCE)
        snapshot = diag._last_snapshot(stdout.getvalue())
        self.assertEqual(snapshot["final_url"], FINAL)
        self.assertEqual(snapshot["error_type"], "RemoteDisconnected")
        self.assertEqual(len(snapshot["redirects"]), 1)
        self.assertIsNone(snapshot["http_status"])
        self.assertFalse(snapshot["tool_error"])
        self.assertNotIn("SECRET", stdout.getvalue())

    def test_worker_detects_html_markers_and_keeps_body_private(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = FINAL
        response.status = 200
        response.headers.get_content_charset.return_value = "utf-8"
        response.read.return_value = BODY + b"PRIVATE BODY CONTENT"
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(diag, "build_opener", return_value=opener), redirect_stdout(io.StringIO()) as stdout:
            diag.urllib_worker(FINAL)
        result = diag._last_snapshot(stdout.getvalue())
        self.assertTrue(result["view_date"] and result["tbl_type1"])
        self.assertNotIn("PRIVATE", stdout.getvalue())
        self.assertEqual(opener.addheaders, [("User-Agent", "Mozilla/5.0")])
        response.read.assert_called_once_with(diag.MAX_BODY_BYTES + 1)

    def test_http_error_is_a_probe_result_not_a_tool_error(self) -> None:
        opener = MagicMock()
        opener.open.side_effect = HTTPError(FINAL, 503, "SECRET REASON", {}, None)
        with patch.object(diag, "build_opener", return_value=opener), redirect_stdout(io.StringIO()) as stdout:
            diag.urllib_worker(FINAL)
        result = diag._last_snapshot(stdout.getvalue())
        self.assertEqual(result["http_status"], 503)
        self.assertEqual(result["error_type"], "HTTPError")
        self.assertFalse(result["tool_error"])
        self.assertNotIn("SECRET", stdout.getvalue())

    def test_redirects_are_limited_to_five(self) -> None:
        result = diag.ProbeResult("urllib", SOURCE, SOURCE)
        handler = diag._RedirectRecorder(result)
        request = Request(SOURCE)
        with redirect_stdout(io.StringIO()):
            for index in range(5):
                request = handler.redirect_request(request, None, 302, "Found", {}, f"https://example.com/{index}")
            with self.assertRaises(diag.RedirectLimitExceeded):
                handler.redirect_request(request, None, 302, "Found", {}, "https://example.com/6")
        self.assertEqual(len(result.redirects), 5)

    def test_parent_uses_worker_process_and_recovers_snapshot_on_timeout(self) -> None:
        snapshot = json.dumps(asdict(redirected("urllib", status=None))).encode()
        with patch.object(diag.subprocess, "run", side_effect=subprocess.TimeoutExpired("worker", 35, output=snapshot)) as run:
            result = diag.probe_urllib(SOURCE)
        self.assertEqual(result.error_type, "ProcessTimeout")
        self.assertEqual(result.final_url, FINAL)
        self.assertFalse(result.tool_error)
        self.assertEqual(run.call_args.kwargs["timeout"], 35)
        self.assertIn("--urllib-worker", run.call_args.args[0])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_broken_worker_output_is_tool_error(self) -> None:
        with patch.object(diag.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "bad", "secret")):
            result = diag.probe_urllib(SOURCE)
        self.assertTrue(result.tool_error)
        self.assertEqual(result.error_type, "WorkerProcessError")


class CurlProbeTests(unittest.TestCase):
    def test_command_matches_request_policy_and_parses_only_safe_fields(self) -> None:
        temp_paths = []

        def run(command, **kwargs):
            self.assertEqual(command[:2], ["curl", "--disable"])
            for option, value in (("--max-time", "30"), ("--max-redirs", "5"), ("--user-agent", "Mozilla/5.0")):
                self.assertEqual(command[command.index(option) + 1], value)
            self.assertIn("--http1.1", command)
            self.assertIn("--globoff", command)
            self.assertNotIn("--insecure", command)
            self.assertNotIn("--retry", command)
            self.assertNotIn("--fail", command)
            self.assertEqual(kwargs["timeout"], 35)
            self.assertNotIn("shell", kwargs)
            for option in ("--dump-header", "--output", "--cookie-jar"):
                temp_paths.append(Path(command[command.index(option) + 1]))
            temp_paths[0].write_text(
                f"HTTP/1.1 302 Found\r\nLocation: {FINAL}\r\nSet-Cookie: COOKIE_SECRET\r\n\r\nHTTP/1.1 200 OK\r\n"
            )
            temp_paths[1].write_bytes(BODY + b"BODY_SECRET")
            temp_paths[2].write_text("COOKIE_SECRET")
            metrics = dict.fromkeys(diag.TIMING_FIELDS, 0.5)
            metrics.update(http_code=200, url_effective=FINAL, remote_ip="192.0.2.1", unused="SECRET_METADATA")
            return subprocess.CompletedProcess(command, 0, json.dumps(metrics), "SECRET_STDERR")

        with patch.object(diag.subprocess, "run", side_effect=run):
            result = diag.probe_curl(SOURCE)
        self.assertEqual(result.final_url, FINAL)
        self.assertEqual(result.remote_ip, "192.0.2.1")
        self.assertEqual(result.timings["time_appconnect"], 0.5)
        self.assertTrue(result.view_date and result.tbl_type1)
        self.assertFalse(result.tool_error)
        self.assertEqual(len(result.redirects), 1)
        self.assertNotIn("SECRET", json.dumps(asdict(result)))
        self.assertTrue(all(not path.exists() for path in temp_paths))

    def test_missing_curl_and_malformed_metrics_are_tool_errors(self) -> None:
        for effect in (FileNotFoundError("private path"), subprocess.CompletedProcess([], 0, "bad", "secret")):
            with self.subTest(effect=effect), patch.object(diag.subprocess, "run", side_effect=[effect]):
                self.assertTrue(diag.probe_curl(SOURCE).tool_error)

    def test_network_exit_and_http_error_are_not_tool_errors(self) -> None:
        for exit_code, status in ((52, 0), (60, 0), (0, 503)):
            with self.subTest(exit_code=exit_code, status=status):
                metrics = dict.fromkeys(diag.TIMING_FIELDS, 0)
                metrics.update(http_code=status, url_effective=SOURCE, remote_ip="")
                with patch.object(diag.subprocess, "run", return_value=subprocess.CompletedProcess([], exit_code, json.dumps(metrics), "secret")):
                    result = diag.probe_curl(SOURCE)
                self.assertFalse(result.tool_error)
                self.assertEqual(result.curl_exit_code, exit_code)
                self.assertEqual(result.http_status, status or None)

    def test_timeout_preserves_headers_and_is_not_a_tool_error(self) -> None:
        def timeout(command, **kwargs):
            Path(command[command.index("--dump-header") + 1]).write_text(f"HTTP/1.1 302 Found\nLocation: {FINAL}\n")
            raise subprocess.TimeoutExpired(command, 35)
        with patch.object(diag.subprocess, "run", side_effect=timeout):
            result = diag.probe_curl(SOURCE)
        self.assertEqual(result.final_url, FINAL)
        self.assertEqual(result.error_type, "ProcessTimeout")
        self.assertFalse(result.tool_error)

    def test_proxy_connect_response_is_not_origin_http_success(self) -> None:
        result = diag.ProbeResult("curl", FINAL, FINAL)
        diag._curl_headers(result, "HTTP/1.1 200 Connection established\r\n\r\n")
        self.assertIsNone(result.http_status)

    def test_redirect_status_is_not_reported_as_a_failed_destination_response(self) -> None:
        def run(command, **kwargs):
            Path(command[command.index("--dump-header") + 1]).write_text(f"HTTP/1.1 302 Found\nLocation: {FINAL}\n")
            metrics = dict.fromkeys(diag.TIMING_FIELDS, 0)
            metrics.update(http_code=302, url_effective=FINAL, remote_ip="")
            return subprocess.CompletedProcess(command, 52, json.dumps(metrics), "Empty reply")
        with patch.object(diag.subprocess, "run", side_effect=run):
            result = diag.probe_curl(SOURCE)
        self.assertIsNone(result.http_status)
        self.assertEqual(result.redirects[0]["status"], 302)
        self.assertEqual(result.final_url, FINAL)


class DiagnosticReportTests(unittest.TestCase):
    def _run(self, root, urllib_probe, curl_probe):
        capture = root / "capture.json"
        capture.write_text(json.dumps(manifest()))
        config = SimpleNamespace(enabled_targets=[SimpleNamespace(id="seoulmetro", url=SOURCE)])
        with patch.object(diag, "load_config", return_value=config), patch.object(diag, "_environment", return_value={}), patch.object(
            diag, "probe_urllib", side_effect=urllib_probe
        ), patch.object(diag, "probe_curl", side_effect=curl_probe), patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(root / "step.md")}):
            code = diag.run_diagnostics(capture_result=capture, config_path=root / "config", output_dir=root / "diagnostics")
        return code, json.loads((root / "diagnostics/report.json").read_text())

    def test_four_sequential_probes_use_urllib_redirect_preference(self) -> None:
        calls = []
        other = "https://example.com:8443/other"
        def urllib(url):
            calls.append(("urllib", url))
            return redirected("urllib", error="RemoteDisconnected", status=None)
        def curl(url):
            calls.append(("curl", url))
            return redirected("curl", final=other)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, report = self._run(root, urllib, curl)
            self.assertEqual(calls, [("urllib", SOURCE), ("curl", SOURCE), ("urllib", FINAL), ("curl", FINAL)])
            self.assertEqual(code, 0)
            self.assertEqual([probe["entry"] for probe in report["probes"]], ["original", "original", "direct", "direct"])
            self.assertEqual((root / "step.md").read_text(), (root / "diagnostics/summary.md").read_text())

    def test_curl_destination_is_used_when_urllib_has_none(self) -> None:
        calls = []
        def urllib(url):
            calls.append(("urllib", url))
            return diag.ProbeResult("urllib", url, url, error_type="TimeoutError")
        def curl(url):
            calls.append(("curl", url))
            return redirected("curl")
        with tempfile.TemporaryDirectory() as directory:
            self._run(Path(directory), urllib, curl)
        self.assertEqual(calls[-2:], [("urllib", FINAL), ("curl", FINAL)])

    def test_no_redirect_skips_direct_requests_even_if_curl_is_missing(self) -> None:
        calls = []
        def urllib(url):
            calls.append(("urllib", url))
            return diag.ProbeResult("urllib", url, url, http_status=200, view_date=False, tbl_type1=False)
        def curl(url):
            calls.append(("curl", url))
            raise FileNotFoundError("SECRET")
        with tempfile.TemporaryDirectory() as directory:
            code, report = self._run(Path(directory), urllib, curl)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(report["probes"]), 2)
        self.assertIn("no distinct redirect", report["notes"][0])
        self.assertNotIn("SECRET", json.dumps(report))

    def test_one_tool_error_does_not_stop_later_comparisons(self) -> None:
        calls = []
        def urllib(url):
            calls.append(("urllib", url))
            return redirected("urllib")
        def curl(url):
            calls.append(("curl", url))
            return diag.ProbeResult("curl", url, url, error_type="FileNotFoundError", tool_error=True)
        with tempfile.TemporaryDirectory() as directory:
            code, report = self._run(Path(directory), urllib, curl)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(report["probes"]), 4)

    def test_environment_reports_only_proxy_presence_not_values(self) -> None:
        with patch.dict(os.environ, {"HTTPS_PROXY": "https://user:SECRET@proxy", "SMTP_PASSWORD": "SECRET"}), patch.object(
            diag.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "curl 8.0 OpenSSL/3.0\nfeatures", "")
        ):
            environment = diag._environment()
        self.assertTrue(environment["proxy_env_present"]["HTTPS_PROXY"])
        self.assertNotIn("SECRET", json.dumps(environment))
        self.assertNotIn("SMTP_PASSWORD", environment)

    def test_public_report_masks_url_credentials_and_sensitive_query_values(self) -> None:
        url = "https://user:SECRET@example.com:444/delay?token=SECRET&menuIdx=543#SECRET"
        result = redirected("urllib", final=url)
        result.requested_url = url
        public = diag._public_probe(result)
        self.assertNotIn("SECRET", json.dumps(public))
        self.assertIn("menuIdx=543", public["final_url"])
        self.assertIn("token=REDACTED", public["final_url"])
        self.assertEqual(result.final_url, url)

    def test_http_200_without_page_markers_is_visible_as_unexpected_content(self) -> None:
        result = diag.ProbeResult("urllib", FINAL, FINAL, http_status=200)
        diag._inspect_body(result, b"<html>Maintenance</html>")
        self.assertFalse(result.view_date)
        self.assertFalse(result.tbl_type1)
        self.assertEqual(diag._public_probe(result)["outcome"], "unexpected_page")


if __name__ == "__main__":
    unittest.main()
