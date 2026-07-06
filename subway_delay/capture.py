from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urljoin

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


def initial_navigation_wait_until(target: TargetConfig) -> str:
    return target.initial_wait_until


def submit_navigation_wait_until(target: TargetConfig) -> str:
    return target.submit_wait_until


def https_fallback_url(url: str) -> str | None:
    if not url.startswith("http://"):
        return None
    return "https://" + url.removeprefix("http://")


def should_retry_with_https_fallback(url: str, error: Exception) -> bool:
    return https_fallback_url(url) is not None and error.__class__.__name__ == "TimeoutError"


def korail_base_href(url: str) -> str:
    return urljoin(url, ".")


def build_korail_form_data(capture_date: date) -> bytes:
    return urlencode({"indate": capture_date.isoformat()}).encode("utf-8")


def inject_base_href(html: str, base_href: str) -> str:
    if "<base " in html:
        return html

    head_close_index = html.find("</head>")
    base_tag = f'<base href="{base_href}">'
    if head_close_index != -1:
        return html[:head_close_index] + base_tag + html[head_close_index:]
    return base_tag + html


async def fetch_korail_html_pair(
    *,
    request_context,
    url: str,
    capture_date: date,
    timeout_ms: int,
) -> tuple[str, str]:
    initial_response = await request_context.get(
        url,
        timeout=timeout_ms,
    )
    initial_html = await _read_korail_response_text(initial_response, url=url, method="GET")

    selected_response = await request_context.post(
        url,
        data=build_korail_form_data(capture_date),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=timeout_ms,
    )
    selected_html = await _read_korail_response_text(selected_response, url=url, method="POST")
    return initial_html, selected_html


async def _read_korail_response_text(response, *, url: str, method: str) -> str:
    if not response.ok:
        raise ValueError(f"Korail {method} request failed with status {response.status} for {url}.")
    return await response.text()


KORAIL_DATE_SELECTOR = 'select[name="indate"]'
KORAIL_TABLE_SELECTOR = ".table-responsive table"
SEOULMETRO_TABLE_SELECTOR = "#contents .tbl-type1"
SEOULMETRO_BOTTOM_MARKERS = ("9호선", "주의사항")
GOTO_RETRY_DELAY_SECONDS = 3

