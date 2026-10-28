from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


MAX_HTML_BYTES = 5_000_000


def valid_direct_url(target_url: str, direct_url: str) -> bool:
    try:
        target, direct = urlsplit(target_url), urlsplit(direct_url)
        return (
            direct.scheme == "https" and bool(direct.hostname)
            and direct.hostname == target.hostname and direct.path == target.path
            and direct.username is None and direct.password is None and not direct.fragment
            and (direct.port is None or 0 < direct.port < 65536)
            and not any(character.isspace() or ord(character) < 32 for character in direct_url)
        )
    except ValueError:
        return False


def _safe_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parts.port is not None:
            host += f":{parts.port}"
        query = urlencode([
            (key, value if key == "menuIdx" else "REDACTED")
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ])
        return urlunsplit((parts.scheme, host, parts.path, query, ""))
    except ValueError:
        return "[invalid URL]"


@dataclass(frozen=True)
class CurlDocument:
    html: str
    final_url: str


class CurlSession:
    """One capture attempt's cookie session; temporary responses never become artifacts."""

    def __init__(self, *, target_url: str, timeout_seconds: float, logger: logging.Logger) -> None:
        self.target_url = target_url
        self.timeout_seconds = timeout_seconds
        self.logger = logger
        self._temporary = None
        self._directory: Path | None = None
        self._request_count = 0

    def __enter__(self) -> "CurlSession":
        self._temporary = tempfile.TemporaryDirectory(prefix="subway-seoulmetro-curl-")
        self._directory = Path(self._temporary.name)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._temporary.cleanup()
        self._directory = None

    def fetch(self, url: str, *, form_data: bytes | None = None) -> CurlDocument:
        if self._directory is None:
            raise RuntimeError("CurlSession must be used as a context manager.")
        if not valid_direct_url(self.target_url, url):
            raise ValueError("Seoul Metro curl URL must be HTTPS on the configured hostname and path.")
        self._request_count += 1
        cookie_path = self._directory / "cookies"
        body_path = self._directory / f"response-{self._request_count}.html"
        method = "POST" if form_data is not None else "GET"
        context = f"Seoul Metro curl {method}; requested_url={_safe_url(url)}"
        command = [
            "curl", "--disable", "--silent", "--show-error", "--globoff", "--http1.1",
            "--location", "--max-redirs", "5", "--proto", "=https", "--proto-redir", "=https",
            "--max-time", str(self.timeout_seconds), "--connect-timeout", str(self.timeout_seconds),
            "--max-filesize", str(MAX_HTML_BYTES), "--user-agent", "Mozilla/5.0",
            "--header", "Accept:", "--header", "Accept-Encoding: identity", "--header", "Connection: close",
            "--cookie", str(cookie_path), "--cookie-jar", str(cookie_path),
            "--output", str(body_path), "--write-out", "%{json}", "--url", url,
        ]
        if form_data is not None:
            command.extend(["--header", "Content-Type: application/x-www-form-urlencoded", "--data-binary", "@-"])
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command, input=form_data, capture_output=True, timeout=self.timeout_seconds + 5,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{context}; process timeout after {self.timeout_seconds + 5}s") from None
        except OSError as exc:
            raise RuntimeError(f"{context}; {type(exc).__name__}; ensure curl is installed") from None

        if completed.returncode != 0:
            raise RuntimeError(f"{context}; curl exit code {completed.returncode}")
        try:
            metrics = json.loads(completed.stdout)
            status = int(metrics["http_code"])
            final_url = metrics["url_effective"]
            content_type = metrics.get("content_type") or ""
            if not isinstance(final_url, str) or not isinstance(content_type, str):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise RuntimeError(f"{context}; invalid curl metadata (curl 7.70+ required)") from None

        context += f"; response_url={_safe_url(final_url)}; HTTP {status}"
        self.logger.info("%s; elapsed_seconds=%.3f", context, time.monotonic() - started)
        if not 200 <= status < 300:
            raise RuntimeError(context)
        if not valid_direct_url(self.target_url, final_url):
            raise RuntimeError(f"{context}; unexpected final destination")
        try:
            with body_path.open("rb") as handle:
                body = handle.read(MAX_HTML_BYTES + 1)
            if len(body) > MAX_HTML_BYTES:
                raise ValueError()
            headers = Message()
            headers["Content-Type"] = content_type
            html = body.decode(headers.get_content_charset() or "utf-8", errors="replace")
        except (OSError, LookupError, ValueError) as exc:
            raise RuntimeError(f"{context}; invalid or unreadable response ({type(exc).__name__})") from None
        return CurlDocument(html, final_url)
