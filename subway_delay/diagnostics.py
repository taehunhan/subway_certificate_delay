from __future__ import annotations

import argparse
import html
import http.client
import json
import os
import platform
import ssl
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from http.cookiejar import CookieJar
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

from subway_delay.config import load_config


REQUEST_TIMEOUT = 30
PROCESS_TIMEOUT = 35
MAX_REDIRECTS = 5
MAX_BODY_BYTES = 2_000_000
USER_AGENT = "Mozilla/5.0"
PROXY_VARIABLES = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)
TIMING_FIELDS = ("time_namelookup", "time_connect", "time_appconnect", "time_starttransfer", "time_total")
CURL_TOOL_ERRORS = {1, 2, 3, 4, 23, 26, 27, 43, 48}


@dataclass
class ProbeResult:
    client: str
    requested_url: str
    final_url: str
    entry: str = "original"
    redirects: list[dict] = field(default_factory=list)
    http_status: int | None = None
    elapsed_seconds: float | None = None
    error_type: str | None = None
    curl_exit_code: int | None = None
    tool_error: bool = False
    view_date: bool | None = None
    tbl_type1: bool | None = None
    body_truncated: bool = False
    remote_ip: str | None = None
    timings: dict[str, float | None] = field(default_factory=lambda: dict.fromkeys(TIMING_FIELDS))


class _MarkerParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.view_date = False
        self.tbl_type1 = False

    def handle_starttag(self, tag, attrs) -> None:
        attributes = dict(attrs)
        if tag == "select" and (
            attributes.get("id") == "view_date" or attributes.get("name") == "view_date"
        ):
            self.view_date = True
        if "tbl-type1" in (attributes.get("class") or "").split():
            self.tbl_type1 = True


def _inspect_body(result: ProbeResult, body: bytes, charset: str = "utf-8") -> None:
    result.body_truncated = len(body) > MAX_BODY_BYTES
    try:
        text = body[:MAX_BODY_BYTES].decode(charset, errors="replace")
    except LookupError:
        text = body[:MAX_BODY_BYTES].decode("utf-8", errors="replace")
    parser = _MarkerParser()
    parser.feed(text)
    parser.close()
    result.view_date, result.tbl_type1 = parser.view_date, parser.tbl_type1


def _valid_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (
            parts.scheme in {"http", "https"} and bool(parts.hostname)
            and parts.username is None and parts.password is None
            and (parts.port is None or 0 < parts.port < 65536)
        )
    except ValueError:
        return False


class RedirectLimitExceeded(Exception):
    pass


def _emit_result(result: ProbeResult) -> None:
    # The parent retains the latest snapshot if a worker exceeds its time limit.
    print(json.dumps(asdict(result)), flush=True)


class _RedirectRecorder(HTTPRedirectHandler):
    def __init__(self, result: ProbeResult) -> None:
        self.result = result

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.result.http_status = code
        if len(self.result.redirects) >= MAX_REDIRECTS:
            raise RedirectLimitExceeded()
        if not _valid_url(newurl):
            raise HTTPError(req.full_url, code, "Unsupported redirect URL", headers, fp)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            self.result.redirects.append({"status": code, "from_url": req.full_url, "to_url": redirected.full_url})
            self.result.final_url = redirected.full_url
            self.result.http_status = None
            _emit_result(self.result)
        return redirected


