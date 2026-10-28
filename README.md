# Subway Delay Certificate Capture

Python program that captures subway operators' summary delay pages, packages PNG files into a ZIP, and sends the result through SMTP.

## Production execution

The existing arrangement stays unchanged:

```text
cron-job.org -> GitHub Actions workflow_dispatch -> GitHub-hosted Ubuntu runner -> SMTP email
```

- Keep the existing cron-job.org schedule, dispatch URL, authentication, and branch. No local cron or self-hosted runner is required.
- Keep the existing GitHub Actions secrets: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, and `SMTP_FROM`. No local `.env` file is required or loaded.
- The workflow remains `.github/workflows/capture-delay-certificates.yml`, on `ubuntu-latest` with Python 3.12. An existing dispatch without inputs defaults to `capture` and runs `python main.py`, including email, as before.
- Scheduling is controlled by cron-job.org, not by this repository. `timezone: Asia/Seoul` controls the capture date, not the external schedule. The previous-weekday rule is unchanged.
- The application still attempts each target at most twice. Partial successes are packaged and emailed; any capture or SMTP failure leaves the normal job failed. An operator's maintenance response is not bypassed.
- The GitHub-hosted connection problem has not been declared fixed. The diagnostic modes below collect evidence on that environment without changing capture transport or disabling certificate checks.

## Run diagnostics on GitHub

Commit and push the changes to the branch used by the workflow first. Open **Actions > Capture Subway Delay Certificates > Run workflow** and select a mode:

| Mode | Behavior | Email |
| --- | --- | --- |
| `capture` (default) | Normal all-target capture with existing Secrets | Yes |
| `diagnose-seoulmetro` | urllib/curl GET comparison on the same hosted runner, without launching Chromium | No |
| `check-seoulmetro` | Trial curl direct-HTTPS GET/POST for Seoul Metro, PNG/ZIP and date validation | No |

`diagnostic_url` defaults to the HTTPS destination observed on 2026-10-27 (`https://www.seoulmetro.co.kr:444/kr/delayProofList.do?menuIdx=543`). It is editable and affects only the two manual test modes, never the normal `capture` job.

The hosted comparison found one successful direct curl GET. The next step is **`check-seoulmetro`**, leaving `diagnostic_url` at its default. This trial uses curl for GET and date POST with the same cookie file, then renders the returned HTML with Playwright at the POST response's final URL. It does not visit the configured HTTP URL first and does not fall back to urllib. The previous weekday is calculated normally. Inspect the selected date in the PNG, the complete table and the bottom notice. Repeat this mode on GitHub before deciding whether to enable the transport in production; a single successful GET does not establish that POST or capture is reliable.

The trial preserves certificate verification for curl and its browser context. It uses HTTP/1.1 and the same public headers as the successful probe, a 30-second request timeout and 35-second process limit, at most five HTTPS redirects per request, and no internal retry. Each existing outer capture attempt starts a fresh temporary cookie session; GET and POST within that attempt share it. Responses are limited to 5 MB. HTTP errors, curl errors, missing dates and incorrect selected dates fail that attempt. `run.log` records GET/POST status and HTML/DOM date validation, not cookies, raw responses or curl stderr. Temporary responses and cookies are removed even after failures. The hosted capture-check step has an eight-minute limit.

The curl trial is intentionally restricted to `--target seoulmetro --no-email`. SMTP Secrets, cron-job.org, other operators and the normal urllib capture path remain unchanged until the trial is verified. To repeat the GET-only comparison instead, use `diagnose-seoulmetro`.

The probe sequence is the configured URL via urllib, the configured URL via curl, the explicit HTTPS destination via urllib, then the same HTTPS destination via curl. **The direct checks still run when both original requests fail before receiving a redirect.** The explicit destination must use HTTPS and the configured target's hostname/path, without credentials or a fragment. If the input is cleared, the tool instead uses an observed redirect destination when available (urllib first); without either, it records why direct checks were skipped.