logger = logging.getLogger(__name__)


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
            request_context = None
            try:
                if target.selection_mode == "korail_select":
                    request_context = await playwright.request.new_context(
                        ignore_https_errors=True,
                        extra_http_headers={"User-Agent": "Mozilla/5.0"},
                        timeout=self.timeout_ms,
                    )
                    await self._capture_korail(page, target, capture_date, request_context)
                else:
                    await self._goto_target_page(page, target)
                    await page.wait_for_selector(
                        target.wait_selector,
                        state="attached",
                        timeout=self.timeout_ms,
                    )
                    await self._apply_date_selection(page, target, capture_date)
                await page.wait_for_selector(target.capture_selector, timeout=self.timeout_ms)
                await page.locator(target.capture_selector).screenshot(path=str(destination))
            finally:
                if request_context is not None:
                    await request_context.dispose()
                await context.close()
                await browser.close()

    async def _apply_date_selection(self, page, target: TargetConfig, capture_date: date) -> None:
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

    async def _goto_target_page(self, page, target: TargetConfig) -> None:
        wait_until = initial_navigation_wait_until(target)
        try:
            await page.goto(
                target.url,
                wait_until=wait_until,
                timeout=self.timeout_ms,
            )
        except Exception as exc:
            fallback_url = https_fallback_url(target.url)
            if should_retry_with_https_fallback(target.url, exc) and fallback_url is not None:
                logger.warning(
                    "Initial goto timed out for %s at %s; retrying with fallback URL %s (%s)",
                    target.id,
                    target.url,
                    fallback_url,
                    exc.__class__.__name__,
                )
                await page.goto(
                    fallback_url,
                    wait_until=wait_until,
                    timeout=self.timeout_ms,
                )
                return

            if exc.__class__.__name__ != "TimeoutError":
                raise

            logger.warning(
                "Initial goto timed out for %s at %s; retrying same URL after %ss (%s)",
                target.id,
                target.url,
                GOTO_RETRY_DELAY_SECONDS,
                exc.__class__.__name__,
            )
            await asyncio.sleep(GOTO_RETRY_DELAY_SECONDS)
            await page.goto(
                target.url,
                wait_until=wait_until,
                timeout=self.timeout_ms,
            )

    async def _capture_korail(
        self,
        page,
        target: TargetConfig,
        capture_date: date,
        request_context,
    ) -> None:
        initial_html, selected_html = await fetch_korail_html_pair(
            request_context=request_context,
            url=target.url,
            capture_date=capture_date,
            timeout_ms=self.timeout_ms,
        )
        base_href = korail_base_href(target.url)

        await page.set_content(
            inject_base_href(initial_html, base_href),
            wait_until="domcontentloaded",
        )
        await page.wait_for_selector(
            KORAIL_DATE_SELECTOR,
            state="attached",
            timeout=self.timeout_ms,
        )

        await page.set_content(
            inject_base_href(selected_html, base_href),
            wait_until="domcontentloaded",
        )
        await self._wait_for_korail_capture_ready(page, capture_date)

    async def _wait_for_korail_capture_ready(self, page, capture_date: date) -> None:
        await page.wait_for_selector(
            KORAIL_DATE_SELECTOR,
            state="attached",
            timeout=self.timeout_ms,
        )
        await page.wait_for_selector(
            KORAIL_TABLE_SELECTOR,
            state="attached",
            timeout=self.timeout_ms,
        )
        await page.wait_for_function(
            """({ selector, captureDate, tableSelector }) => {
                const select = document.querySelector(selector);
                const table = document.querySelector(tableSelector);
                return Boolean(
                    select &&
                    select.value === captureDate &&
                    table
                );
            }""",
            arg={
                "selector": KORAIL_DATE_SELECTOR,
                "captureDate": capture_date.isoformat(),
                "tableSelector": KORAIL_TABLE_SELECTOR,
            },
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
        await self._submit_target_form(page, target)
        await self._wait_for_seoulmetro_capture_ready(page, target)

    async def _submit_target_form(self, page, target: TargetConfig) -> None:
        if not target.submit_selector:
            return

        async with page.expect_navigation(
            wait_until=submit_navigation_wait_until(target),
            timeout=self.timeout_ms,
        ):
            await page.locator(target.submit_selector).click()

    async def _wait_for_seoulmetro_capture_ready(self, page, target: TargetConfig) -> None:
        await page.wait_for_selector(
            target.wait_selector,
            state="attached",
            timeout=self.timeout_ms,
        )
        await page.wait_for_selector(
            SEOULMETRO_TABLE_SELECTOR,
            state="attached",
            timeout=self.timeout_ms,
        )
        await self._wait_for_seoulmetro_bottom_markers(page, target.capture_selector)
        await self._wait_for_stable_height(page, target.capture_selector)

    async def _wait_for_seoulmetro_bottom_markers(self, page, capture_selector: str) -> None:
        await page.wait_for_function(
            """({ selector, lineText, noticeText }) => {
                const container = document.querySelector(selector);
                if (!container) {
                    return false;
                }
                const text = container.textContent || "";
                return text.includes(lineText) && text.includes(noticeText);
            }""",
            arg={
                "selector": capture_selector,
                "lineText": SEOULMETRO_BOTTOM_MARKERS[0],
                "noticeText": SEOULMETRO_BOTTOM_MARKERS[1],
            },
            timeout=self.timeout_ms,
        )

    async def _wait_for_stable_height(self, page, selector: str) -> None:
        await page.wait_for_function(
            """({ selector, stableCount }) => {
                const element = document.querySelector(selector);
                if (!element) {
                    return false;
                }

                const height = Math.max(
                    Math.ceil(element.scrollHeight || 0),
                    Math.ceil(element.getBoundingClientRect().height || 0)
                );
                window.__codexStableHeights = window.__codexStableHeights || {};

                const previous = window.__codexStableHeights[selector] || { height: -1, stable: 0 };
                const stable = previous.height === height ? previous.stable + 1 : 1;
                window.__codexStableHeights[selector] = { height, stable };

                return stable >= stableCount;
            }""",
            arg={"selector": selector, "stableCount": 2},
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
