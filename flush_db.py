"""
Clears all data from tables (TRUNCATE). Tables and schema are kept intact.
USE WITH CAUTION — all rows will be permanently deleted.

Usage:
    python flush_db.py
    python flush_db.py --confirm   # skip the prompt
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text  # noqa: E402 — must follow load_dotenv()
from core.database import engine  # noqa: E402 — must follow load_dotenv()

# Order matters — child tables first to avoid FK constraint errors
_TABLES = [
    "audit_logs",
    "transcript_segments",
    "participants",
    "meetings",
]


async def flush() -> None:
    print("[flush_db] WARNING: This will DELETE all rows from all tables.")
    print("[flush_db] Tables will NOT be dropped — only data is cleared.")
    if "--confirm" not in sys.argv:
        answer = input("[flush_db] Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            print("[flush_db] Aborted.")
            return

    async with engine.begin() as conn:
        await conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in _TABLES:
            await conn.execute(text(f"TRUNCATE TABLE `{table}`"))
            print(f"[flush_db] Cleared: {table}")
        await conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

    print("[flush_db] All data cleared. Tables intact.")


if __name__ == "__main__":
    asyncio.run(flush())
