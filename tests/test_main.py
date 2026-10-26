from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml

import main
from subway_delay.runner import TargetResult


class ResultJsonTests(unittest.TestCase):
    def test_result_json_contains_only_date_and_final_target_flags(self) -> None:
        for capture_success, overall_success in ((True, True), (False, False), (True, False)):
            with self.subTest(capture_success=capture_success, overall_success=overall_success):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "nested" / "result.json"
                    result = SimpleNamespace(
                        capture_date=date(2026, 10, 23), overall_success=overall_success,
                        results=[TargetResult(
                            "seoulmetro", "Seoul Metro", capture_success, 2, error="PRIVATE ERROR",
                        )],
                    )
                    with patch("sys.argv", ["main.py", "--date", "2026-10-23", "--no-email", "--result-json", str(path)]), patch(
                        "main.load_config", return_value=SimpleNamespace(timezone="Asia/Seoul")
                    ), patch("main.execute_run", new=AsyncMock(return_value=result)) as execute:
                        code = asyncio.run(main.async_main())

                    self.assertEqual(code, 0 if overall_success else 1)
                    self.assertFalse(execute.call_args.kwargs["send_email"])
                    self.assertEqual(json.loads(path.read_text()), {
                        "capture_date": "2026-10-23", "targets": [{"id": "seoulmetro", "success": capture_success}],
                    })
                    self.assertNotIn("PRIVATE ERROR", path.read_text())

    def test_result_option_is_optional_and_does_not_write_when_omitted(self) -> None:
        self.assertIsNone(main.build_parser().parse_args([]).result_json)
        with patch("sys.argv", ["main.py", "--date", "2026-10-23"]), patch(
            "main.load_config", return_value=SimpleNamespace(timezone="Asia/Seoul")
        ), patch("main.execute_run", new=AsyncMock(return_value=SimpleNamespace(overall_success=True))), patch(
            "pathlib.Path.write_text"
        ) as write:
            self.assertEqual(asyncio.run(main.async_main()), 0)
        write.assert_not_called()


class DiagnosticWorkflowTests(unittest.TestCase):
    def test_diagnostic_runs_in_same_job_without_hiding_capture_failure(self) -> None:
        path = Path(__file__).resolve().parents[1] / ".github/workflows/capture-delay-certificates.yml"
        workflow = yaml.safe_load(path.read_text())
        steps = workflow["jobs"]["capture"]["steps"]
        capture = next(step for step in steps if step.get("id") == "capture")
        diagnostic = next(step for step in steps if "subway_delay.diagnostics" in step.get("run", ""))
        upload = next(step for step in steps if "upload-artifact" in step.get("uses", ""))

        self.assertLess(steps.index(capture), steps.index(diagnostic))
        self.assertLess(steps.index(diagnostic), steps.index(upload))
        self.assertNotIn("continue-on-error", capture)
        self.assertEqual(diagnostic["if"], "${{ failure() && steps.capture.outcome == 'failure' }}")
        self.assertIs(diagnostic["continue-on-error"], True)
        self.assertEqual(diagnostic["timeout-minutes"], 4)
        self.assertNotIn("env", diagnostic)
        self.assertIn("--result-json", capture["run"])
        self.assertIn("$RUNNER_TEMP/capture-result-${{ github.run_id }}-${{ github.run_attempt }}.json", diagnostic["run"])
        self.assertEqual(upload["if"], "always()")
        self.assertEqual(upload["with"]["path"], "output/")
        self.assertEqual(workflow.get("on", workflow.get(True)), {"workflow_dispatch": None})


if __name__ == "__main__":
    unittest.main()
