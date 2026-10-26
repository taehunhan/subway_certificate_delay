from __future__ import annotations

import argparse
import asyncio
import json
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
    parser.add_argument(
        "--result-json", type=Path,
        help="Write the capture date and final per-target success flags to a JSON file.",
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
    if args.result_json is not None:
        args.result_json.parent.mkdir(parents=True, exist_ok=True)
        args.result_json.write_text(
            json.dumps({
                "capture_date": run_result.capture_date.isoformat(),
                "targets": [
                    {"id": result.target_id, "success": result.success}
                    for result in run_result.results
                ],
            }, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0 if run_result.overall_success else 1


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