def urllib_worker(url: str) -> None:
    result = ProbeResult("urllib", url, url)
    started = time.monotonic()
    _emit_result(result)
    try:
        opener = build_opener(HTTPCookieProcessor(CookieJar()), _RedirectRecorder(result))
        opener.addheaders = [("User-Agent", USER_AGENT)]
        with opener.open(Request(url, method="GET"), timeout=REQUEST_TIMEOUT) as response:
            result.final_url = response.geturl()
            result.http_status = response.status
            _emit_result(result)
            _inspect_body(result, response.read(MAX_BODY_BYTES + 1), response.headers.get_content_charset() or "utf-8")
    except HTTPError as exc:
        result.http_status, result.final_url = exc.code, exc.geturl()
        result.error_type = type(exc).__name__
        exc.close()
    except URLError as exc:
        result.error_type = f"URLError/{type(exc.reason).__name__}"
    except (OSError, http.client.HTTPException, RedirectLimitExceeded) as exc:
        result.error_type = type(exc).__name__
    except Exception as exc:
        result.error_type, result.tool_error = type(exc).__name__, True
    result.elapsed_seconds = time.monotonic() - started
    _emit_result(result)


def _last_snapshot(output: str | bytes | None) -> dict | None:
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    snapshot = None
    for line in (output or "").splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict) and value.get("client") == "urllib":
                snapshot = value
        except json.JSONDecodeError:
            continue
    return snapshot


def probe_urllib(url: str) -> ProbeResult:
    result = ProbeResult("urllib", url, url)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "subway_delay.diagnostics", "--urllib-worker", url],
            capture_output=True, text=True, timeout=PROCESS_TIMEOUT,
        )
        snapshot = _last_snapshot(completed.stdout)
        if snapshot:
            result = ProbeResult(**snapshot)
        if not snapshot or completed.returncode != 0:
            result.error_type, result.tool_error = "WorkerProcessError", True
    except subprocess.TimeoutExpired as exc:
        snapshot = _last_snapshot(exc.stdout)
        if snapshot:
            result = ProbeResult(**snapshot)
        result.error_type = "ProcessTimeout"
    except (OSError, TypeError, ValueError) as exc:
        result.error_type, result.tool_error = type(exc).__name__, True
    result.elapsed_seconds = time.monotonic() - started
    return result


def _curl_command(url: str, directory: Path) -> list[str]:
    return [
        "curl", "--disable", "--silent", "--show-error", "--globoff", "--http1.1", "--location",
        "--max-redirs", str(MAX_REDIRECTS), "--max-time", str(REQUEST_TIMEOUT),
        "--connect-timeout", str(REQUEST_TIMEOUT), "--proto", "=http,https", "--proto-redir", "=http,https",
        "--user-agent", USER_AGENT, "--header", "Accept:", "--header", "Accept-Encoding: identity",
        "--header", "Connection: close", "--cookie-jar", str(directory / "cookies"),
        "--dump-header", str(directory / "headers"), "--output", str(directory / "body"),
        "--write-out", "%{json}", "--url", url,
    ]


def _curl_headers(result: ProbeResult, text: str) -> None:
    current_url = result.requested_url
    status = None
    for line in text.splitlines():
        if line.startswith("HTTP/"):
            if "connection established" in line.lower():
                continue
            pieces = line.split()
            status = int(pieces[1]) if len(pieces) > 1 and pieces[1].isdigit() else None
            result.http_status = status
        elif line.lower().startswith("location:") and status in {301, 302, 303, 307, 308}:
            new_url = urljoin(current_url, line.split(":", 1)[1].strip())
            if len(result.redirects) < MAX_REDIRECTS and _valid_url(new_url):
                result.redirects.append({"status": status, "from_url": current_url, "to_url": new_url})
                current_url = new_url
                result.final_url = new_url
                result.http_status = None


