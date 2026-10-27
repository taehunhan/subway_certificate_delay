from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from subway_delay.config import AppConfig, TargetConfig


def config_for_test(root: Path) -> AppConfig:
    target = TargetConfig(
        id="seoulmetro", name="Seoul Metro", url="https://example.com",
        enabled=True, selection_mode="seoulmetro_select", capture_selector="#contents",
        wait_selector="#view_date",
    )
    return AppConfig("Asia/Seoul", ["test@example.com"], root / "output", [
        target, replace(target, id="korail"), replace(target, id="disabled", enabled=False),
    ])


class MainTests(unittest.TestCase):
    def run_main(self, config, arguments, *, success=True):
        with patch("sys.argv", ["main.py", "--date", "2026-10-26", *arguments]), patch(
            "main.load_config", return_value=config
        ), patch("main.execute_run", new=AsyncMock(return_value=SimpleNamespace(overall_success=success))) as execute:
            code = asyncio.run(main.async_main())
        return code, execute.call_args.kwargs

    def test_single_target_dry_run_uses_separate_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = config_for_test(root)
            code, args = self.run_main(original, [
                "--target", "seoulmetro", "--no-email", "--output-dir", str(root / "local-check"),
            ])
            self.assertEqual(code, 0)
            self.assertEqual([target.id for target in args["config"].targets], ["seoulmetro"])
            self.assertEqual(args["config"].output_dir, (root / "local-check").resolve())
            self.assertFalse(args["send_email"])
            self.assertEqual(args["capture_date"].isoformat(), "2026-10-26")
            self.assertEqual(len(original.targets), 3)
            self.assertEqual(original.output_dir, root / "output")

    def test_default_keeps_config_and_email_behavior(self):
        config = config_for_test(Path("/tmp"))
        code, args = self.run_main(config, [], success=False)
        self.assertEqual(code, 1)
        self.assertIs(args["config"], config)
        self.assertTrue(args["send_email"])

    def test_repeated_targets_preserve_config_order_without_duplicates(self):
        _, args = self.run_main(config_for_test(Path("/tmp")), [
            "--target", "korail", "--target", "seoulmetro", "--target", "korail",
        ])
        self.assertEqual([target.id for target in args["config"].targets], ["seoulmetro", "korail"])

    def test_unknown_and_disabled_targets_fail_before_capturing(self):
        for target_id in ("missing", "disabled"):
            with self.subTest(target=target_id), patch("sys.argv", ["main.py", "--target", target_id]), patch(
                "main.load_config", return_value=config_for_test(Path("/tmp"))
            ), patch("main.execute_run", new=AsyncMock()) as execute, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    asyncio.run(main.async_main())
                self.assertEqual(error.exception.code, 2)
                execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
