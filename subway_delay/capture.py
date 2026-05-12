from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from subway_delay.config import TargetConfig


class CaptureServiceProtocol(Protocol):
    async def capture(
        self,
        *,
        target: TargetConfig,
        capture_date: date,
        destination: Path,
    ) -> None: ...


@dataclass(frozen=True)
class SelectOption:
    value: str
    text: str


def option_value_for_date(options: list[SelectOption], target_date: date) -> str:
    target_text = target_date.isoformat()
    for option in options:
        if target_text in option.text:
            return option.value
    raise ValueError(f"Could not find an option containing date {target_text}.")


def metro9_tab_selector(target_date: date) -> str:
    return f'li.button_tab[data-tab="{target_date.isoformat()}"]'


class PlaywrightCaptureService:
    def __init__(self, timeout_ms: int = 30_000) -> None:
        self.timeout_ms = timeout_ms

    async def capture(
        self,
        *,
        target: TargetConfig,
        capture_date: date,
        destination: Path,
    ) -> None:
        from playwright.async_api import async_playwright

        destination.parent.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(
                viewport={"width": 1600, "height": 2200},
                locale="ko-KR",
            )
            try:
                await page.goto(
                    target.url,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                )
                await page.wait_for_selector(
                    target.wait_selector,
                    state="attached",
                    timeout=self.timeout_ms,
                )
                await self._apply_date_selection(page, target, capture_date)
                await page.wait_for_selector(target.capture_selector, timeout=self.timeout_ms)
                await page.locator(target.capture_selector).screenshot(path=str(destination))
            finally:
                await page.close()
                await browser.close()

    async def _apply_date_selection(self, page, target: TargetConfig, capture_date: date) -> None:
        if target.selection_mode == "korail_select":
            await self._capture_korail(page, target, capture_date)
            return
        if target.selection_mode == "seoulmetro_select":
            await self._capture_seoulmetro(page, target, capture_date)
            return
        if target.selection_mode == "metro9_tab":
            await self._capture_metro9(page, target, capture_date)
            return
        raise ValueError(f"Unsupported selection_mode: {target.selection_mode}")

    async def _capture_korail(self, page, target: TargetConfig, capture_date: date) -> None:
        await page.select_option('select[name="indate"]', value=capture_date.isoformat())
        if target.submit_selector:
            await page.locator(target.submit_selector).click()
            await page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
        await page.wait_for_selector(
            target.wait_selector,
            state="attached",
            timeout=self.timeout_ms,
        )

    async def _capture_seoulmetro(self, page, target: TargetConfig, capture_date: date) -> None:
        options = await page.locator("#view_date option").evaluate_all(
            """(elements) => elements.map((element) => ({
                value: element.value,
                text: (element.textContent || "").trim()
            }))"""
        )
        selected_value = option_value_for_date(
            [SelectOption(value=item["value"], text=item["text"]) for item in options],
            capture_date,
        )
        await page.select_option("#view_date", value=selected_value)
        if not target.submit_selector:
            raise ValueError("seoulmetro_select requires submit_selector.")
        await page.locator(target.submit_selector).click()
        await page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
        await page.wait_for_selector(
            target.wait_selector,
            state="attached",
            timeout=self.timeout_ms,
        )

    async def _capture_metro9(self, page, target: TargetConfig, capture_date: date) -> None:
        selector = metro9_tab_selector(capture_date)
        await page.wait_for_selector(selector, timeout=self.timeout_ms)
        await page.locator(selector).click()
        await page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
        active_selector = f'li.button_tab.on[data-tab="{capture_date.isoformat()}"]'
        await page.wait_for_selector(active_selector, timeout=self.timeout_ms)
        await page.wait_for_selector(
            target.wait_selector,
            state="attached",
            timeout=self.timeout_ms,
        )
