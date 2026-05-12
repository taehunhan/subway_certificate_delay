# Subway Delay Certificate Capture

Python program that captures the summary delay certificate pages for selected subway operators, zips the PNG files, and sends them through SMTP.

## Local run

1. Install dependencies.
   `python3 -m pip install -r requirements.txt`
2. Install the Playwright browser.
   `python3 -m playwright install --with-deps chromium`
3. Update [config/targets.yaml](/Users/taehunhan/Python/subway_certificate_delay/config/targets.yaml) with your recipient list.
4. Export SMTP variables.
   `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`
5. Run manually.
   `python3 main.py --no-email`
   `python3 main.py --date 2026-05-12`

## GitHub Actions secrets

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_FROM`

The workflow runs at `09:00` KST on weekdays and can also be triggered manually.
