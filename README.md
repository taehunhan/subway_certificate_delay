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
4. Export SMTP variables.
   `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`
5. Run manually.
   `python3 main.py --no-email`
   `python3 main.py --date 2026-05-12`
6. Or run it from `cron`.
   `0 9 * * 1-5 cd /path/to/subway_certificate_delay && /usr/bin/env python3 main.py`

## Cron notes

- `cron` registration and execution time are managed outside this repository.
- Keep the SMTP variables available in the cron environment, or source them before `python3 main.py`.
