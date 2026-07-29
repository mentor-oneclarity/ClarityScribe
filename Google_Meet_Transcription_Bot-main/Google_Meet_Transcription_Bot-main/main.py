import asyncio
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from core.logger import backend_logger, LOG_LEVEL

load_dotenv()

logger = backend_logger

_MEET_URL_RE = re.compile(r"https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}", re.IGNORECASE)
_CAL_JOIN_AHEAD_SEC = 120   # join 2 min before meeting starts
_CAL_POLL_SEC       = 60    # check calendar every 60s


def _fetch_upcoming_meet_events(token_file: Path) -> list[dict]:
    """Blocking — run in executor. Returns Meet events starting within the next 2 min."""
    from google.auth.transport.requests import Request as GRequest
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(
        str(token_file), ["https://www.googleapis.com/auth/calendar.readonly"]
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(GRequest())
        token_file.write_text(creds.to_json())

    service = build("calendar", "v3", credentials=creds)
    now = datetime.now(timezone.utc)
    result = service.events().list(
        calendarId="primary",
        timeMin=now.isoformat(),
        timeMax=(now + timedelta(seconds=_CAL_JOIN_AHEAD_SEC + _CAL_POLL_SEC)).isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=10,
    ).execute()

    upcoming = []
    for event in result.get("items", []):
        # Find Google Meet URL in conferenceData or description/location
        meet_url = None
        for ep in event.get("conferenceData", {}).get("entryPoints", []):
            if ep.get("entryPointType") == "video" and "meet.google.com" in ep.get("uri", ""):
                meet_url = ep["uri"]
                break
        if not meet_url:
            for field in ("description", "location", "summary"):
                m = _MEET_URL_RE.search(event.get(field, "") or "")
                if m:
                    meet_url = m.group(0)
                    break
        if not meet_url:
            continue

        # Parse start time
        start_str = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date", "")
        if not start_str:
            continue
        try:
            start_dt = datetime.fromisoformat(start_str)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        # Only include events starting within the join window
        secs_until_start = (start_dt - now).total_seconds()
        if not (-60 <= secs_until_start <= _CAL_JOIN_AHEAD_SEC):
            continue

        # Extract organizer and attendees
        organizer_email = event.get("organizer", {}).get("email")
        attendees = [
            a["email"] for a in event.get("attendees", [])
            if a.get("email") and not a.get("self") and a.get("responseStatus") != "declined"
        ]

        upcoming.append({
            "event_id": event["id"],
            "title":    event.get("summary", "Untitled Meeting"),
            "meet_url": meet_url,
            "organizer_email": organizer_email,
            "attendee_emails": ",".join(attendees) if attendees else None,
        })

    return upcoming


async def _calendar_autojoin_loop(bot_manager) -> None:
    """Polls Google Calendar every 60s and auto-joins upcoming Meet events."""
    token_file = Path(__file__).parent / "bot" / "calendar_token.json"
    joined_event_ids: set[str] = set()

    logger.info("Calendar auto-join loop started (poll every %ds)", _CAL_POLL_SEC)

    while True:
        await asyncio.sleep(_CAL_POLL_SEC)

        if not token_file.exists():
            continue

        try:
            events = await asyncio.get_running_loop().run_in_executor(
                None, _fetch_upcoming_meet_events, token_file
            )
        except Exception as exc:
            logger.warning("Calendar poll failed (non-fatal): %s", exc)
            continue

        for ev in events:
            eid = ev["event_id"]
            if eid in joined_event_ids:
                continue
            joined_event_ids.add(eid)
            # Clear old entries so the set doesn't grow forever
            if len(joined_event_ids) > 500:
                joined_event_ids.clear()

            logger.info(
                "Calendar auto-join: '%s' → %s (organizer=%s attendees=%s)",
                ev["title"], ev["meet_url"], ev["organizer_email"], ev["attendee_emails"],
            )
            try:
                await bot_manager.join(
                    meet_url=ev["meet_url"],
                    title=ev["title"],
                    organizer_email=ev["organizer_email"],
                    attendee_emails=ev["attendee_emails"],
                )
            except Exception as exc:
                logger.error("Calendar auto-join failed for %s: %s", ev["meet_url"], exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Tables are managed by setup_db.py — skipping create_all here avoids a
    # slow Azure MySQL round-trip that delays startup and causes health-check failures.
    logger.info("Database tables ready")

    # Restore bot session from env var (for Docker / CI deployments)
    _sess_b64 = os.getenv("BOT_SESSION_B64", "").strip()
    if _sess_b64:
        import base64 as _b64mod
        _sess_path = Path("bot/session_state.json")
        if not _sess_path.exists():
            _sess_path.parent.mkdir(parents=True, exist_ok=True)
            _sess_path.write_bytes(_b64mod.b64decode(_sess_b64))
            logger.info("Bot session restored from BOT_SESSION_B64")
        else:
            logger.info("Bot session already exists — skipping BOT_SESSION_B64 restore")

    # On startup: mark any stale "active"/"pending" meetings as completed.
    # These are left over from a previous container crash or SIGKILL where
    # _teardown() never ran and end_meeting() was never called.
    try:
        from datetime import datetime as _dt
        from sqlalchemy import update as _update
        from core.database import Meeting as _Meeting, async_session as _async_session
        async with _async_session() as _sess:
            _result = await _sess.execute(
                _update(_Meeting)
                .where(_Meeting.status.in_(["active", "pending"]))
                .values(status="completed", ended_at=_dt.utcnow())
            )
            await _sess.commit()
            if _result.rowcount:
                logger.warning(
                    "Startup cleanup: marked %d stale meeting(s) as completed "
                    "(left over from previous crash/restart)",
                    _result.rowcount,
                )
            else:
                logger.info("Startup cleanup: no stale meetings found")
    except Exception as _exc:
        logger.error("Startup cleanup failed (non-fatal): %s", _exc)

    from core.bot_manager import bot_manager
    from core.startup_login import run_startup_login

    login_task   = asyncio.create_task(run_startup_login(), name="startup-login")
    cal_task     = asyncio.create_task(_calendar_autojoin_loop(bot_manager), name="calendar-autojoin")

    yield

    for _t in (login_task, cal_task):
        _t.cancel()
        try:
            await _t
        except asyncio.CancelledError:
            pass

    await bot_manager.stop_all()
    logger.info("All bots stopped")


app = FastAPI(title="AI Meeting Recorder", version="1.0.0", lifespan=lifespan)

_allowed_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if LOG_LEVEL == "DEBUG":
    @app.middleware("http")
    async def _log_requests(request: Request, call_next):
        t0 = time.perf_counter()
        logger.debug("→ %s %s  params=%s", request.method, request.url.path, dict(request.query_params))
        response = await call_next(request)
        ms = (time.perf_counter() - t0) * 1000
        logger.debug("← %s %s  status=%d  %.1fms", request.method, request.url.path, response.status_code, ms)
        return response

from API.routes import router
app.include_router(router)

# Serve frontend — must be mounted AFTER API routes
_frontend = Path(__file__).parent / "frontend"
if _frontend.exists():
    app.mount("/static", StaticFiles(directory=str(_frontend)), name="static")

    @app.get("/", include_in_schema=False)
    async def serve_index():
        return FileResponse(str(_frontend / "index.html"))
