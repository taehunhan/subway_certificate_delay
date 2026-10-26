from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from zipfile import ZipFile

from subway_delay.config import AppConfig, TargetConfig
from subway_delay.runner import TargetResult, _build_email_body, execute_run


class FakeCaptureService:
    def __init__(
        self,
        failure_budget: dict[str, int] | None = None,
        failure_messages: dict[str, list[str]] | None = None,
    ) -> None:
        self.failure_budget = failure_budget or {}
        self.failure_messages = failure_messages or {}
        self.calls: list[tuple[str, Path]] = []

    async def capture(self, *, target: TargetConfig, capture_date: date, destination: Path) -> None:
        self.calls.append((target.id, destination))
        messages = self.failure_messages.get(target.id)
        if messages:
            raise RuntimeError(messages.pop(0))
        remaining_failures = self.failure_budget.get(target.id, 0)
        if remaining_failures > 0:
            self.failure_budget[target.id] = remaining_failures - 1
            raise RuntimeError(f"Planned failure for {target.id}")

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(f"{target.id}:{capture_date.isoformat()}".encode("utf-8"))


@dataclass
class FakeMailer:
    sent_messages: list[dict[str, object]] = field(default_factory=list)
    should_fail: bool = False

    def send(
        self,
        *,
        recipients: list[str],
        subject: str,
        body: str,
        attachment_path: Path | None = None,
    ) -> None:
        if self.should_fail:
            raise RuntimeError("SMTP unavailable")
        self.sent_messages.append(
            {
                "recipients": recipients,
                "subject": subject,
                "body": body,
                "attachment_path": attachment_path,
            }
        )


def build_test_config(base_dir: Path) -> AppConfig:
    return AppConfig(
        timezone="Asia/Seoul",
        recipients=["recipient@example.com"],
        output_dir=base_dir / "output",
        targets=[
            TargetConfig(
                id="korail",
                name="코레일",
                url="https://example.com/korail",
                enabled=True,
                selection_mode="korail_select",
                capture_selector="#main",
                wait_selector="#main",
                submit_selector="button",
            ),
            TargetConfig(
                id="metro9",
                name="서울시메트로9호선",
                url="https://example.com/metro9",
                enabled=True,
                selection_mode="metro9_tab",
                capture_selector="#main",
                wait_selector="#main",
            ),
        ],
    )


