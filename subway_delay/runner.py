from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from subway_delay.archive import build_zip
from subway_delay.capture import CaptureServiceProtocol, PlaywrightCaptureService
from subway_delay.config import AppConfig, TargetConfig
from subway_delay.emailer import MailerProtocol, SmtpMailer


@dataclass(frozen=True)
class TargetResult:
    target_id: str
    name: str
    success: bool
    attempts: int
    screenshot_path: Path | None = None
    error: str | None = None


@dataclass(frozen=True)
class RunResult:
    capture_date: date
    output_dir: Path
    log_path: Path
    results: list[TargetResult]
    zip_path: Path | None
    email_sent: bool
    email_error: str | None

    @property
    def overall_success(self) -> bool:
        return (
            bool(self.results)
            and self.email_error is None
            and all(result.success for result in self.results)
        )


class ArchiveBuilderProtocol(Protocol):
    def __call__(self, file_paths: list[Path], destination: Path) -> Path: ...


async def execute_run(
    *,
    config: AppConfig,
    capture_date: date,
    send_email: bool,
    capture_service: CaptureServiceProtocol | None = None,
    archive_builder: ArchiveBuilderProtocol = build_zip,
    mailer: MailerProtocol | None = None,
) -> RunResult:
    run_dir = config.output_dir / capture_date.isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)
    logger, log_path = _setup_logger(run_dir / "run.log")

    logger.info("Run started for %s", capture_date.isoformat())
    results = await _capture_targets(
        logger=logger,
        capture_service=capture_service or PlaywrightCaptureService(),
        targets=config.enabled_targets,
        capture_date=capture_date,
        run_dir=run_dir,
    )

    success_paths = [result.screenshot_path for result in results if result.success and result.screenshot_path]
    zip_path: Path | None = None
    if success_paths:
        zip_path = archive_builder(
            success_paths,
            run_dir / f"subway-delay-{capture_date.isoformat()}.zip",
        )
        logger.info("Created zip archive at %s", zip_path)
    else:
        logger.warning("No screenshots were created; zip archive skipped.")

    email_sent = False
    email_error: str | None = None
    if send_email:
        try:
            selected_mailer = mailer or SmtpMailer.from_environment()
            selected_mailer.send(
                recipients=config.recipients,
                subject=f"[Subway Delay] {capture_date.isoformat()} capture",
                body=_build_email_body(
                    capture_date=capture_date,
                    timezone_name=config.timezone,
                    results=results,
                    zip_path=zip_path,
                ),
                attachment_path=zip_path,
            )
            email_sent = True
            logger.info("Email sent to %s", ", ".join(config.recipients))
        except Exception as exc:
            email_error = str(exc)
            logger.exception("Failed to send email.")
    else:
        logger.info("Email sending skipped by --no-email.")

    if not results:
        logger.error("No enabled targets configured.")

    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)

    return RunResult(
        capture_date=capture_date,
        output_dir=run_dir,
        log_path=log_path,
        results=results,
        zip_path=zip_path,
        email_sent=email_sent,
        email_error=email_error,
    )


async def _capture_targets(
    *,
    logger: logging.Logger,
    capture_service: CaptureServiceProtocol,
    targets: list[TargetConfig],
    capture_date: date,
    run_dir: Path,
) -> list[TargetResult]:
    results: list[TargetResult] = []
    for target in targets:
        screenshot_path = run_dir / target.screenshot_filename(capture_date)
        last_error: str | None = None
        for attempt in range(1, 3):
            try:
                logger.info("Capturing %s (attempt %s)", target.id, attempt)
                await capture_service.capture(
                    target=target,
                    capture_date=capture_date,
                    destination=screenshot_path,
                )
                results.append(
                    TargetResult(
                        target_id=target.id,
                        name=target.name,
                        success=True,
                        attempts=attempt,
                        screenshot_path=screenshot_path,
                    )
                )
                break
            except Exception as exc:
                last_error = str(exc)
                logger.exception("Capture failed for %s on attempt %s", target.id, attempt)
                if screenshot_path.exists():
                    screenshot_path.unlink()
        else:
            results.append(
                TargetResult(
                    target_id=target.id,
                    name=target.name,
                    success=False,
                    attempts=2,
                    error=last_error or "Unknown capture error",
                )
            )
    return results


def _build_email_body(
    *,
    capture_date: date,
    timezone_name: str,
    results: list[TargetResult],
    zip_path: Path | None,
) -> str:
    now_local = datetime.now(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M:%S %Z")
    success_targets = ", ".join(result.name for result in results if result.success) or "-"
    failed_targets = ", ".join(result.name for result in results if not result.success) or "-"
    zip_name = zip_path.name if zip_path is not None else "-"
    return "\n".join(
        [
            f"Run time: {now_local}",
            f"Capture date: {capture_date.isoformat()}",
            f"Successful targets: {success_targets}",
            f"Failed targets: {failed_targets}",
            f"Zip file: {zip_name}",
        ]
    )


def _setup_logger(log_path: Path) -> tuple[logging.Logger, Path]:
    logger = logging.getLogger(f"subway_delay.{log_path.parent.name}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger, log_path
