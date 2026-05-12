from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from subway_delay.config import load_config
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
    return parser


async def async_main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path, base_dir=Path.cwd())
    manual_date = parse_manual_date(args.date)
    capture_date = resolve_capture_date(
        timezone_name=config.timezone,
        explicit_date=manual_date,
    )

    run_result = await execute_run(
        config=config,
        capture_date=capture_date,
        send_email=not args.no_email,
    )
    return 0 if run_result.overall_success else 1


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
