# Subway Delay Certificate Capture

Python program that captures the summary delay certificate pages for selected subway operators, zips the PNG files, and sends them through SMTP.

## Local run

1. Install dependencies.
   `python3 -m pip install -r requirements.txt`
2. Install the Playwright browser.
   `python3 -m playwright install --with-deps chromium`
3. Update [config/targets.yaml](/Users/taehunhan/Python/subway_certificate_delay/config/targets.yaml) with your recipient list.
   Optional per-target load policy fields:
   `initial_wait_until` for the first `page.goto()`, `submit_wait_until` for submit navigation.
   If omitted, they default to `domcontentloaded` and `networkidle`. Use `commit` for flaky pages that time out before the DOM finishes loading.
   Korail is captured from server-rendered HTML fetched through a Playwright API request context and rendered locally in Playwright.
   Seoul Metro is fetched using `urllib` with certificate verification enabled. After the initial GET, the date is POSTed to the final redirected URL using the same cookie session. The POST response is replayed through a routed document load at its final URL, without hardcoding a scheme or port.
   Seoul Metro's selected date and option value are checked in both the returned HTML and the rendered page. Missing or mismatched selections fail the capture instead of saving the wrong date.
4. Export SMTP variables.
   `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`
5. Run manually.
   `python3 main.py --no-email`
   `python3 main.py --date 2026-05-12`
6. Or run it from `cron`.
   `15 10 * * 1-5 cd /path/to/subway_certificate_delay && /usr/bin/env python3 main.py`

## Failure reporting

- Browser navigation checks HTTP error responses (status 400 or higher) before waiting for page content, including after an initial-navigation retry or HTTPS fallback. Errors include the target ID, status, requested URL, and final response URL; Korail and Seoul Metro keep their separate fetch paths.
- HTTP errors do not trigger an extra navigation retry. Existing timeout retries and up to two capture attempts per target remain unchanged.
- Console output and `output/YYYY-MM-DD/run.log` retain full errors. Emails include `Failure details:` only for targets that still fail after retries, with each error normalized to one line and limited to 500 characters. Successful captures are still attached as a ZIP.
- An HTTP 503 indicates unavailability, not necessarily maintenance. Maintenance pages returning HTTP 200 and failures of individual CSS/image requests are not detected by this check.
- Seoul Metro logs GET/POST stages, redirect status codes and URLs, and final response status/URL to the console and run log, without cookies or full response bodies. Fetch failures retain the original exception and include the request stage and last URL. There are no internal fetch retries or added delays; only the existing maximum of two capture attempts applies.

## Cron notes

- `cron` registration and execution time are managed outside this repository.
- `10:15 KST` is the recommended weekday run time to avoid the post-commute spike that can affect operator websites.
- Keep the SMTP variables available in the cron environment, or source them before `python3 main.py`.

## Seoul Metro connectivity diagnostics

The existing GitHub Actions job diagnoses Seoul Metro automatically after a final capture failure, on the same runner. It does not run network probes for failures limited to other operators or SMTP. The original capture failure stays failed; diagnostic errors cannot turn it into a success or prevent the existing artifact upload.

The optional capture flag `--result-json PATH` writes only the capture date and final target IDs/success flags. Calls without this option, including existing cron commands, are unchanged. To use diagnostics locally:

```sh
python3 main.py --no-email --result-json output/capture-result.json
python3 -m subway_delay.diagnostics --capture-result output/capture-result.json --config config/targets.yaml --output-dir output/diagnostics
```

Run the second command even if capture exits non-zero. Missing or malformed results, successful Seoul Metro captures, and missing/disabled Seoul Metro configuration produce a skip report without network requests.

- Diagnostics make GET requests only: urllib and curl each check the configured URL, then each checks the observed redirect destination directly. The urllib destination takes priority. No distinct destination means the direct checks are skipped. No scheme or port is hardcoded.
- Each of at most four comparisons uses a separate process, a fresh cookie session, HTTP/1.1, `User-Agent: Mozilla/5.0`, and certificate verification. Cookies are preserved within a redirect chain. There are no retries, at most five redirects, a 30-second request timeout, and a 35-second process limit. These are diagnostic requests, not additional capture attempts; they do not generate PNGs or send email.
- Download the existing `subway-delay-output` artifact and inspect `diagnostics/report.json` and `diagnostics/summary.md`. The summary also appears in GitHub Step Summary. JSON includes redirect paths, exception types or curl exit codes, total elapsed time, curl DNS/TCP/TLS/first-response cumulative timings and remote IP. Unavailable values remain null; phase timings are not fabricated for urllib.
- `expected_page` means a 2xx response with both `view_date` and `tbl-type1` markers, not a full certificate validation. `unexpected_page` distinguishes a 2xx response with missing markers. Request failures are collected normally; missing/broken diagnostic tools make the diagnostic command exit non-zero after preserving the other results.
- Only Python/OpenSSL/curl versions and proxy-variable presence are recorded. Cookies, raw headers/body/stderr, SMTP data, and environment-variable values are not included. Response files are temporary; URL credentials and query values other than `menuIdx` are redacted in reports. Marker checks inspect at most the first 2 MB of a response and record truncation.
- Both clients failing suggests investigating the connection path/server; different results suggest investigating client behavior. Neither proves blocking, and success during diagnosis does not disprove an earlier failure. The Ubuntu runner provides curl; local machines need curl with `%{json}` support (7.70 or newer). No Python dependencies are added.
