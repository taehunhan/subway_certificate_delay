from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from subway_delay.config import load_config


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


if __name__ == "__main__":
    unittest.main()