Download the existing `subway-delay-output` artifact:
- `diagnostics/report.json` contains redirect paths, HTTP status, exception type/curl exit code, elapsed time, curl DNS/TCP/TLS/first-byte cumulative timings and remote IP.
- `diagnostics/summary.md` contains the comparison table and collection issues. It also appears in GitHub Step Summary.
- `check-seoulmetro` results are under `seoulmetro-check/YYYY-MM-DD/`, including `seoulmetro.png`, ZIP and `run.log`.

Diagnostics use at most four comparisons, no retries, at most five redirects each, a 30-second request timeout and a 35-second subprocess limit. The workflow caps the diagnostic step at four minutes and uploads artifacts with `always()`. Both clients use HTTP/1.1, `User-Agent: Mozilla/5.0`, certificate verification, and a fresh cookie session for each comparison, preserving cookies within redirects.

Connection failures are diagnostic results, so a green **diagnostic** job only means collection completed. Look at each row's result. `expected_page` means a 2xx response containing both `view_date` and `tbl-type1`, not a verified selected date. `unexpected_page` means those markers are missing. Missing/broken tools or report-storage failures make the diagnostic process fail; other completed probe results are retained when a probe fails.

Cookies, raw headers/body/stderr, SMTP values and proxy values are not included in reports. Only Python/OpenSSL/curl versions and proxy-variable presence are recorded. Sensitive URL query values are masked; temporary response files are discarded. Body inspection is limited to the first 2 MB, with truncation recorded.

If only one client or entry URL succeeds, investigate that difference before changing production transport. If all fail, investigate the hosted execution environment and server/network path. Neither outcome alone proves IP blocking, and success now does not disprove an earlier failure. No automatic switch of library, certificate policy, runner or production URL occurs.

## Capture correctness

Seoul Metro's initial GET final URL is used for POST in the same cookie session, so redirects from the old address do not silently lose the date. The POST response's final URL is used for rendering; the production code hardcodes neither HTTPS nor port 444. Both returned HTML and the rendered dropdown must select exactly the requested date/value, otherwise capture fails. The same validation, lines 1-9, bottom notice and height-stability checks apply to the curl trial.

This date-integrity correction is independent of the connection failures. It has been verified locally with a real PNG, but does not guarantee GitHub-hosted connectivity.

Recipients and targets remain in [config/targets.yaml](config/targets.yaml). Optional `initial_wait_until` / `submit_wait_until` fields default to `domcontentloaded` / `networkidle`. Korail uses Playwright API request context HTML fetch; Seoul Metro uses standard-library HTTP fetch and routed document rendering. Other operators, filenames, ZIP and SMTP behavior are unchanged.

## Developer checks

Install `requirements.txt` and Chromium with `python -m playwright install chromium` (Linux may also need `--with-deps`). New Python dependencies have not been added. Diagnostics require curl 7.70+ for its JSON timing output; Ubuntu runners include curl.

```sh
python main.py --target seoulmetro --no-email --output-dir output/seoulmetro-check --seoulmetro-curl-url 'https://www.seoulmetro.co.kr:444/kr/delayProofList.do?menuIdx=543'
python -m subway_delay.diagnostics --config config/targets.yaml --direct-url 'https://www.seoulmetro.co.kr:444/kr/delayProofList.do?menuIdx=543'
python -m unittest discover -s tests -q -b
```

`--target ID` selects enabled targets without editing YAML and can be repeated. Unknown/disabled IDs fail before capture. `--output-dir` isolates test artifacts. Optional `--date YYYY-MM-DD` uses a date still available on the site; omitting it selects the previous weekday. Normal calls without these options keep their original behavior.

Omitting `--seoulmetro-curl-url` keeps the existing urllib transport. New Python packages are not required; the trial uses the curl executable already supplied on the Ubuntu runner. `diagnostic_url` must be nonempty for `check-seoulmetro`; no silent fallback is used when it is missing or invalid.
