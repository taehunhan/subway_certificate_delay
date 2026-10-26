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
   Seoul Metro is fetched over standard HTTP, then replayed through a routed document load so its original page assets and scripts render under the real URL.
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

## Cron notes

- `cron` registration and execution time are managed outside this repository.
- `10:15 KST` is the recommended weekday run time to avoid the post-commute spike that can affect operator websites.
- Keep the SMTP variables available in the cron environment, or source them before `python3 main.py`.
