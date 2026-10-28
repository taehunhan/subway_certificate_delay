from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from pathlib import Path

from subway_delay.config import load_config
from subway_delay.curl_http import valid_direct_url
from subway_delay.dates import parse_manual_date, resolve_capture_date
from subway_delay.runner import execute_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture subway delay certificate summary pages and email them."
    )
    parser.add_argument(
        "--date",
        help="Override capture date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--config",
        default="config/targets.yaml",
        help="Path to the YAML configuration file.",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Skip SMTP delivery and only create local artifacts.",
    )
    parser.add_argument(
        "--target", action="append", metavar="ID",
        help="Run only this enabled target; repeat to select multiple targets.",
    )
    parser.add_argument(
        "--output-dir", type=Path,
        help="Override the output directory, for example for an isolated dry run.",
    )
    parser.add_argument(
        "--seoulmetro-curl-url", metavar="HTTPS_URL",
        help="Override the configured Seoul Metro HTTPS URL for checks; requires --target seoulmetro --no-email.",
    )
    return parser


async def async_main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path, base_dir=Path.cwd())
    if args.target:
        requested = set(args.target)
        available = {target.id for target in config.enabled_targets}
        unknown = requested - available
        if unknown:
            parser.error("Unknown or disabled target(s): " + ", ".join(sorted(unknown)))
        config = replace(config, targets=[target for target in config.enabled_targets if target.id in requested])
    if args.output_dir is not None:
        config = replace(config, output_dir=args.output_dir.resolve())
    if args.seoulmetro_curl_url is not None:
        if not args.no_email or set(args.target or []) != {"seoulmetro"}:
            parser.error("--seoulmetro-curl-url requires --target seoulmetro --no-email")
        target = config.enabled_targets[0]
        if target.selection_mode != "seoulmetro_select" or not valid_direct_url(target.url, args.seoulmetro_curl_url):
            parser.error("Curl URL must be HTTPS on the Seoul Metro target's hostname and path, without credentials or a fragment")
    manual_date = parse_manual_date(args.date)
    capture_date = resolve_capture_date(
        timezone_name=config.timezone,
        explicit_date=manual_date,
    )

    run_result = await execute_run(
        config=config,
        capture_date=capture_date,
        send_email=not args.no_email,
        seoulmetro_curl_url=args.seoulmetro_curl_url,
    )
    return 0 if run_result.overall_success else 1


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
