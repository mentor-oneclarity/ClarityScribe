# build: batch-migrations-v2
"""Creates all database tables. Safe to run multiple times (IF NOT EXISTS)."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from core.database import Base, engine, _db_host, _db_port, _db_name, _db_user, _db_password, _connect_args


async def _ensure_database() -> None:
    """Create the database if it doesn't exist (connects without a DB first)."""
    if not (_db_host and _db_name and _db_user):
        return  # SQLite mode — nothing to do

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.engine.url import URL
    from sqlalchemy import text

    no_db_url = URL.create(
        drivername="mysql+aiomysql",
        username=_db_user,
        password=_db_password,
        host=_db_host,
        port=int(_db_port),
    )
    tmp_engine = create_async_engine(no_db_url, connect_args=_connect_args)
    try:
        async with tmp_engine.begin() as conn:
            await conn.execute(
                text(f"CREATE DATABASE IF NOT EXISTS `{_db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            )
        print(f"[setup_db] Database '{_db_name}' created")
    finally:
        await tmp_engine.dispose()


async def _migrate_columns() -> None:
    """Add missing columns to existing tables — uses ONE query for all checks."""
    from sqlalchemy import text

    migrations = [
        # (table, column, definition)
        ("ai_gm_meetings", "attendee_emails", "TEXT"),
        ("ai_gm_meetings", "organizer_email", "VARCHAR(255)"),
        # Speaker attribution metadata
        ("ai_transcript_segments", "speaker_source", "VARCHAR(50) NULL"),
        ("ai_transcript_segments", "speaker_confidence", "DECIMAL(5,4) NULL"),
        ("ai_transcript_segments", "alignment_overlap_ms", "INT NULL"),
        ("ai_transcript_segments", "is_overlapping_speech", "BOOLEAN DEFAULT FALSE"),
        ("ai_transcript_segments", "secondary_speakers_json", "JSON NULL"),
        ("ai_transcript_segments", "confidence", "VARCHAR(10) NOT NULL DEFAULT 'high'"),
        # Language and translation
        ("ai_transcript_segments", "language_code", "VARCHAR(20) NULL"),
        ("ai_transcript_segments", "translated_text", "TEXT NULL"),
        ("ai_transcript_segments", "translated_language_code", "VARCHAR(10) NULL"),
        # Hand-raise + in-call chat capture
        ("ai_meeting_master", "hand_raises", "JSON NULL"),
        ("ai_meeting_master", "chat_messages", "JSON NULL"),
    ]

    tables = list({t for t, _, _ in migrations})

    async with engine.begin() as conn:
        # Single batch query instead of 7 individual round-trips to Azure MySQL
        placeholders = ", ".join(f"'{t}'" for t in tables)
        result = await conn.execute(text(f"""
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
            AND table_name IN ({placeholders})
        """))
        existing = {(row[0], row[1]) for row in result.fetchall()}

        for table, column, col_def in migrations:
            if (table, column) not in existing:
                try:
                    await conn.execute(text(
                        f"ALTER TABLE `{table}` ADD COLUMN `{column}` {col_def}"
                    ))
                    print(f"[setup_db] Added column {table}.{column}")
                except Exception as e:
                    print(f"[setup_db] Migration {table}.{column}: {e}")
            else:
                print(f"[setup_db] Column {table}.{column} already exists ✓")


async def main() -> None:
    from sqlalchemy import text
    print("[setup_db] Connecting to database...")

    # Try connecting with the full DB URL first — if it works the database exists
    # and we skip _ensure_database() (which needs a separate no-DB connection).
    db_exists = False
    if _db_host and _db_name and _db_user:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            db_exists = True
            print(f"[setup_db] Database '{_db_name}' ready")
        except Exception:
            pass

    if not db_exists:
        await _ensure_database()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _migrate_columns()
    await engine.dispose()
    print("[setup_db] Tables created/verified:")
    for table in Base.metadata.sorted_tables:
        print(f"  - {table.name}")
    print("[setup_db] Done.")


if __name__ == "__main__":
    asyncio.run(main())
