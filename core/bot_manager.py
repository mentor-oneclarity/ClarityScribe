import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("bot")

_MAX_RESTARTS = 2          # max auto-restarts per meeting code
_RESTART_DELAY = 10        # seconds to wait before restarting
_MIN_RUNTIME_SEC = 30      # don't restart if bot ran <30s (likely a join failure, not a crash)

_BASE_PROFILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "bot", "bot_browser_profile")
)

def is_authenticated() -> bool:
    # Guest mode: no Google auth needed — bot joins as anonymous guest
    if os.getenv("BOT_GUEST_MODE", "false").lower() == "true":
        logger.debug("Auth check: BOT_GUEST_MODE=true — skipping auth (guest join)")
        return True
    # CI/CD path: session restored from BOT_SESSION_B64
    session = Path(_BASE_PROFILE).parent / "session_state.json"
    if session.exists():
        logger.debug("Auth check: session_state.json found — authenticated via saved session")
        return True
    # Local dev path: persistent profile with Cookies file
    cookies = Path(_BASE_PROFILE) / "Default" / "Cookies"
    if cookies.exists():
        logger.debug("Auth check: Cookies file found — authenticated via local profile")
        return True
    # Auto-login path: credentials available — bot will login automatically
    if os.getenv("BOT_GOOGLE_EMAIL") and os.getenv("BOT_GOOGLE_PASSWORD"):
        logger.debug("Auth check: BOT_GOOGLE_EMAIL + BOT_GOOGLE_PASSWORD set — will auto-login when joining")
        return True
    logger.warning("Auth check: FAILED — no session file, no Cookies, no credentials set")
    return False


def auth_status() -> dict:
    from core.startup_login import get_startup_login_state

    session = Path(_BASE_PROFILE).parent / "session_state.json"
    session_exists = session.exists()
    profile_exists = os.path.exists(_BASE_PROFILE)
    logged_in = is_authenticated()
    login_info = get_startup_login_state()

    guest_mode = os.getenv("BOT_GUEST_MODE", "false").lower() == "true"
    if guest_mode:
        detail = "guest"
        message = "Guest mode — bot joins as anonymous (host must admit it)."
    elif session_exists:
        detail = "session"
        message = "Bot is logged in (active session saved)."
    elif logged_in:
        detail = "credentials"
        message = login_info["message"] or "Credentials set — bot will auto-login when joining a meeting."
    else:
        detail = "none"
        message = "Bot is NOT logged in. Add BOT_GOOGLE_EMAIL + BOT_GOOGLE_PASSWORD to env."

    email = os.getenv("BOT_GOOGLE_EMAIL", "")
    logger.debug(
        "Auth status: authenticated=%s session_exists=%s profile_exists=%s detail=%s login_state=%s",
        logged_in, session_exists, profile_exists, detail, login_info["state"],
    )
    return {
        "authenticated": logged_in,
        "session_exists": session_exists,
        "profile_exists": profile_exists,
        "auth_detail": detail,
        "login_state": login_info["state"],
        "message": message,
        "email": email,
    }


def _profile_for(meet_code: str) -> str:
    # Use the base profile directly — no per-meeting clone.
    # Cloning causes Chrome to land on a fresh profile slot (Profile 3, etc.)
    # instead of Default, so the bot has no valid Google session at launch.
    logger.info("Using base profile for %s (no clone)", meet_code)
    return _BASE_PROFILE