def probe_curl(url: str) -> ProbeResult:
    result = ProbeResult("curl", url, url)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="subway-diagnostic-") as temp_dir:
        directory = Path(temp_dir)
        try:
            completed = subprocess.run(
                _curl_command(url, directory), capture_output=True, text=True, timeout=PROCESS_TIMEOUT,
            )
            result.curl_exit_code = completed.returncode
            if completed.returncode:
                result.error_type = f"CurlExit{completed.returncode}"
                result.tool_error = completed.returncode in CURL_TOOL_ERRORS
            metrics = json.loads(completed.stdout)
            if not isinstance(metrics, dict):
                raise ValueError("Invalid curl metrics")
            result.final_url = metrics.get("url_effective") or url
            result.remote_ip = metrics.get("remote_ip") or None
            result.http_status = int(metrics["http_code"]) or None
            result.timings = {key: float(metrics[key]) for key in TIMING_FIELDS}
        except subprocess.TimeoutExpired:
            result.error_type = "ProcessTimeout"
        except (OSError, ValueError, TypeError, KeyError) as exc:
            result.error_type, result.tool_error = type(exc).__name__, True
        # Only allowlisted metadata is retained; temporary response files are deleted.
        headers_path = directory / "headers"
        if headers_path.exists():
            final_url = result.final_url
            _curl_headers(result, headers_path.read_text(encoding="utf-8", errors="replace"))
            if final_url != url:
                result.final_url = final_url
        body_path = directory / "body"
        if body_path.exists():
            with body_path.open("rb") as body:
                _inspect_body(result, body.read(MAX_BODY_BYTES + 1))
    result.elapsed_seconds = time.monotonic() - started
    return result


def should_diagnose(path: Path) -> tuple[bool, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("capture_date"), str):
            raise ValueError()
        if date.fromisoformat(data["capture_date"]).isoformat() != data["capture_date"]:
            raise ValueError()
        targets = data["targets"]
        if not isinstance(targets, list):
            raise ValueError()
        seen = set()
        failed = False
        for target in targets:
            if (
                not isinstance(target, dict) or not isinstance(target.get("id"), str)
                or not target["id"] or target["id"] in seen or type(target.get("success")) is not bool
            ):
                raise ValueError()
            seen.add(target["id"])
            failed |= target["id"] == "seoulmetro" and target["success"] is False
        return failed, "Seoul Metro failed" if failed else "Seoul Metro did not fail in this capture run"
    except FileNotFoundError:
        return False, "Capture result file is missing"
    except (OSError, ValueError, KeyError, TypeError):
        return False, "Capture result file is invalid or unreadable"


def _environment() -> dict:
    result = {
        "python_version": platform.python_version(), "openssl_version": ssl.OPENSSL_VERSION,
        "curl_version": None,
        "proxy_env_present": {key: bool(os.environ.get(key)) for key in PROXY_VARIABLES},
    }
    try:
        version = subprocess.run(["curl", "--disable", "--version"], capture_output=True, text=True, timeout=5)
        if version.returncode == 0 and version.stdout.splitlines():
            result["curl_version"] = version.stdout.splitlines()[0]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return result


def _direct_url(probes: list[ProbeResult]) -> str | None:
    for probe in probes:
        if probe.redirects and probe.final_url != probe.requested_url and _valid_url(probe.final_url):
            return probe.final_url
    return None


def _safe_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parts.port is not None:
            host += f":{parts.port}"
        query = urlencode([(key, value if key == "menuIdx" else "REDACTED") for key, value in parse_qsl(parts.query, keep_blank_values=True)])
        return urlunsplit((parts.scheme, host, parts.path, query, ""))
    except ValueError:
        return "[invalid URL]"


def _public_probe(probe: ProbeResult) -> dict:
    result = asdict(probe)
    if probe.tool_error:
        result["outcome"] = "tool_error"
    elif probe.http_status is not None and probe.http_status >= 400:
        result["outcome"] = "http_error"
    elif probe.error_type:
        result["outcome"] = "request_error"
    elif probe.http_status is not None and 200 <= probe.http_status < 300:
        result["outcome"] = "expected_page" if probe.view_date and probe.tbl_type1 else "unexpected_page"
    else:
        result["outcome"] = "unconfirmed"
    for key in ("requested_url", "final_url"):
        result[key] = _safe_url(result[key])
    for redirect in result["redirects"]:
        for key in ("from_url", "to_url"):
            redirect[key] = _safe_url(redirect[key])
    return result


