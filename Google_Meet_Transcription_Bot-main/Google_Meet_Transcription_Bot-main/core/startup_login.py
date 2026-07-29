"""
Startup auto-login: runs once when the server boots.
If session_state.json is missing but BOT_GOOGLE_EMAIL + BOT_GOOGLE_PASSWORD
are set, performs a headless Google login and saves the session so the bot
can join meetings immediately without needing manual authentication.
"""

import asyncio
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright

from core.logger import get_logger

logger = get_logger("bot")

# idle → running → success | failed | skipped
_startup_login_state: str = "idle"
_startup_login_message: str = ""


def get_startup_login_state() -> dict:
    return {"state": _startup_login_state, "message": _startup_login_message}

_SESSION_FILE = Path(__file__).parent.parent / "bot" / "session_state.json"
_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-extensions",
]


async def run_startup_login() -> None:
    """
    Called from main.py lifespan as a background task.
    Skips silently if session already exists or credentials are not set.
    """
    global _startup_login_state, _startup_login_message

    # BOT_GUEST_MODE=true → skip all auto-login; bot joins meetings as anonymous guest.
    if os.getenv("BOT_GUEST_MODE", "false").lower() == "true":
        _startup_login_state = "skipped"
        _startup_login_message = "BOT_GUEST_MODE=true — skipping auto-login; bot will join as anonymous guest."
        logger.info("Startup login: BOT_GUEST_MODE=true — skipping auto-login")
        return

    if _SESSION_FILE.exists():
        # Check if the session is substantial (≥10 cookies = real login).
        # A stale/guest session has only 2 cookies (restored from an old BOT_SESSION_B64
        # that was created on a different machine/IP). Delete it so we do a fresh login.
        try:
            import time as _time
            _session_data = json.loads(_SESSION_FILE.read_text())
            _cookie_count = len(_session_data.get("cookies", []))
            if _cookie_count < 10:
                logger.warning(
                    "Startup login: session_state.json has only %d cookies — likely stale/guest session. "
                    "Deleting and re-logging in …",
                    _cookie_count,
                )
                _SESSION_FILE.unlink()
            else:
                # Check if critical meet.google.com cookies have expired.
                # The COMPASS cookie controls Meet-specific auth — when it expires the bot
                # loads Meet as a guest, sees "Ask to join" instead of "Join now", and
                # gets stuck in the waiting room until Google kicks it out.
                _now = _time.time()
                _meet_cookies = [
                    c for c in _session_data.get("cookies", [])
                    if "meet.google.com" in c.get("domain", "")
                    and c.get("expires", 0) > 0
                ]
                _expired = [c["name"] for c in _meet_cookies if c["expires"] < _now]
                if _expired:
                    logger.warning(
                        "Startup login: meet.google.com cookie(s) EXPIRED: %s — "
                        "bot would join as guest and get 'Ask to join' instead of 'Join now'. "
                        "Deleting stale session and re-logging in …",
                        ", ".join(_expired),
                    )
                    _SESSION_FILE.unlink()
                else:
                    _startup_login_state = "skipped"
                    _startup_login_message = f"Session file exists with {_cookie_count} cookies — login not needed."
                    logger.info(
                        "Startup login: session_state.json exists (%d cookies) — skipping",
                        _cookie_count,
                    )
                    return
        except Exception as _exc:
            logger.warning("Startup login: could not read session file (%s) — proceeding with fresh login", _exc)
            try:
                _SESSION_FILE.unlink()
            except Exception:
                pass

    email = os.getenv("BOT_GOOGLE_EMAIL", "").strip()
    password = os.getenv("BOT_GOOGLE_PASSWORD", "").strip()

    if not email or not password:
        _startup_login_state = "skipped"
        _startup_login_message = "No credentials set — add BOT_GOOGLE_EMAIL + BOT_GOOGLE_PASSWORD to env."
        logger.warning(
            "Startup login: BOT_GOOGLE_EMAIL / BOT_GOOGLE_PASSWORD not set — "
            "skipping auto-login. Bot will need manual login before joining meetings."
        )
        return

    _startup_login_state = "running"
    _startup_login_message = f"Logging in as {email} …"

    logger.info("=" * 60)
    logger.info("STARTUP LOGIN: beginning headless Google login for %s", email)
    logger.info("=" * 60)

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 720},
                ignore_https_errors=True,
            )
            page = await context.new_page()

            # Step 1: Navigate to Google sign-in.
            # Use ServiceLogin (same flow as meet_bot._auto_login) — it redirects to
            # /v3/signin/identifier which is the only active signin URL as of 2026.
            # The old /signin/identifier URL returns a 404.
            logger.info("Startup login [1/5]: navigating to Google sign-in …")
            await page.goto(
                "https://accounts.google.com/ServiceLogin?flowName=GlifWebSignIn&flowEntry=ServiceLogin",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            # Wait for network to settle so JS-rendered inputs have time to appear
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            await asyncio.sleep(2)
            _post_nav_url = page.url
            logger.info("Startup login [1/5]: post-navigation URL: %s", _post_nav_url)
            try:
                _ss_path_step1 = Path(__file__).parent.parent / "uploads" / "startup_step1_signin.png"
                _ss_path_step1.parent.mkdir(exist_ok=True)
                await page.screenshot(path=str(_ss_path_step1))
                logger.info("Startup login [1/5]: screenshot saved → %s", _ss_path_step1)
            except Exception:
                pass

            # Step 2: Enter email
            # Google sometimes renders the email field under different selectors
            logger.info("Startup login [2/5]: entering email …")
            _EMAIL_SELECTORS = [
                'input[type="email"]',
                'input[name="identifier"]',
                '#identifierId',
            ]
            try:
                _email_el = None
                for _sel in _EMAIL_SELECTORS:
                    try:
                        _email_el = await page.wait_for_selector(_sel, timeout=8_000)
                        if _email_el:
                            logger.info("Startup login [2/5]: found email field via selector: %s", _sel)
                            break
                    except Exception:
                        continue
                if not _email_el:
                    raise RuntimeError("No email input found with any known selector")
                await _email_el.fill(email)
                await page.keyboard.press("Enter")
                await asyncio.sleep(3)
            except Exception as exc:
                _page_url_at_fail = page.url
                _page_text = ""
                try:
                    _page_text = await page.evaluate("() => document.body ? document.body.innerText.slice(0, 500) : ''")
                except Exception:
                    pass
                logger.error(
                    "Startup login [2/5]: email field not found — %s  URL: %s  Page text: %s",
                    exc, _page_url_at_fail, _page_text,
                )
                _startup_login_state = "failed"
                _startup_login_message = "Login failed: email field not found. Check if Google login page changed."
                await _cleanup(context, browser)
                return

            # Step 3: Enter password
            logger.info("Startup login [3/5]: entering password …")
            _PWD_SELECTORS = [
                'input[type="password"]',
                'input[name="Passwd"]',
                '#password input',
            ]
            try:
                _pwd_el = None
                for _sel in _PWD_SELECTORS:
                    try:
                        _pwd_el = await page.wait_for_selector(_sel, timeout=8_000)
                        if _pwd_el:
                            logger.info("Startup login [3/5]: found password field via selector: %s", _sel)
                            break
                    except Exception:
                        continue
                if not _pwd_el:
                    raise RuntimeError("No password input found with any known selector")
                await _pwd_el.fill(password)
                await page.keyboard.press("Enter")
                await asyncio.sleep(4)
            except Exception as exc:
                logger.error(
                    "Startup login [3/5]: password field not found — %s  "
                    "Possible: wrong email, Google blocked the attempt, or 2FA required",
                    exc,
                )
                _startup_login_state = "failed"
                _startup_login_message = "Login failed: password field not found. Wrong email or Google blocked the attempt."
                await _cleanup(context, browser)
                return

            # Step 3b: Screenshot + security challenge detection right after password
            _post_pwd_url = page.url
            logger.info("Startup login [3b]: post-password URL: %s", _post_pwd_url)
            try:
                _ss_path = Path(__file__).parent.parent / "uploads" / "startup_post_password.png"
                _ss_path.parent.mkdir(exist_ok=True)
                await page.screenshot(path=str(_ss_path))
                logger.info("Startup login [3b]: screenshot saved → %s", _ss_path)
            except Exception:
                pass

            if "accounts.google.com" in _post_pwd_url:
                try:
                    _body = await page.evaluate("() => document.body ? document.body.innerText : ''")
                    _body_lower = _body.lower()
                    _challenge_phrases = [
                        "verify it's you", "confirm it's you", "verify your identity",
                        "unusual activity", "this sign-in attempt", "suspicious",
                        "check your phone", "2-step verification", "verification code",
                        "google prompt", "enter the code", "recovery phone",
                    ]
                    _hit = next((p for p in _challenge_phrases if p in _body_lower), None)
                    if _hit:
                        logger.error(
                            "Startup login FAILED: Google security challenge ('%s') — URL: %s  "
                            "Google is blocking this server IP. "
                            "FIX: open the app UI → 'Login Bot to Google' to authenticate manually, "
                            "OR disable 2FA for %s in Google Workspace Admin.",
                            _hit, _post_pwd_url, email,
                        )
                        _startup_login_state = "failed"
                        _startup_login_message = (
                            f"Google security challenge ({_hit}). "
                            "Use 'Login Bot to Google' in the UI to authenticate from the server, "
                            "or disable 2FA for the bot account."
                        )
                        await _cleanup(context, browser)
                        return
                except Exception:
                    pass

            # Step 4: Dismiss post-login prompts (2FA skip, TOS, recovery prompts, etc.)
            logger.info("Startup login [4/5]: dismissing post-login prompts …")
            for _round in range(3):
                _dismissed_any = False
                for selector in [
                    'button:has-text("Not now")',   'button:has-text("Skip")',
                    'button:has-text("Done")',       'button:has-text("Continue")',
                    'button:has-text("I agree")',    'button:has-text("Confirm")',
                    'button:has-text("Got it")',     'button:has-text("OK")',
                    'button:has-text("No thanks")',  'button:has-text("Remind me later")',
                    '[aria-label="Dismiss"]',        '[aria-label="Close"]',
                ]:
                    try:
                        el = await page.query_selector(selector)
                        if el and await el.is_visible():
                            await el.click()
                            logger.info("Startup login: dismissed prompt '%s'", selector)
                            await asyncio.sleep(1)
                            _dismissed_any = True
                    except Exception:
                        continue
                if not _dismissed_any:
                    break

            # Step 5: Verify login succeeded
            current_url = page.url
            logger.info("Startup login [5/5]: verifying — current URL: %s", current_url)

            if "accounts.google.com" in current_url and any(
                kw in current_url for kw in ("signin", "challenge", "identifier", "rejected")
            ):
                logger.error(
                    "Startup login FAILED: still on auth/challenge page — URL: %s  "
                    "Causes: wrong password, 2FA enabled, or Google blocked this server's IP. "
                    "Fix: open app UI → 'Login Bot to Google' to authenticate manually, "
                    "OR disable 2FA for %s in Google Workspace Admin.",
                    current_url, email,
                )
                _startup_login_state = "failed"
                _startup_login_message = (
                    "Login failed: Google blocked the attempt or wrong password / 2FA. "
                    "Use 'Login Bot to Google' button to authenticate manually."
                )
                await _cleanup(context, browser)
                return

            # Save session
            state = await context.storage_state()
            _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            _SESSION_FILE.write_text(json.dumps(state))
            num_cookies = len(state.get("cookies", []))
            num_origins = len(state.get("origins", []))

            _startup_login_state = "success"
            _startup_login_message = f"Logged in successfully as {email}."

            logger.info("=" * 60)
            logger.info(
                "STARTUP LOGIN: SUCCESS — session saved to %s  "
                "(%d cookies, %d origins)",
                _SESSION_FILE, num_cookies, num_origins,
            )
            logger.info("=" * 60)

            await _cleanup(context, browser)

    except asyncio.CancelledError:
        logger.info("Startup login: cancelled (server shutting down)")
        if _startup_login_state == "running":
            _startup_login_state = "failed"
            _startup_login_message = "Login cancelled (server shutting down)."
    except Exception as exc:
        logger.error("Startup login UNEXPECTED ERROR: %s", exc, exc_info=True)
        _startup_login_state = "failed"
        _startup_login_message = f"Login failed with unexpected error: {exc}"


async def _cleanup(context, browser) -> None:
    try:
        await context.close()
    except Exception:
        pass
    try:
        await browser.close()
    except Exception:
        pass
