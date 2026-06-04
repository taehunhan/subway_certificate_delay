from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml


VALID_SELECTION_MODES = {
    "korail_select",
    "seoulmetro_select",
    "metro9_tab",
    "ui_line_tab",
    "gtx_fetch",
    "dxline_static",
}

VALID_WAIT_UNTIL = {
    "commit",
    "domcontentloaded",
    "load",
    "networkidle",
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
    selection_value: str | None = None
    submit_selector: str | None = None
    filename_template: str | None = None
    initial_wait_until: str = "domcontentloaded"
    submit_wait_until: str = "networkidle"

    def screenshot_filename(self, capture_date: date) -> str:
        template = self.filename_template or "{id}.png"
        filename_date = capture_date
        if self.selection_mode == "dxline_static":
            filename_date = next_business_day(capture_date)
        return template.format(id=self.id, date=filename_date.isoformat())


@dataclass(frozen=True)
class AppConfig:
    timezone: str
    recipients: list[str]
    output_dir: Path
    targets: list[TargetConfig]

    @property
    def enabled_targets(self) -> list[TargetConfig]:
        return [target for target in self.targets if target.enabled]


def next_business_day(value: date) -> date:
    weekday = value.weekday()
    if weekday == 4:
        return value + timedelta(days=3)
    if weekday == 5:
        return value + timedelta(days=2)
    return value + timedelta(days=1)


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

        selection_value = item.get("selection_value")
        if selection_value is not None and (
            not isinstance(selection_value, str) or not selection_value.strip()
        ):
            raise ValueError(
                f"Target '{item.get('id', index)}' has an invalid 'selection_value'."
            )

        filename_template = item.get("filename_template")
        if filename_template is not None and (
            not isinstance(filename_template, str) or not filename_template.strip()
        ):
            raise ValueError(
                f"Target '{item.get('id', index)}' has an invalid 'filename_template'."
            )

        initial_wait_until = _parse_wait_until(
            item,
            key="initial_wait_until",
            target_id=item.get("id", index),
            default="domcontentloaded",
        )
        submit_wait_until = _parse_wait_until(
            item,
            key="submit_wait_until",
            target_id=item.get("id", index),
            default="networkidle",
        )

        if selection_mode == "gtx_fetch" and selection_value is None:
            raise ValueError(
                f"Target '{item.get('id', index)}' must define 'selection_value' for gtx_fetch."
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
                selection_value=selection_value.strip() if selection_value else None,
                submit_selector=submit_selector.strip() if submit_selector else None,
                filename_template=filename_template.strip() if filename_template else None,
                initial_wait_until=initial_wait_until,
                submit_wait_until=submit_wait_until,
            )
        )

    return targets


def _require_string(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' must be a non-empty string.")
    return value.strip()


def _parse_wait_until(
    mapping: dict[str, Any],
    *,
    key: str,
    target_id: Any,
    default: str,
) -> str:
    value = mapping.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Target '{target_id}' has an invalid '{key}'.")

    normalized = value.strip()
    if normalized not in VALID_WAIT_UNTIL:
        raise ValueError(
            f"Target '{target_id}' has unsupported '{key}' value '{normalized}'. "
            f"Expected one of: {', '.join(sorted(VALID_WAIT_UNTIL))}."
        )
    return normalized
