import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Optional

from playwright.async_api import BrowserContext, Page, async_playwright

logger = logging.getLogger("bot")

_BASE_PROFILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "bot", "bot_browser_profile")
)


class LoginSession:
    def __init__(self) -> None:
        self._pw = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self.active: bool = False
        self.opening: bool = False  # visible-browser login in progress

    # ── Simple visible-browser login (local / any machine with a display) ─────

    async def open_browser(self) -> None:
        """
        Opens a real visible Chrome window so the user can log in normally.
        Waits until the user closes the window, then saves the session.
        """
        if self.opening or self.active:
            return

        headless = os.getenv("HEADLESS", "false").lower() == "true"
        if headless:
            logger.warning("open_browser() called in headless/server mode — skipping (use BOT_SESSION_B64 instead)")
            return

        self.opening = True
        logger.info("Opening visible login browser …")
        pw = await async_playwright().start()
        _session_file = Path(_BASE_PROFILE).parent / "session_state.json"
        try:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=_BASE_PROFILE,
                headless=False,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--start-maximized"],
            )
            page = await ctx.new_page()
            await page.goto("https://accounts.google.com/ServiceLogin")
            logger.info("Login window open — waiting for user to log in and close it")
            try:
                await ctx.wait_for_event("close", timeout=600_000)
            except Exception:
                pass
            # Save session_state.json BEFORE closing so cookies stay in sync with profile.
            # This prevents meet_bot from injecting stale cookies on top of fresh profile cookies.
            try:
                state = await ctx.storage_state()
                _session_file.write_text(json.dumps(state))
                logger.info(
                    "Login window closed — session saved to profile and session_state.json (%d cookies)",
                    len(state.get("cookies", [])),
                )
            except Exception as _e:
                logger.warning("Could not export session_state.json after login: %s", _e)
                logger.info("Login window closed — session saved to profile only")
            try:
                await ctx.close()
            except Exception:
                pass
        finally:
            try:
                await pw.stop()
            except Exception:
                pass
            self.opening = False

    # ── Headless screenshot-based login (deployment / no display) ────────────

    async def start(self) -> None:
        if self.active:
            return
        self._pw = await async_playwright().start()
        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=_BASE_PROFILE,
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
            viewport={"width": 1280, "height": 720},
            ignore_https_errors=True,
        )
        self._page = await self._context.new_page()
        await self._page.goto("https://accounts.google.com/ServiceLogin")
        self.active = True
        logger.info("Headless login session started")

    async def screenshot_b64(self) -> str:
        if not self._page:
            raise RuntimeError("No active login session")
        data = await self._page.screenshot(type="jpeg", quality=75)
        return base64.b64encode(data).decode()

    async def current_url(self) -> str:
        return self._page.url if self._page else ""

    async def click(self, x: int, y: int) -> None:
        if not self._page:
            raise RuntimeError("No active login session")
        await self._page.mouse.click(x, y)
        await asyncio.sleep(0.6)

    async def type_text(self, text: str) -> None:
        if not self._page:
            raise RuntimeError("No active login session")
        await self._page.keyboard.type(text, delay=60)

    async def press_key(self, key: str) -> None:
        if not self._page:
            raise RuntimeError("No active login session")
        await self._page.keyboard.press(key)
        await asyncio.sleep(0.8)

    async def navigate(self, url: str) -> None:
        if not self._page:
            raise RuntimeError("No active login session")
        await self._page.goto(url, wait_until="domcontentloaded")

    async def abort(self) -> None:
        """Close the headless browser WITHOUT saving session (cancel path)."""
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._page = None
        self._context = None
        self._pw = None
        self.active = False
        logger.info("Headless login session aborted (no session saved)")

    async def done(self) -> None:
        """Save session_state.json from the current context, then close."""
        if self._context:
            # ── Save session BEFORE closing ──────────────────────────────────
            try:
                state = await self._context.storage_state()
                _session_file = Path(__file__).parent.parent / "bot" / "session_state.json"
                _session_file.parent.mkdir(parents=True, exist_ok=True)
                _session_file.write_text(json.dumps(state))
                num_cookies = len(state.get("cookies", []))
                logger.info(
                    "Session saved → %s  (%d cookies, %d origins)",
                    _session_file, num_cookies, len(state.get("origins", [])),
                )
            except Exception as exc:
                logger.error("Failed to save session state: %s", exc)
            try:
                await self._context.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._page = None
        self._context = None
        self._pw = None
        self.active = False
        logger.info("Headless login session closed")


login_session = LoginSession()
