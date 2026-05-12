from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol


class MailerProtocol(Protocol):
    def send(
        self,
        *,
        recipients: list[str],
        subject: str,
        body: str,
        attachment_path: Path | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    username: str
    password: str
    sender: str


class SmtpMailer:
    def __init__(self, settings: SmtpSettings) -> None:
        self.settings = settings

    @classmethod
    def from_environment(cls) -> "SmtpMailer":
        missing = [
            name
            for name in (
                "SMTP_HOST",
                "SMTP_PORT",
                "SMTP_USERNAME",
                "SMTP_PASSWORD",
                "SMTP_FROM",
            )
            if not os.getenv(name)
        ]
        if missing:
            raise ValueError(
                "Missing SMTP environment variables: " + ", ".join(sorted(missing))
            )

        settings = SmtpSettings(
            host=os.environ["SMTP_HOST"],
            port=int(os.environ["SMTP_PORT"]),
            username=os.environ["SMTP_USERNAME"],
            password=os.environ["SMTP_PASSWORD"],
            sender=os.environ["SMTP_FROM"],
        )
        return cls(settings)

    def send(
        self,
        *,
        recipients: list[str],
        subject: str,
        body: str,
        attachment_path: Path | None = None,
    ) -> None:
        message = EmailMessage()
        message["From"] = self.settings.sender
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message.set_content(body)

        if attachment_path is not None:
            with attachment_path.open("rb") as handle:
                message.add_attachment(
                    handle.read(),
                    maintype="application",
                    subtype="zip",
                    filename=attachment_path.name,
                )

        with smtplib.SMTP(self.settings.host, self.settings.port, timeout=30) as server:
            server.starttls()
            server.login(self.settings.username, self.settings.password)
            server.send_message(message)
