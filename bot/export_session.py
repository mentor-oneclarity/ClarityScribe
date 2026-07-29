"""
Run this ONCE locally after logging in:

    python -m bot.export_session

It opens a visible Chrome window, waits for you to confirm you're logged in,
then saves all Google cookies as plain-text JSON to bot/session_state.json.

That file is then base64-encoded and stored as BOT_SESSION_B64 in your
CI/CD secrets (Azure App Settings / GitHub Secrets). The server restores
it automatically on every deploy — no SSH needed.
"""

import asyncio
import base64
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

PROFILE_DIR = str(Path(__file__).parent / "bot_browser_profile")
OUTPUT     = Path(__file__).parent / "session_state.json"


async def main() -> None:
    print("[export_session] Opening Chrome with your saved profile …")
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            args=["--no-sandbox", "--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Navigate to Meet directly so Google issues Meet-verified session cookies.
        # If an account chooser or password prompt appears, complete it in the browser.
        await page.goto("https://meet.google.com/mve-cqyi-ibg")
        await asyncio.sleep(4)

        print()
        print("=" * 60)
        print("Browser opened to Google Meet.")
        print()
        print("  IMPORTANT — do the following in the browser:")
        print("  1. If account chooser appears → click your bot's Google account")
        print("  2. If password prompt appears → enter your bot's Google account password")
        print("  3. Wait until you see the Meet LOBBY (camera/mic preview screen)")
        print("  4. Once you see the lobby — come back here and press ENTER")
        print()
        print("  Do NOT click 'Join' — just reach the lobby screen.")
        print("=" * 60)
        input("\nPress ENTER once you see the Meet lobby screen …")
        print("Saving session …")

        # Export all cookies + localStorage as plain-text JSON
        state = await ctx.storage_state()
        google_cookies = [
            c for c in state["cookies"]
            if "google" in c["domain"] or "youtube" in c["domain"]
        ]
        state["cookies"] = google_cookies

        OUTPUT.write_text(json.dumps(state, indent=2))
        await ctx.close()

    print(f"\n[export_session] Saved {len(google_cookies)} cookies → {OUTPUT}")

    # Print base64 for copy-paste into CI/CD secret
    b64 = base64.b64encode(OUTPUT.read_bytes()).decode()
    print()
    print("=" * 60)
    print("Copy the value below and store it as:")
    print("  Azure App Setting:  BOT_SESSION_B64")
    print("  GitHub Secret:      BOT_SESSION_B64")
    print()
    print(b64)
    print("=" * 60)
    print()
    print("Then redeploy / restart the server — bot will authenticate automatically.")


if __name__ == "__main__":
    asyncio.run(main())
