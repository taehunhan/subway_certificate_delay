from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

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


def gtx_line_label(line_cd: str) -> str:
    if line_cd == "L08":
        return "운정중앙역 - 서울역"
    if line_cd == "L09":
        return "수서역 - 동탄역"
    raise ValueError(f"Unsupported GTX lineCd: {line_cd}")


def gtx_cell_updates(records: list[dict[str, Any]]) -> dict[str, str]:
    updates = {
        "up-time1": "-",
        "up-time2": "-",
        "up-time3": "-",
        "down-time1": "-",
        "down-time2": "-",
        "down-time3": "-",
    }

    for record in records:
        direction = record.get("updwtDvsnNm")
        if direction == "상행":
            prefix = "up"
        elif direction == "하행":
            prefix = "down"
        else:
            continue

        for index, key in enumerate(("timeDvsn1", "timeDvsn2", "timeDvsn3"), start=1):
            value = record.get(key)
            updates[f"{prefix}-time{index}"] = value.strip() if isinstance(value, str) and value.strip() else "-"

    return updates


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
            context = await browser.new_context(
                viewport={"width": 1600, "height": 2200},
                locale="ko-KR",
                ignore_https_errors=True,
            )
            page = await context.new_page()
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
                await context.close()
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
        if target.selection_mode == "ui_line_tab":
            await self._capture_ui_line(page, capture_date)
            return
        if target.selection_mode == "gtx_fetch":
            await self._capture_gtx(page, target, capture_date)
            return
        if target.selection_mode == "dxline_static":
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
        await page.wait_for_selector(
            selector,
            state="attached",
            timeout=self.timeout_ms,
        )
        await page.wait_for_function(
            "() => typeof fn_delay_list === 'function'",
            timeout=self.timeout_ms,
        )
        button_label = await page.locator(f"{selector} span").text_content()
        if button_label is None:
            raise ValueError(f"Could not resolve metro9 tab label for {capture_date.isoformat()}.")

        async with page.expect_response(
            lambda response: response.url.endswith("/prog/delayCrtf/kor/sub01_09/ajax.do")
            and response.request.method == "POST",
            timeout=self.timeout_ms,
        ) as response_info:
            await page.evaluate(
                """({ delayDt, label }) => {
                    const tab = document.querySelector(`li.button_tab[data-tab="${delayDt}"]`);
                    if (!tab) {
                        throw new Error(`Metro9 tab not found for ${delayDt}`);
                    }

                    document.querySelectorAll("li.button_tab").forEach((element) => {
                        element.classList.remove("on");
                    });
                    tab.classList.add("on");

                    const button = document.querySelector("#tab_moType1 button.station-active");
                    if (button) {
                        button.textContent = label.trim();
                    }

                    fn_delay_list(delayDt);
                }""",
                {"delayDt": capture_date.isoformat(), "label": button_label},
            )
        response = await response_info.value
        if not response.ok:
            raise ValueError(
                f"Metro9 ajax request failed with status {response.status} "
                f"for {capture_date.isoformat()}."
            )

        await page.wait_for_function(
            """(delayDt) => {
                const tab = document.querySelector(`li.button_tab[data-tab="${delayDt}"]`);
                return Boolean(tab && tab.classList.contains("on"));
            }""",
            arg=capture_date.isoformat(),
            timeout=self.timeout_ms,
        )
        await page.wait_for_function(
            """(selector) => {
                const tbody = document.querySelector(selector);
                return Boolean(tbody && tbody.querySelector("tr"));
            }""",
            arg=target.wait_selector,
            timeout=self.timeout_ms,
        )

    async def _capture_ui_line(self, page, capture_date: date) -> None:
        panels = await page.locator("div.tab-content").evaluate_all(
            """(elements) => elements.map((element) => ({
                value: element.id,
                text: (element.querySelector("tbody td[rowspan='2']")?.textContent || "").trim()
            }))"""
        )
        panel_id = option_value_for_date(
            [SelectOption(value=item["value"], text=item["text"]) for item in panels],
            capture_date,
        )
        await page.evaluate(
            """(targetId) => {
                document.querySelectorAll("ul.tabs li").forEach((element) => {
                    element.classList.toggle("current", element.dataset.tab === targetId);
                });
                document.querySelectorAll("div.tab-content").forEach((element) => {
                    element.classList.toggle("current", element.id === targetId);
                });
            }""",
            panel_id,
        )
        await page.wait_for_function(
            """(dateText) => {
                const current = document.querySelector("div.tab-content.current tbody td[rowspan='2']");
                return Boolean(current && current.textContent.trim() === dateText);
            }""",
            arg=capture_date.isoformat(),
            timeout=self.timeout_ms,
        )

    async def _capture_gtx(self, page, target: TargetConfig, capture_date: date) -> None:
        if not target.selection_value:
            raise ValueError("gtx_fetch requires selection_value.")

        line_cd = target.selection_value
        line_label = gtx_line_label(line_cd)
        capture_date_text = capture_date.isoformat()

        records = await page.evaluate(
            """async ({ captureDate, lineCd }) => {
                const response = await fetch(
                    `/getCertificateSD.do?date=${encodeURIComponent(captureDate)}&lineCd=${encodeURIComponent(lineCd)}`
                );
                if (!response.ok) {
                    throw new Error(`GTX fetch failed with status ${response.status}`);
                }
                return await response.json();
            }""",
            {"captureDate": capture_date_text, "lineCd": line_cd},
        )
        if not isinstance(records, list):
            raise ValueError("GTX fetch returned a non-list payload.")

        updates = gtx_cell_updates(records)
        await page.evaluate(
            """({ captureDate, lineCd, lineLabel, updates }) => {
                const dateTrigger = document.querySelector("#select-trigger");
                if (dateTrigger) {
                    dateTrigger.textContent = captureDate;
                }

                document.querySelectorAll(".tab-btn[data-line-cd]").forEach((button) => {
                    const isActive = button.dataset.lineCd === lineCd;
                    button.classList.toggle("is-active", isActive);
                    button.setAttribute("aria-selected", String(isActive));
                    button.setAttribute("tabindex", isActive ? "0" : "-1");
                });

                const caption = document.querySelector(".table-wrap table caption");
                if (caption) {
                    caption.textContent = `${lineLabel} 방면 - 노선 첫차 ~ 09시 09시 ~ 18시 18시 ~ 막차에 대한 정보를 제공`;
                }

                Object.entries(updates).forEach(([cellId, value]) => {
                    const cell = document.getElementById(cellId);
                    if (cell) {
                        cell.textContent = value;
                    }
                });

                const certificateWrap = document.querySelector("#certificate-outer-wrap");
                if (certificateWrap) {
                    certificateWrap.innerHTML = "";
                }
            }""",
            {
                "captureDate": capture_date_text,
                "lineCd": line_cd,
                "lineLabel": line_label,
                "updates": updates,
            },
        )
        await page.wait_for_function(
            """({ captureDate, lineCd }) => {
                const dateTrigger = document.querySelector("#select-trigger");
                const activeButton = document.querySelector(`.tab-btn[data-line-cd="${lineCd}"]`);
                return Boolean(
                    dateTrigger &&
                    dateTrigger.textContent.trim() === captureDate &&
                    activeButton &&
                    activeButton.classList.contains("is-active") &&
                    activeButton.getAttribute("aria-selected") === "true"
                );
            }""",
            arg={"captureDate": capture_date_text, "lineCd": line_cd},
            timeout=self.timeout_ms,
        )
