import logging
import os
import socket
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import JSON, Boolean, Column, Date, DateTime, Float, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

load_dotenv()

_log = logging.getLogger("db")

_db_host = os.getenv("DB_HOST", "").strip()
_db_port = os.getenv("DB_PORT", "3306").strip()
_db_name = os.getenv("DB_NAME", "").strip()
_db_user = os.getenv("DB_USER", "").strip().split()[0] if os.getenv("DB_USER", "").strip() else ""
_db_password = os.getenv("DB_PASSWORD", "").strip()

_use_sqlite = False
_connect_args: dict = {}

_require_mysql: bool = os.getenv("REQUIRE_MYSQL", "false").lower() == "true"

if _db_host and _db_name and _db_user:
    # Quick TCP probe before building the async engine.
    # Set REQUIRE_MYSQL=true in production .env to fail loudly instead of falling
    # back to SQLite (which loses data on container restart).
    try:
        _sock = socket.create_connection((_db_host, int(_db_port)), timeout=4)
        _sock.close()
        _mysql_ok = True
    except OSError as _tcp_err:
        if _require_mysql:
            raise RuntimeError(
                f"REQUIRE_MYSQL=true but MySQL {_db_host}:{_db_port} is unreachable: "
                f"{_tcp_err}. Fix DB_HOST/.env or Azure firewall rules."
            ) from _tcp_err
        _log.warning(
            "MySQL %s:%s unreachable (%s) — falling back to local SQLite. "
            "Set REQUIRE_MYSQL=true in production to prevent this silent fallback.",
            _db_host, _db_port, _tcp_err,
        )
        _mysql_ok = False

    if _mysql_ok:
        from sqlalchemy.engine.url import URL as _URL
        DATABASE_URL = _URL.create(
            drivername="mysql+aiomysql",
            username=_db_user,
            password=_db_password,
            host=_db_host,
            port=int(_db_port),
            database=_db_name,
        )
        if "azure.com" in _db_host:
            import ssl as _ssl
            _ssl_ctx = _ssl.create_default_context()
            # Set AZURE_MYSQL_SSL_NO_VERIFY=true only if using a self-signed cert.
            # Default is to verify — don't disable in production without a reason.
            if os.getenv("AZURE_MYSQL_SSL_NO_VERIFY", "false").lower() == "true":
                _ssl_ctx.check_hostname = False
                _ssl_ctx.verify_mode = _ssl.CERT_NONE
                _log.warning(
                    "AZURE_MYSQL_SSL_NO_VERIFY=true — TLS cert verification disabled. "
                    "Set to false in production to prevent MITM attacks."
                )
            _connect_args = {"ssl": _ssl_ctx, "connect_timeout": 10}
        else:
            _connect_args = {"connect_timeout": 10}
    else:
        _use_sqlite = True
else:
    _log.warning(
        "DB_HOST / DB_NAME / DB_USER not fully configured — using local SQLite. "
        "Set these in .env to use MySQL."
    )
    _use_sqlite = True

if _use_sqlite:
    _sqlite_dir = Path(__file__).parent.parent / "uploads"
    _sqlite_dir.mkdir(exist_ok=True)
    DATABASE_URL = f"sqlite+aiosqlite:///{_sqlite_dir / 'local_meetings.db'}"
    _log.info("Database: SQLite → %s", DATABASE_URL)
    engine = create_async_engine(DATABASE_URL, echo=False, future=True)
else:
    _log.info("Database: MySQL → %s:%s/%s", _db_host, _db_port, _db_name)
    _pool_size = int(os.getenv("DB_POOL_SIZE", "5"))
    _max_overflow = int(os.getenv("DB_MAX_OVERFLOW", "10"))
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        future=True,
        connect_args=_connect_args,
        pool_pre_ping=False,
        pool_recycle=1800,
        pool_timeout=15,
        pool_size=_pool_size,
        max_overflow=_max_overflow,
    )
async_session: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


class Meeting(Base):
    __tablename__ = "ai_gm_meetings"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=True)
    meet_code = Column(String(100), nullable=False, index=True)
    status = Column(String(20), default="pending")  # pending | active | completed
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    ended_at = Column(DateTime, nullable=True)
    summary = Column(Text, nullable=True)
    organizer_email = Column(String(255), nullable=True)   # summary TO: email (meeting host)
    attendee_emails = Column(Text, nullable=True)           # summary BCC: comma-separated attendee emails

class TranscriptSegment(Base):
    __tablename__ = "ai_transcript_segments"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, nullable=False, index=True)
    speaker_name = Column(String(200), nullable=True)
    text = Column(Text, nullable=False)
    source = Column(String(50), default="caption")
    start_time = Column(Float, nullable=True)
    end_time = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Speaker attribution metadata
    speaker_source = Column(String(50), nullable=True)           # caption | mixed_caption | active_dom | webrtc | track_cache
    speaker_confidence = Column(Numeric(5, 4), nullable=True)    # 0.0000–1.0000
    alignment_overlap_ms = Column(Integer, nullable=True)         # caption overlap in ms
    is_overlapping_speech = Column(Boolean, default=False, nullable=True)
    secondary_speakers_json = Column(JSON, nullable=True)         # [{speaker, overlap_ms}, ...]
    confidence = Column(String(10), default="high", nullable=True) # "high" | "low" (crosstalk flag)
    # Language and translation
    language_code = Column(String(20), nullable=True)             # e.g. en-IN, mr-IN, hi-IN
    translated_text = Column(Text, nullable=True)                 # English translation (null if already English)
    translated_language_code = Column(String(10), nullable=True)  # always "en" when set


class Participant(Base):
    __tablename__ = "ai_participants"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, nullable=False, index=True)
    name = Column(String(200), nullable=False)
    is_active = Column(Boolean, default=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    entity_type = Column(String(50), nullable=True)
    entity_id = Column(Integer, nullable=True)
    action = Column(String(20), nullable=True)
    changes = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class MeetingMaster(Base):
    __tablename__ = "ai_meeting_master"
    __table_args__ = (
        UniqueConstraint("meeting_id", name="uq_meeting_master_meeting_id"),
    )

    id                  = Column(Integer, primary_key=True, index=True)
    meeting_id          = Column(Integer, nullable=False, index=True)
    meet_code           = Column(String(100), nullable=False, index=True)
    title               = Column(String(255), nullable=True)
    meeting_status      = Column(String(20), nullable=True)
    meeting_started_at  = Column(DateTime, nullable=True)
    meeting_ended_at    = Column(DateTime, nullable=True)
    meeting_date        = Column(Date, nullable=True, index=True)
    organizer_email     = Column(String(255), nullable=True)
    attendee_emails     = Column(Text, nullable=True)
    summary             = Column(Text, nullable=True)
    user_id             = Column(String(36), nullable=True, index=True)
    tenant_id           = Column(String(36), nullable=True, index=True)
    person_transcript   = Column(JSON, nullable=True)   # [{speaker, text}, ...]
    hand_raises         = Column(JSON, nullable=True)   # [{speaker, raised_at, lowered_at}, ...]
    chat_messages       = Column(JSON, nullable=True)   # [{sender, text, sent_at}, ...]
    speaker_source      = Column(String(50), nullable=True)
    created_at          = Column(DateTime, default=datetime.utcnow, nullable=False)


async def get_session():
    async with async_session() as session:
        yield session

