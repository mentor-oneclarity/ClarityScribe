"""
Entry point for the meeting bot.

Usage:
    python -m bot.bot_runner https://meet.google.com/abc-defg-hij
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from bot.bots.meet_bot import MeetBot
from bot.utils.logger import get_logger

load_dotenv()

logger = get_logger("bot_runner")


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m bot.bot_runner <google-meet-url>")
        print("Example: python -m bot.bot_runner https://meet.google.com/abc-defg-hij")
        sys.exit(1)

    meet_url = sys.argv[1]
    if "meet.google.com" not in meet_url:
        logger.error("URL does not look like a Google Meet link: %s", meet_url)
        sys.exit(1)

    logger.info("Bot starting for: %s", meet_url)
    bot = MeetBot(meet_url)

    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Bot interrupted by user")
    except Exception as exc:
        logger.error("Bot crashed: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