def _cell(value) -> str:
    text = "-" if value is None else " ".join(str(value).split())
    return "<code>" + html.escape(text).replace("|", "&#124;") + "</code>"


def _summary(report: dict) -> str:
    lines = ["## Seoul Metro Connectivity Diagnostics", "", report["reason"], ""]
    if report["probes"]:
        lines.extend([
            "| Client | Entry | Result | HTTP | Seconds | view_date / tbl-type1 | Error | Final URL |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ])
        for probe in report["probes"]:
            values = [
                probe["client"], probe["entry"], probe["outcome"], probe["http_status"],
                round(probe["elapsed_seconds"], 3) if probe["elapsed_seconds"] is not None else None,
                f'{probe["view_date"]} / {probe["tbl_type1"]}', probe["error_type"], probe["final_url"],
            ]
            lines.append("| " + " | ".join(_cell(value) for value in values) + " |")
    if report["notes"]:
        lines.extend(["", *[f"- {note}" for note in report["notes"]]])
    lines.append("")
    lines.append("These GET probes are not capture retries. Success now does not disprove the earlier failure; differences are evidence to investigate, not proof of blocking.")
    return "\n".join(lines) + "\n"


def run_diagnostics(*, capture_result: Path, config_path: Path, output_dir: Path) -> int:
    enabled, reason = should_diagnose(capture_result)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(), "reason": reason,
        "skipped": not enabled, "environment": {}, "probes": [], "notes": [],
    }
    tool_error = False
    if enabled:
        report["environment"] = _environment()
        try:
            config = load_config(config_path)
            target = next((item for item in config.enabled_targets if item.id == "seoulmetro"), None)
            if target is None:
                report.update(skipped=True, reason="No enabled Seoul Metro target in the configuration")
            elif not _valid_url(target.url):
                raise ValueError("Unsupported target URL")
            else:
                probes = []
                for client in (probe_urllib, probe_curl):
                    probes.append(_run_probe(client, target.url, "original"))
                direct_url = _direct_url(probes)
                if direct_url:
                    for client in (probe_urllib, probe_curl):
                        probes.append(_run_probe(client, direct_url, "direct"))
                else:
                    report["notes"].append("Direct URL checks skipped: no distinct redirect destination observed.")
                report["probes"] = [_public_probe(probe) for probe in probes]
                tool_error = any(probe.tool_error for probe in probes)
                if tool_error:
                    report["notes"].append("Some diagnostic data could not be collected; other probe results are retained.")
        except Exception as exc:
            tool_error = True
            report["notes"].append(f"Diagnostic setup failed: {type(exc).__name__}")
    report["tool_error"] = tool_error
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    summary = _summary(report)
    (output_dir / "summary.md").write_text(summary, encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as handle:
            handle.write(summary)
    print(summary)
    return int(tool_error)


def _run_probe(client, url: str, entry: str) -> ProbeResult:
    try:
        probe = client(url)
    except Exception as exc:
        probe = ProbeResult(
            "urllib" if client is probe_urllib else "curl", url, url,
            error_type=type(exc).__name__, tool_error=True,
        )
    probe.entry = entry
    return probe


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare Seoul Metro HTTP clients after a failed capture.")
    parser.add_argument("--capture-result", type=Path)
    parser.add_argument("--config", type=Path, default=Path("config/targets.yaml"))
    parser.add_argument("--output-dir", type=Path, default=Path("output/diagnostics"))
    parser.add_argument("--urllib-worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.urllib_worker is not None:
        if not _valid_url(args.urllib_worker):
            parser.error("Unsupported worker URL")
        urllib_worker(args.urllib_worker)
        return
    if args.capture_result is None:
        parser.error("--capture-result is required")
    raise SystemExit(run_diagnostics(
        capture_result=args.capture_result, config_path=args.config, output_dir=args.output_dir,
    ))


if __name__ == "__main__":
    main()
