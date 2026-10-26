from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from zipfile import ZipFile

from subway_delay.config import AppConfig, TargetConfig
from subway_delay.runner import RunResult, execute_run


class FakeCaptureService:
    def __init__(self, failure_budget: dict[str, int] | None = None) -> None:
        self.failure_budget = failure_budget or {}
        self.calls: list[tuple[str, Path]] = []

    async def capture(self, *, target: TargetConfig, capture_date: date, destination: Path) -> None:
        self.calls.append((target.id, destination))
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

            with ZipFile(result.zip_path) as archive:
                self.assertEqual(archive.namelist(), ["korail.png"])

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


if __name__ == "__main__":
    unittest.main()