class BotManager:
    # Maximum simultaneous bots.
    # Each Playwright Chromium uses ~400-500 MB RAM.
    # Azure B2s (4 GB): safe max = 4  |  B4ms (16 GB): safe max = 12
    # Read from env so ops can tune without a code change.
    _MAX_CONCURRENT: int = int(os.getenv("BOT_MAX_CONCURRENT", "4"))

    def __init__(self) -> None:
        self._tasks: Dict[str, asyncio.Task] = {}
        self._restart_counts: Dict[str, int] = {}
        self._start_times: Dict[str, float] = {}
        # Store (meet_url, title, organizer_email) for restart
        self._bot_args: Dict[str, tuple] = {}
        # Live bot objects, so stop() can request a GRACEFUL shutdown (which runs
        # the full teardown: end_meeting + summary + email) instead of a hard
        # cancel that interrupts teardown mid-way.
        self._bots: Dict[str, object] = {}

    async def join(self, meet_url: str, title: Optional[str] = None, organizer_email: Optional[str] = None, attendee_emails: Optional[str] = None) -> Optional[str]:
        from bot.bots.meet_bot import MeetBot

        if not is_authenticated():
            raise RuntimeError(
                "Bot is not authenticated. Add BOT_GOOGLE_EMAIL + BOT_GOOGLE_PASSWORD to env "
                "and restart the server, or use 'Login Bot to Google' in the UI."
            )

        meet_code = MeetBot._extract_code(meet_url)
        task = self._tasks.get(meet_code)
        if task and not task.done():
            logger.info("Bot already running for %s — skipping", meet_code)
            return None

        # ── Concurrency guard ──────────────────────────────────────────────────
        active_count = len(self.active())
        if active_count >= self._MAX_CONCURRENT:
            logger.warning(
                "Concurrency limit reached (%d/%d active) — rejecting join for %s. "
                "Increase BOT_MAX_CONCURRENT env var or wait for a bot to finish.",
                active_count, self._MAX_CONCURRENT, meet_code,
            )
            raise RuntimeError(
                f"Bot capacity reached ({active_count}/{self._MAX_CONCURRENT} meetings active). "
                "Try again in a few minutes."
            )

        self._bot_args[meet_code] = (meet_url, title, organizer_email, attendee_emails)
        self._restart_counts.setdefault(meet_code, 0)
        await self._launch(meet_code)
        logger.info(
            "Bot launched for %s — now running %d/%d bots",
            meet_code, len(self.active()), self._MAX_CONCURRENT,
        )
        return meet_code

    async def _launch(self, meet_code: str) -> None:
        from bot.bots.meet_bot import MeetBot

        meet_url, title, organizer_email, attendee_emails = self._bot_args[meet_code]
        profile = _profile_for(meet_code)
        bot = MeetBot(meet_url, profile_dir=profile, title=title, organizer_email=organizer_email, attendee_emails=attendee_emails)
        t = asyncio.create_task(bot.run(), name=f"bot-{meet_code}")
        self._start_times[meet_code] = time.monotonic()
        self._tasks[meet_code] = t
        self._bots[meet_code] = bot

        def _on_done(task: asyncio.Task) -> None:
            runtime = time.monotonic() - self._start_times.get(meet_code, 0)
            if task.cancelled():
                logger.info("Bot task for %s was cancelled (runtime=%.0fs)", meet_code, runtime)
                return
            if exc := task.exception():
                logger.error("Bot task for %s CRASHED after %.0fs: %s",
                             meet_code, runtime, exc, exc_info=exc)
                restarts = self._restart_counts.get(meet_code, 0)
                if runtime >= _MIN_RUNTIME_SEC and restarts < _MAX_RESTARTS:
                    self._restart_counts[meet_code] = restarts + 1
                    logger.warning(
                        "Watchdog: scheduling restart %d/%d for %s in %ds …",
                        restarts + 1, _MAX_RESTARTS, meet_code, _RESTART_DELAY,
                    )
                    asyncio.get_running_loop().call_later(
                        _RESTART_DELAY,
                        lambda: asyncio.ensure_future(self._launch(meet_code))
                    )
                elif runtime < _MIN_RUNTIME_SEC:
                    logger.warning(
                        "Watchdog: bot for %s crashed too fast (%.0fs < %ds) — not restarting",
                        meet_code, runtime, _MIN_RUNTIME_SEC,
                    )
                else:
                    logger.error(
                        "Watchdog: max restarts (%d) reached for %s — giving up",
                        _MAX_RESTARTS, meet_code,
                    )
            else:
                logger.info("Bot task for %s finished cleanly (runtime=%.0fs)", meet_code, runtime)
                # Reset restart counter on clean exit so future manual joins start fresh
                self._restart_counts[meet_code] = 0

        t.add_done_callback(_on_done)
        logger.info("Bot launched for %s (restart#%d)", meet_code,
                    self._restart_counts.get(meet_code, 0))

    def active(self) -> list[str]:
        return [code for code, t in self._tasks.items() if not t.done()]

    def status(self) -> dict:
        out = {}
        for code, t in self._tasks.items():
            if not t.done():
                out[code] = "running"
            elif t.cancelled():
                out[code] = "cancelled"
            elif t.exception():
                out[code] = "error"
            else:
                out[code] = "done"
        return out

    async def stop(self, meet_code: str) -> bool:
        task = self._tasks.get(meet_code)
        if not task or task.done():
            return False

        # GRACEFUL stop: signal the bot to finish its poll loops, which lets the
        # `finally: _teardown()` run end_meeting + summary + email to completion.
        # A hard task.cancel() would interrupt teardown's awaits (the STT-flush
        # sleep and the summarize call), so the meeting would stay "active" and no
        # summary email would be sent — exactly the bug we're fixing.
        bot = self._bots.get(meet_code)
        if bot is not None and hasattr(bot, "request_stop"):
            logger.info("Stop requested for %s — graceful shutdown (teardown + summary will run)", meet_code)
            bot.request_stop()
            # Don't block the HTTP response on the full teardown (it includes an
            # STT-flush wait + AI summary). Give it a brief head start, then let it
            # finish in the background; the done-callback marks completion.
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=8.0)
            except asyncio.TimeoutError:
                logger.info("Bot %s still tearing down (summary in progress) — continuing in background", meet_code)
            except asyncio.CancelledError:
                pass
            return True

        # Fallback: no bot handle → hard cancel (best effort).
        logger.warning("Stop for %s — no bot handle, hard-cancelling (teardown may not complete)", meet_code)
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        return True

    async def stop_all(self) -> None:
        for code in list(self.active()):
            await self.stop(code)


bot_manager = BotManager()
