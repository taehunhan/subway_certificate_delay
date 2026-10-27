from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

import yaml


class HostedWorkflowTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / ".github/workflows/capture-delay-certificates.yml"
        self.workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.trigger = self.workflow.get("on", self.workflow.get(True))
        self.job = self.workflow["jobs"]["capture"]
        self.steps = {step["name"]: step for step in self.job["steps"]}

    def test_existing_external_dispatch_stays_normal_capture_on_hosted_runner(self):
        self.assertEqual(set(self.trigger), {"workflow_dispatch"})
        mode = self.trigger["workflow_dispatch"]["inputs"]["mode"]
        self.assertEqual(mode["default"], "capture")
        self.assertFalse(mode["required"])
        self.assertEqual(self.job["runs-on"], "ubuntu-latest")
        self.assertEqual(self.steps["Set up Python"]["with"]["python-version"], "3.12")
        capture = self.steps["Run capture job"]
        self.assertEqual(capture["if"], "${{ inputs.mode == 'capture' || inputs.mode == '' }}")
        self.assertEqual(capture["run"], "python main.py")
        self.assertNotIn("continue-on-error", capture)
        for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM"):
            self.assertEqual(capture["env"][key], "${{ secrets." + key + " }}")

    def test_diagnostic_modes_never_receive_smtp_secrets(self):
        self.assertNotIn("env", self.job)
        self.assertNotIn("env", self.workflow)
        for step in self.job["steps"]:
            if step["name"] != "Run capture job":
                self.assertNotIn("secrets.SMTP", str(step))
        diagnostic = self.steps["Diagnose Seoul Metro connectivity"]
        self.assertEqual(diagnostic["if"], "${{ inputs.mode == 'diagnose-seoulmetro' }}")
        self.assertIn("subway_delay.diagnostics", diagnostic["run"])
        self.assertIn('--direct-url "$DIAGNOSTIC_URL"', diagnostic["run"])
        self.assertNotIn("${{", diagnostic["run"])
        self.assertEqual(diagnostic["timeout-minutes"], 4)
        self.assertEqual(self.steps["Install Playwright browser"]["if"], "${{ inputs.mode != 'diagnose-seoulmetro' }}")
        check = self.steps["Check Seoul Metro without email"]
        self.assertEqual(check["if"], "${{ inputs.mode == 'check-seoulmetro' }}")
        self.assertIn("--target seoulmetro --no-email", check["run"])
        self.assertIn("--output-dir output/seoulmetro-check", check["run"])

    def test_diagnostics_have_explicit_https_destination_and_artifacts_always_upload(self):
        url = self.trigger["workflow_dispatch"]["inputs"]["diagnostic_url"]["default"]
        self.assertEqual(url, "https://www.seoulmetro.co.kr:444/kr/delayProofList.do?menuIdx=543")
        upload = self.steps["Upload output artifacts"]
        self.assertEqual(upload["if"], "always()")
        self.assertEqual(upload["with"]["path"], "output/")
        self.assertEqual(upload["with"]["name"], "subway-delay-output")

    @unittest.skipUnless(os.name == "posix", "Workflow shell validation uses a POSIX shell")
    def test_invalid_mode_is_rejected_but_empty_external_dispatch_is_supported(self):
        script = self.steps["Validate run mode"]["run"]
        for mode, expected in (("", 0), ("capture", 0), ("diagnose-seoulmetro", 0), ("check-seoulmetro", 0), ("typo", 2)):
            with self.subTest(mode=mode):
                result = subprocess.run(
                    ["/bin/sh", "-c", script], env={**os.environ, "RUN_MODE": mode},
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, expected, result.stderr)


if __name__ == "__main__":
    unittest.main()
