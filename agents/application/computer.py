# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Browser backend for the Application agent's Computer Use tool."""

from __future__ import annotations

from typing import Literal

from google.adk.tools.computer_use.base_computer import (
    BaseComputer,
    ComputerEnvironment,
    ComputerState,
)


class HermesBrowserComputer(BaseComputer):
    """Browser backend for the Application agent's Computer Use tool.

    This is a scaffold stub. To submit real applications you must wire a
    concrete browser driver (e.g. Playwright, or the Vertex AI Agent Engine
    browser sandbox) into each method below. ``screen_size`` and
    ``environment`` are implemented so the ``ComputerUseToolset`` can
    initialize; the action methods raise ``NotImplementedError`` until a
    driver is connected.
    """

    SCREEN_WIDTH = 1280
    SCREEN_HEIGHT = 800

    async def screen_size(self) -> tuple[int, int]:
        return (self.SCREEN_WIDTH, self.SCREEN_HEIGHT)

    async def environment(self) -> ComputerEnvironment:
        return ComputerEnvironment.ENVIRONMENT_BROWSER

    def _todo(self, action: str) -> ComputerState:
        raise NotImplementedError(
            f"HermesBrowserComputer.{action} is not wired to a browser driver "
            "yet. Connect Playwright or an Agent Engine browser sandbox to "
            "enable it."
        )

    async def open_web_browser(self) -> ComputerState:
        return self._todo("open_web_browser")

    async def click_at(self, x: int, y: int) -> ComputerState:
        return self._todo("click_at")

    async def hover_at(self, x: int, y: int) -> ComputerState:
        return self._todo("hover_at")

    async def type_text_at(
        self,
        x: int,
        y: int,
        text: str,
        press_enter: bool = True,
        clear_before_typing: bool = True,
    ) -> ComputerState:
        return self._todo("type_text_at")

    async def scroll_document(
        self, direction: Literal["up", "down", "left", "right"]
    ) -> ComputerState:
        return self._todo("scroll_document")

    async def scroll_at(
        self,
        x: int,
        y: int,
        direction: Literal["up", "down", "left", "right"],
        magnitude: int,
    ) -> ComputerState:
        return self._todo("scroll_at")

    async def wait(self, seconds: int) -> ComputerState:
        return self._todo("wait")

    async def go_back(self) -> ComputerState:
        return self._todo("go_back")

    async def go_forward(self) -> ComputerState:
        return self._todo("go_forward")

    async def search(self) -> ComputerState:
        return self._todo("search")

    async def navigate(self, url: str) -> ComputerState:
        return self._todo("navigate")

    async def key_combination(self, keys: list[str]) -> ComputerState:
        return self._todo("key_combination")

    async def drag_and_drop(
        self, x: int, y: int, destination_x: int, destination_y: int
    ) -> ComputerState:
        return self._todo("drag_and_drop")

    async def current_state(self) -> ComputerState:
        return self._todo("current_state")
