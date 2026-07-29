"""
Run this script ONCE to authenticate the bot's Google account.

    python -m bot.login_once

A Chromium window opens. Log in with your bot's Google account
(set BOT_GOOGLE_EMAIL / BOT_GOOGLE_PASSWORD in .env — never hardcode
credentials here), then wait for the Google Meet lobby to appear.
Press ENTER here. The session is saved to bot/bot_browser_profile/
and bot/session_state.json.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright

PROFILE_DIR  = str(Path(__file__).parent / "bot_browser_profile")
SESSION_FILE = Path(__file__).parent / "session_state.json"


async def main() -> None:
    print("=" * 60)
    print("  Google Account — One-Time Login")
    print("=" * 60)
    print(f"  Profile: {PROFILE_DIR}")
    print()
    print("  Steps:")
    print("  1. A Chrome window will open at Google Sign-In.")
    print("  2. Sign in as:  <your bot's Google account email>")
    print("  3. Password:    <your bot's Google account password>")
    print("  4. Once you see the Google account home page, come back here.")
    print("  5. Press ENTER — session will be saved automatically.")
    print("=" * 60)
    print()

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1280,720",
            ],
        )

        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://accounts.google.com/ServiceLogin", wait_until="domcontentloaded")

        print("Browser opened. Log in, then press ENTER here …")
        await asyncio.get_running_loop().run_in_executor(None, input, "")

        # Save full storage state (cookies + localStorage)
        state = await context.storage_state()
        google_cookies = [
            c for c in state["cookies"]
            if any(d in c.get("domain", "") for d in ("google.com", "youtube.com", "google.co"))
        ]
        state["cookies"] = google_cookies
        SESSION_FILE.write_text(json.dumps(state, indent=2))
        num_cookies = len(google_cookies)
        num_ls = sum(len(o.get("localStorage", [])) for o in state.get("origins", []))

        await context.close()

    print()
    print(f"Session saved → {SESSION_FILE}")
    print(f"  {num_cookies} cookies   {num_ls} localStorage entries")
    print()
    print("You can now run the bot:")
    print("    python -m bot.bot_runner <meet-url>")


if __name__ == "__main__":
    asyncio.run(main())
