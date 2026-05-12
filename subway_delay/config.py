from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


VALID_SELECTION_MODES = {
    "korail_select",
    "seoulmetro_select",
    "metro9_tab",
}


@dataclass(frozen=True)
class TargetConfig:
    id: str
    name: str
    url: str
    enabled: bool
    selection_mode: str
    capture_selector: str
    wait_selector: str
    submit_selector: str | None = None


@dataclass(frozen=True)
class AppConfig:
    timezone: str
    recipients: list[str]
    output_dir: Path
    targets: list[TargetConfig]

    @property
    def enabled_targets(self) -> list[TargetConfig]:
        return [target for target in self.targets if target.enabled]


def load_config(path: str | Path, base_dir: Path | None = None) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)

    if not isinstance(raw, dict):
        raise ValueError("Config file must contain a YAML mapping.")

    timezone = _require_string(raw, "timezone")
    output_dir_raw = _require_string(raw, "output_dir")
    recipients = _parse_recipients(raw.get("recipients"))
    targets = _parse_targets(raw.get("targets"))

    resolved_base_dir = base_dir or Path.cwd()
    output_dir = Path(output_dir_raw)
    if not output_dir.is_absolute():
        output_dir = resolved_base_dir / output_dir

    return AppConfig(
        timezone=timezone,
        recipients=recipients,
        output_dir=output_dir,
        targets=targets,
    )


def _parse_recipients(raw_recipients: Any) -> list[str]:
    if not isinstance(raw_recipients, list) or not raw_recipients:
        raise ValueError("'recipients' must be a non-empty list of email addresses.")

    recipients: list[str] = []
    for recipient in raw_recipients:
        if not isinstance(recipient, str) or not recipient.strip():
            raise ValueError("Each recipient must be a non-empty string.")
        recipients.append(recipient.strip())
    return recipients


def _parse_targets(raw_targets: Any) -> list[TargetConfig]:
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("'targets' must be a non-empty list.")

    targets: list[TargetConfig] = []
    for index, item in enumerate(raw_targets):
        if not isinstance(item, dict):
            raise ValueError(f"Target entry at index {index} must be a mapping.")

        selection_mode = _require_string(item, "selection_mode")
        if selection_mode not in VALID_SELECTION_MODES:
            raise ValueError(
                f"Unsupported selection_mode '{selection_mode}'. "
                f"Expected one of: {', '.join(sorted(VALID_SELECTION_MODES))}."
            )

        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"Target '{item.get('id', index)}' has a non-boolean 'enabled'.")

        submit_selector = item.get("submit_selector")
        if submit_selector is not None and (
            not isinstance(submit_selector, str) or not submit_selector.strip()
        ):
            raise ValueError(
                f"Target '{item.get('id', index)}' has an invalid 'submit_selector'."
            )

        targets.append(
            TargetConfig(
                id=_require_string(item, "id"),
                name=_require_string(item, "name"),
                url=_require_string(item, "url"),
                enabled=enabled,
                selection_mode=selection_mode,
                capture_selector=_require_string(item, "capture_selector"),
                wait_selector=_require_string(item, "wait_selector"),
                submit_selector=submit_selector.strip() if submit_selector else None,
            )
        )

    return targets


def _require_string(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' must be a non-empty string.")
    return value.strip()
