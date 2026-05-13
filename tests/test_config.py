from __future__ import annotations

import tempfile
import textwrap
import unittest
from datetime import date
from pathlib import Path

from subway_delay.config import TargetConfig, load_config


class LoadConfigTests(unittest.TestCase):
    def test_loads_valid_config(self) -> None:
        config_text = textwrap.dedent(
            """
            timezone: Asia/Seoul
            output_dir: output
            recipients:
              - first@example.com
              - second@example.com
            targets:
              - id: one
                name: One
                url: https://example.com
                enabled: true
                selection_mode: korail_select
                capture_selector: "#main"
                wait_selector: "#main"
            """
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "targets.yaml"
            config_path.write_text(config_text, encoding="utf-8")

            config = load_config(config_path, base_dir=Path(temp_dir))

        self.assertEqual(config.timezone, "Asia/Seoul")
        self.assertEqual(config.recipients, ["first@example.com", "second@example.com"])
        self.assertEqual(config.output_dir, Path(temp_dir) / "output")
        self.assertEqual(len(config.targets), 1)

    def test_disabled_targets_are_filtered_from_enabled_targets(self) -> None:
        config_text = textwrap.dedent(
            """
            timezone: Asia/Seoul
            output_dir: output
            recipients:
              - first@example.com
            targets:
              - id: enabled
                name: Enabled
                url: https://example.com/one
                enabled: true
                selection_mode: korail_select
                capture_selector: "#main"
                wait_selector: "#main"
              - id: disabled
                name: Disabled
                url: https://example.com/two
                enabled: false
                selection_mode: metro9_tab
                capture_selector: "#main"
                wait_selector: "#main"
            """
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "targets.yaml"
            config_path.write_text(config_text, encoding="utf-8")

            config = load_config(config_path, base_dir=Path(temp_dir))

        self.assertEqual([target.id for target in config.enabled_targets], ["enabled"])

    def test_missing_required_field_raises(self) -> None:
        config_text = textwrap.dedent(
            """
            timezone: Asia/Seoul
            output_dir: output
            recipients:
              - first@example.com
            targets:
              - id: broken
                name: Broken
                enabled: true
                selection_mode: korail_select
                capture_selector: "#main"
                wait_selector: "#main"
            """
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "targets.yaml"
            config_path.write_text(config_text, encoding="utf-8")

            with self.assertRaises(ValueError):
                load_config(config_path, base_dir=Path(temp_dir))

    def test_empty_recipient_string_raises(self) -> None:
        config_text = textwrap.dedent(
            """
            timezone: Asia/Seoul
            output_dir: output
            recipients:
              - ""
            targets:
              - id: one
                name: One
                url: https://example.com
                enabled: true
                selection_mode: korail_select
                capture_selector: "#main"
                wait_selector: "#main"
            """
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "targets.yaml"
            config_path.write_text(config_text, encoding="utf-8")

            with self.assertRaises(ValueError):
                load_config(config_path, base_dir=Path(temp_dir))

    def test_loads_optional_selection_and_filename_fields(self) -> None:
        config_text = textwrap.dedent(
            """
            timezone: Asia/Seoul
            output_dir: output
            recipients:
              - first@example.com
            targets:
              - id: gtx
                name: GTX
                url: https://example.com
                enabled: true
                selection_mode: gtx_fetch
                selection_value: L08
                capture_selector: "#main"
                wait_selector: "#main"
                filename_template: "gtx-{date}.png"
            """
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "targets.yaml"
            config_path.write_text(config_text, encoding="utf-8")

            config = load_config(config_path, base_dir=Path(temp_dir))

        self.assertEqual(config.targets[0].selection_value, "L08")
        self.assertEqual(config.targets[0].filename_template, "gtx-{date}.png")

    def test_gtx_fetch_requires_selection_value(self) -> None:
        config_text = textwrap.dedent(
            """
            timezone: Asia/Seoul
            output_dir: output
            recipients:
              - first@example.com
            targets:
              - id: gtx
                name: GTX
                url: https://example.com
                enabled: true
                selection_mode: gtx_fetch
                capture_selector: "#main"
                wait_selector: "#main"
            """
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "targets.yaml"
            config_path.write_text(config_text, encoding="utf-8")

            with self.assertRaises(ValueError):
                load_config(config_path, base_dir=Path(temp_dir))


class TargetConfigFilenameTests(unittest.TestCase):
    def test_filename_template_expands_id_and_date(self) -> None:
        target = TargetConfig(
            id="dxline",
            name="신분당선",
            url="https://example.com",
            enabled=True,
            selection_mode="dxline_static",
            capture_selector="#main",
            wait_selector="#main",
            filename_template="dxline-{date}-{id}.png",
        )

        self.assertEqual(
            target.screenshot_filename(date(2026, 5, 12)),
            "dxline-2026-05-12-dxline.png",
        )


if __name__ == "__main__":
    unittest.main()