class ExecuteRunTests(unittest.TestCase):
    def test_dry_run_creates_pngs_zip_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_test_config(Path(temp_dir))
            result = asyncio.run(
                execute_run(
                    config=config,
                    capture_date=date(2026, 5, 12),
                    send_email=False,
                    capture_service=FakeCaptureService(),
                )
            )

            self.assertTrue(result.zip_path is not None)
            self.assertTrue(result.log_path.exists())
            self.assertTrue((result.output_dir / "korail.png").exists())
            self.assertTrue((result.output_dir / "metro9.png").exists())
            self.assertTrue(result.overall_success)

            with ZipFile(result.zip_path) as archive:
                self.assertEqual(sorted(archive.namelist()), ["korail.png", "metro9.png"])

    def test_partial_failure_sends_email_with_successful_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_test_config(Path(temp_dir))
            mailer = FakeMailer()
            result = asyncio.run(
                execute_run(
                    config=config,
                    capture_date=date(2026, 5, 12),
                    send_email=True,
                    capture_service=FakeCaptureService(failure_budget={"metro9": 2}),
                    mailer=mailer,
                )
            )

            self.assertFalse(result.overall_success)
            self.assertEqual(len(mailer.sent_messages), 1)
            self.assertTrue(result.zip_path is not None)
            self.assertIn("Failed targets: 서울시메트로9호선", mailer.sent_messages[0]["body"])
            self.assertIn(
                "Failure details:\n- 서울시메트로9호선 (metro9): Planned failure for metro9",
                mailer.sent_messages[0]["body"],
            )

            with ZipFile(result.zip_path) as archive:
                self.assertEqual(archive.namelist(), ["korail.png"])

    def test_full_errors_are_logged_but_email_only_summarizes_final_failure(self) -> None:
        first_error = "Initial navigation failed for metro9: HTTP 404 Not Found"
        final_error = (
            "Initial navigation failed for metro9: HTTP 503 Service Unavailable; "
            "requested_url=https://example.com/metro9; response_url=https://example.com/maintenance\n"
            "  Additional details:\t" + "x" * 600
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_test_config(Path(temp_dir))
            mailer = FakeMailer()
            service = FakeCaptureService(failure_messages={"metro9": [first_error, final_error]})

            result = asyncio.run(
                execute_run(
                    config=config,
                    capture_date=date(2026, 10, 23),
                    send_email=True,
                    capture_service=service,
                    mailer=mailer,
                )
            )

            failure = result.results[1]
            self.assertEqual(failure.error, final_error)
            self.assertEqual(failure.attempts, 2)
            self.assertEqual([target_id for target_id, _ in service.calls], ["korail", "metro9", "metro9"])
            log = result.log_path.read_text(encoding="utf-8")
            self.assertIn(first_error, log)
            self.assertIn(final_error, log)
            body = mailer.sent_messages[0]["body"]
            summary = " ".join(final_error.split())[:497] + "..."
            self.assertIn(f"- 서울시메트로9호선 (metro9): {summary}", body)
            self.assertNotIn(first_error, body)
            self.assertNotIn(final_error, body)
            self.assertFalse((result.output_dir / "metro9.png").exists())

    def test_successful_and_recovered_targets_have_no_failure_details(self) -> None:
        for failures in (0, 1):
            with self.subTest(failures=failures), tempfile.TemporaryDirectory() as temp_dir:
                config = build_test_config(Path(temp_dir))
                mailer = FakeMailer()
                result = asyncio.run(
                    execute_run(
                        config=config,
                        capture_date=date(2026, 5, 12),
                        send_email=True,
                        capture_service=FakeCaptureService(failure_budget={"metro9": failures}),
                        mailer=mailer,
                    )
                )

                self.assertTrue(result.overall_success)
                self.assertEqual(result.results[1].attempts, failures + 1)
                self.assertIsNone(result.results[1].error)
                body = mailer.sent_messages[0]["body"]
                self.assertIn("Failed targets: -", body)
                self.assertNotIn("Failure details:", body)
                self.assertNotIn("Planned failure", body)

    def test_all_failures_send_details_without_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_test_config(Path(temp_dir))
            mailer = FakeMailer()
            service = FakeCaptureService(failure_budget={"korail": 2, "metro9": 2})
            result = asyncio.run(
                execute_run(
                    config=config,
                    capture_date=date(2026, 5, 12),
                    send_email=True,
                    capture_service=service,
                    mailer=mailer,
                )
            )

            self.assertFalse(result.overall_success)
            self.assertIsNone(result.zip_path)
            self.assertEqual(len(service.calls), 4)
            self.assertEqual(len(mailer.sent_messages), 1)
            message = mailer.sent_messages[0]
            self.assertIsNone(message["attachment_path"])
            self.assertIn("Failure details:", message["body"])
            for target_result in result.results:
                self.assertEqual(target_result.attempts, 2)
                self.assertIn(
                    f"- {target_result.name} ({target_result.target_id}): {target_result.error}",
                    message["body"],
                )

    def test_mail_failure_marks_run_unsuccessful(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_test_config(Path(temp_dir))
            result = asyncio.run(
                execute_run(
                    config=config,
                    capture_date=date(2026, 5, 12),
                    send_email=True,
                    capture_service=FakeCaptureService(),
                    mailer=FakeMailer(should_fail=True),
                )
            )

            self.assertFalse(result.overall_success)
            self.assertEqual(result.email_error, "SMTP unavailable")

    def test_custom_filename_template_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = AppConfig(
                timezone="Asia/Seoul",
                recipients=["recipient@example.com"],
                output_dir=Path(temp_dir) / "output",
                targets=[
                    TargetConfig(
                        id="gtx-a-wunjeong-seoul",
                        name="GTX-A 운정중앙역-서울역",
                        url="https://example.com/gtx",
                        enabled=True,
                        selection_mode="gtx_fetch",
                        capture_selector="#main",
                        wait_selector="#main",
                        selection_value="L08",
                    ),
                    TargetConfig(
                        id="dxline",
                        name="신분당선",
                        url="https://example.com/dxline",
                        enabled=True,
                        selection_mode="dxline_static",
                        capture_selector="#main",
                        wait_selector="#main",
                        filename_template="dxline-{date}.png",
                    ),
                ],
            )

            result = asyncio.run(
                execute_run(
                    config=config,
                    capture_date=date(2026, 5, 12),
                    send_email=False,
                    capture_service=FakeCaptureService(),
                )
            )

            self.assertTrue((result.output_dir / "gtx-a-wunjeong-seoul.png").exists())
            self.assertTrue((result.output_dir / "dxline-2026-05-13.png").exists())

            with ZipFile(result.zip_path) as archive:
                self.assertEqual(
                    sorted(archive.namelist()),
                    ["dxline-2026-05-13.png", "gtx-a-wunjeong-seoul.png"],
                )


class EmailBodyTests(unittest.TestCase):
    def test_error_summary_normalizes_whitespace_and_limits_length(self) -> None:
        cases = [
            (None, "Unknown capture error"),
            ("", "Unknown capture error"),
            (" \n\t", "Unknown capture error"),
            ("  HTTP 503\n\r  Service\tUnavailable  ", "HTTP 503 Service Unavailable"),
            ("x" * 500, "x" * 500),
            ("x" * 501, "x" * 497 + "..."),
            (" \n" + "x" * 500 + "\t ", "x" * 500),
        ]
        for error, expected in cases:
            with self.subTest(error=error):
                result = TargetResult(
                    target_id="ui-line", name="Ui Line", success=False, attempts=2, error=error
                )
                body = _build_email_body(
                    capture_date=date(2026, 10, 23),
                    timezone_name="Asia/Seoul",
                    results=[result],
                    zip_path=None,
                )

                self.assertEqual(body.split("Failure details:\n")[1], f"- Ui Line (ui-line): {expected}")
                self.assertEqual(result.error, error)


if __name__ == "__main__":
    unittest.main()
