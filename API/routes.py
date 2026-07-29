import asyncio
import json
import os
import time as _time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from API.models import (
    BotJoinRequest, LoginClickRequest, LoginKeyRequest,
    LoginNavRequest, LoginTypeRequest,
    MeetingCreate, MeetingEmailsUpdate, ParticipantUpsert,
    PushSummaryRequest, SummarizeRequest, TranscriptAdd,
)
from core.bot_manager import auth_status, bot_manager
from core.login_session import login_session
from core.database import AuditLog, Meeting, MeetingMaster, Participant, TranscriptSegment, async_session, get_session
from core.logger import backend_logger
from helpers.summarizer import generate_summary, generate_and_email_summary
from helpers.translator import schedule_translation

logger = backend_logger

router = APIRouter()

# ─── Security & rate limiting ──────────────────────────────────────────────────

_BOT_API_KEY: str = os.getenv("BOT_API_KEY", "").strip()

# In-memory rate-limit buckets: {bucket_key: [timestamp, ...]}
_rl_buckets: dict = defaultdict(list)


def _check_rate_limit(key: str, max_calls: int, window_sec: int = 60) -> None:
    """Raise 429 if more than max_calls have been made within window_sec."""
    now = _time.time()
    calls = _rl_buckets[key]
    _rl_buckets[key] = [t for t in calls if now - t < window_sec]
    if len(_rl_buckets[key]) >= max_calls:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit: max {max_calls} requests per {window_sec}s. Try again shortly.",
        )
    _rl_buckets[key].append(now)


def _require_api_key(x_api_key: str = Header(default="")) -> None:
    """FastAPI dependency — enforces BOT_API_KEY if the env var is set.
    If BOT_API_KEY is not configured the check is skipped (backward compatible)."""
    if _BOT_API_KEY and x_api_key != _BOT_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


# ─── Helpers ───────────────────────────────────────────────────────────────────


def _meeting_dict(m: Meeting) -> dict:
    return {
        "id": m.id,
        "title": m.title,
        "meet_code": m.meet_code,
        "status": m.status,
        "created_at": m.created_at.isoformat(),
        "ended_at": m.ended_at.isoformat() if m.ended_at else None,
        "summary": m.summary,
        "organizer_email": m.organizer_email,
        "attendee_emails": m.attendee_emails,
    }


def _segment_dict(s: TranscriptSegment) -> dict:
    d = {
        "id": s.id,
        "meeting_id": s.meeting_id,
        "speaker_name": s.speaker_name,
        "text": s.text,
        "source": s.source,
        "start_time": s.start_time,
        "end_time": s.end_time,
        "created_at": s.created_at.isoformat(),
    }
    # Language — always include so frontend can show language badge
    d["language_code"] = s.language_code
    d["translated_text"] = s.translated_text
    # Optional attribution fields — only include if present (backward compat)
    if s.speaker_source is not None:
        d["speaker_source"] = s.speaker_source
    if s.speaker_confidence is not None:
        d["speaker_confidence"] = float(s.speaker_confidence)
    if s.alignment_overlap_ms is not None:
        d["alignment_overlap_ms"] = s.alignment_overlap_ms
    if s.is_overlapping_speech is not None:
        d["is_overlapping_speech"] = s.is_overlapping_speech
    if s.secondary_speakers_json is not None:
        d["secondary_speakers_json"] = s.secondary_speakers_json
    if s.confidence is not None:
        d["confidence"] = s.confidence
    return d


async def _get_meeting_or_404(meeting_id: int, session: AsyncSession) -> Meeting:
    result = await session.execute(select(Meeting).where(Meeting.id == meeting_id))
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail=f"Meeting {meeting_id} not found")
    return meeting


# ─── Routes ────────────────────────────────────────────────────────────────────


@router.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@router.get("/debug/db")
async def debug_db():
    """Temporary: shows DB connection status and actual error."""
    from core.database import DATABASE_URL, _use_sqlite
    import traceback
    result = {"db_url": str(DATABASE_URL)[:60], "using_sqlite": _use_sqlite}
    try:
        async with async_session() as session:
            from sqlalchemy import text
            r = await session.execute(text("SELECT COUNT(*) FROM ai_gm_meetings"))
            result["meetings_count"] = r.scalar()
            result["db_ok"] = True
    except Exception as e:
        result["db_ok"] = False
        result["error"] = str(e)
        result["traceback"] = traceback.format_exc()[-500:]
    return result


# ── Meetings ───────────────────────────────────────────────────────────────────


def _sanitize_title(title: str, meet_code: str) -> str:
    """Remove email addresses and junk from calendar event titles."""
    import re as _re
    if not title:
        return f"Meeting {meet_code}"
    # Remove anything that looks like an email address
    cleaned = _re.sub(r'\S+@\S+\.\S+', '', title).strip()
    # Collapse multiple spaces
    cleaned = _re.sub(r'\s{2,}', ' ', cleaned).strip()
    # If title became empty or too short after cleaning, use fallback
    return cleaned if len(cleaned) >= 3 else f"Meeting {meet_code}"


@router.post("/meetings/start", status_code=201)
async def start_meeting(
    body: MeetingCreate, session: AsyncSession = Depends(get_session)
):
    meeting = Meeting(
        title=_sanitize_title(body.title, body.meet_code),
        meet_code=body.meet_code,
        status="active",
        created_at=datetime.utcnow(),
        organizer_email=body.organizer_email or None,
        attendee_emails=body.attendee_emails or None,
    )
    session.add(meeting)
    await session.commit()
    await session.refresh(meeting)
    logger.info("Meeting started: id=%d code=%s", meeting.id, meeting.meet_code)
    try:
        session.add(AuditLog(
            entity_type="meeting", entity_id=meeting.id, action="start",
            changes={"meet_code": meeting.meet_code, "title": meeting.title, "organizer_email": meeting.organizer_email},
        ))
        await session.commit()
    except Exception as _exc:
        logger.warning("Audit log failed for meeting start %d: %s", meeting.id, _exc)
    return _meeting_dict(meeting)


@router.patch("/meetings/{meeting_id}/end")
async def end_meeting(
    meeting_id: int, session: AsyncSession = Depends(get_session)
):
    meeting = await _get_meeting_or_404(meeting_id, session)
    meeting.status = "completed"
    meeting.ended_at = datetime.utcnow()
    await session.commit()
    await session.refresh(meeting)
    logger.info("Meeting ended: id=%d", meeting_id)
    try:
        session.add(AuditLog(
            entity_type="meeting", entity_id=meeting.id, action="end",
            changes={"ended_at": meeting.ended_at.isoformat(), "meet_code": meeting.meet_code},
        ))
        await session.commit()
    except Exception as _exc:
        logger.warning("Audit log failed for meeting end %d: %s", meeting.id, _exc)
    return _meeting_dict(meeting)


@router.patch("/meetings/{meeting_id}/title")
async def update_meeting_title(
    meeting_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_api_key),
):
    """Update the title of a meeting. Called by the bot during teardown if title was missing."""
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    meeting = await _get_meeting_or_404(meeting_id, session)
    if not meeting.title:
        meeting.title = title
        await session.commit()
        logger.info("update_meeting_title: meeting_id=%d title=%r", meeting_id, title)
    return {"meeting_id": meeting_id, "title": meeting.title}


@router.patch("/meetings/{meeting_id}/emails")
async def update_meeting_emails(
    meeting_id: int,
    body: MeetingEmailsUpdate,
    session: AsyncSession = Depends(get_session),
):
    """Update organizer_email and/or attendee_emails on an existing meeting.

    Called by the calendar integration service after auto-join to store the
    attendee list from the Google Calendar invite, enabling BCC on summary emails.
    Also useful for manual joins where emails are known only after the meeting starts.
    """
    meeting = await _get_meeting_or_404(meeting_id, session)
    if body.organizer_email is not None:
        meeting.organizer_email = body.organizer_email or None
    if body.attendee_emails is not None:
        # Merge with existing attendee_emails (deduplicate)
        existing = set(
            e.strip() for e in (meeting.attendee_emails or "").split(",") if e.strip()
        )
        new_ones = set(
            e.strip() for e in body.attendee_emails.split(",") if e.strip()
        )
        merged = existing | new_ones
        meeting.attendee_emails = ",".join(sorted(merged)) or None
    await session.commit()
    await session.refresh(meeting)
    logger.info(
        "Meeting %d emails updated — organizer=%s attendees=%s",
        meeting_id, meeting.organizer_email, meeting.attendee_emails,
    )
    return _meeting_dict(meeting)


@router.get("/meetings")
async def list_meetings(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Meeting).order_by(desc(Meeting.created_at)).limit(100)
    )
    return [_meeting_dict(m) for m in result.scalars().all()]


@router.get("/meetings/by-code/{meet_code}")
async def get_meeting_by_code(
    meet_code: str, session: AsyncSession = Depends(get_session)
):
    result = await session.execute(
        select(Meeting)
        .where(Meeting.meet_code == meet_code)
        .order_by(desc(Meeting.created_at))
        .limit(1)
    )
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail=f"No meeting for code {meet_code}")
    return _meeting_dict(meeting)


@router.get("/meetings/{meeting_id}")
async def get_meeting(
    meeting_id: int, session: AsyncSession = Depends(get_session)
):
    meeting = await _get_meeting_or_404(meeting_id, session)
    return _meeting_dict(meeting)


# ── Participants ───────────────────────────────────────────────────────────────


@router.post("/meetings/{meeting_id}/participants", status_code=201)
async def upsert_participant(
    meeting_id: int,
    body: ParticipantUpsert,
    session: AsyncSession = Depends(get_session),
):
    await _get_meeting_or_404(meeting_id, session)

    _name = (body.name or "").strip()
    if not _name:
        raise HTTPException(status_code=422, detail="Participant name must not be empty")

    # Case-insensitive dedup — "Rishabh Tripathi" and "rishabh tripathi" are the same person
    existing = await session.execute(
        select(Participant).where(
            Participant.meeting_id == meeting_id,
            func.lower(Participant.name) == _name.lower(),
        )
    )
    participant = existing.scalar_one_or_none()

    if participant:
        participant.is_active = body.is_active
    else:
        participant = Participant(
            meeting_id=meeting_id, name=_name, is_active=body.is_active
        )
        session.add(participant)

    await session.commit()
    await session.refresh(participant)
    return {"id": participant.id, "name": participant.name, "is_active": participant.is_active}


@router.get("/meetings/{meeting_id}/participants")
async def list_participants(
    meeting_id: int, session: AsyncSession = Depends(get_session)
):
    await _get_meeting_or_404(meeting_id, session)
    result = await session.execute(
        select(Participant).where(Participant.meeting_id == meeting_id)
    )
    return [
        {"id": p.id, "name": p.name, "is_active": p.is_active}
        for p in result.scalars().all()
    ]


# ── Transcript ─────────────────────────────────────────────────────────────────


@router.post("/meetings/{meeting_id}/transcript", status_code=201)
async def add_transcript_segment(
    meeting_id: int,
    body: TranscriptAdd,
    session: AsyncSession = Depends(get_session),
):
    await _get_meeting_or_404(meeting_id, session)

    segment = TranscriptSegment(
        meeting_id=meeting_id,
        speaker_name=body.speaker_name,
        text=body.text,
        source=body.source,
        start_time=body.start_time,
        end_time=body.end_time,
        created_at=datetime.utcnow(),
        language_code=body.language_code,
        speaker_source=body.speaker_source,
        speaker_confidence=body.speaker_confidence,
        alignment_overlap_ms=body.alignment_overlap_ms,
        is_overlapping_speech=body.is_overlapping_speech,
        secondary_speakers_json=body.secondary_speakers_json,
        confidence=body.confidence or "high",
    )
    session.add(segment)
    await session.commit()
    await session.refresh(segment)
    logger.info(
        "Transcript saved — meeting=%d id=%d source=%s speaker=%r text=%r",
        meeting_id, segment.id, body.source,
        body.speaker_name, body.text[:80],
    )
    # Fire-and-forget background translation for non-English segments.
    # By the time the meeting ends and summarize runs, most will be done.
    schedule_translation(segment.id, body.text, body.language_code or "unknown")
    return _segment_dict(segment)


@router.get("/meetings/{meeting_id}/transcript")
async def get_transcript(
    meeting_id: int, session: AsyncSession = Depends(get_session)
):
    await _get_meeting_or_404(meeting_id, session)
    result = await session.execute(
        select(TranscriptSegment)
        .where(TranscriptSegment.meeting_id == meeting_id)
        .order_by(TranscriptSegment.created_at)
    )
    return [_segment_dict(s) for s in result.scalars().all()]


@router.get("/meetings/{meeting_id}/speaker-stats")
async def get_speaker_stats(
    meeting_id: int, session: AsyncSession = Depends(get_session)
):
    await _get_meeting_or_404(meeting_id, session)
    result = await session.execute(
        select(TranscriptSegment)
        .where(TranscriptSegment.meeting_id == meeting_id)
        .order_by(TranscriptSegment.created_at)
    )
    segments = result.scalars().all()
    stats: dict = {}
    for seg in segments:
        name = seg.speaker_name or "Unknown"
        if name not in stats:
            stats[name] = {
                "name": name,
                "segment_count": 0,
                "total_duration": 0.0,
                "first_seen": seg.created_at.isoformat(),
            }
        stats[name]["segment_count"] += 1
        if seg.start_time is not None and seg.end_time is not None:
            stats[name]["total_duration"] += round(seg.end_time - seg.start_time, 2)
    return list(stats.values())


@router.get("/meetings/{meeting_id}/transcript/live")
async def live_transcript(meeting_id: int, request: Request, since: int = 0):
    """SSE stream — pushes new TranscriptSegment rows in real time.

    One DB connection is held per connected client (not one per poll tick).
    A rollback after each tick advances the MySQL read snapshot so fresh
    rows committed by the bot are visible on the next poll.

    Pass ?since=<last_segment_id> to skip segments already loaded via REST.
    """

    async def event_generator() -> AsyncIterator[dict]:
        async with async_session() as db:
            result = await db.execute(select(Meeting).where(Meeting.id == meeting_id))
            if not result.scalar_one_or_none():
                logger.warning("SSE: meeting %d not found", meeting_id)
                yield {
                    "event": "error",
                    "data": json.dumps({"message": f"Meeting {meeting_id} not found"}),
                }
                return
            # End the initial check transaction before entering the poll loop.
            await db.rollback()

            logger.info("SSE: client connected for meeting %d (since id=%d)", meeting_id, since)
            last_id: int = since
            idle_ticks: int = 0
            total_pushed: int = 0

            while True:
                if await request.is_disconnected():
                    logger.info(
                        "SSE: client disconnected for meeting %d (pushed %d segments)",
                        meeting_id, total_pushed,
                    )
                    break

                try:
                    seg_result = await db.execute(
                        select(TranscriptSegment)
                        .where(
                            TranscriptSegment.meeting_id == meeting_id,
                            TranscriptSegment.id > last_id,
                        )
                        .order_by(TranscriptSegment.id)
                    )
                    segments = seg_result.scalars().all()

                    for seg in segments:
                        last_id = seg.id
                        idle_ticks = 0
                        total_pushed += 1
                        logger.info(
                            "SSE: pushing segment id=%d meeting=%d speaker=%r text=%r",
                            seg.id, meeting_id, seg.speaker_name, seg.text[:60],
                        )
                        yield {
                            "event": "transcript",
                            "data": json.dumps(_segment_dict(seg)),
                        }

                    mtg_result = await db.execute(
                        select(Meeting).where(Meeting.id == meeting_id)
                    )
                    current = mtg_result.scalar_one_or_none()
                    if current and current.status == "completed":
                        logger.info(
                            "SSE: meeting %d completed — sending meeting_ended event (pushed %d segments total)",
                            meeting_id, total_pushed,
                        )
                        yield {
                            "event": "meeting_ended",
                            "data": json.dumps(
                                {
                                    "message": "Meeting has ended",
                                    "ended_at": current.ended_at.isoformat()
                                    if current.ended_at
                                    else None,
                                }
                            ),
                        }
                        break

                    # Rollback ends the current MySQL transaction so the next
                    # tick opens a fresh read snapshot and sees newly committed rows.
                    await db.rollback()

                except Exception as exc:
                    logger.warning("SSE: DB error for meeting %d — %s", meeting_id, exc)
                    try:
                        await db.rollback()
                    except Exception:
                        pass

                idle_ticks += 1
                if idle_ticks >= 30:
                    yield {"event": "ping", "data": ""}
                    idle_ticks = 0

                await asyncio.sleep(1)

    return EventSourceResponse(event_generator())


# ── Bot control ────────────────────────────────────────────────────────────────


@router.get("/bot/auth-status")
async def get_auth_status():
    status = auth_status()
    logger.info(
        "GET /bot/auth-status → authenticated=%s session_exists=%s detail=%s",
        status["authenticated"], status["session_exists"], status["auth_detail"],
    )
    return status


@router.post("/bot/join", status_code=202)
async def join_bot(body: BotJoinRequest, request: Request, _: None = Depends(_require_api_key)):
    _check_rate_limit(f"bot_join:{request.client.host}", max_calls=10, window_sec=60)
    logger.info("POST /bot/join — url=%s title=%s", body.meet_url, body.title)
    _parsed_url = urlparse(body.meet_url)
    if _parsed_url.hostname != "meet.google.com":
        logger.warning("POST /bot/join — invalid URL rejected: %s", body.meet_url)
        raise HTTPException(status_code=400, detail="URL must be a Google Meet link (meet.google.com)")
    # Fallback: if organizer_email not in the request, use DEFAULT_ORGANIZER_EMAIL env var.
    # This ensures manual joins from the UI still send the summary email.
    _organizer = body.organizer_email or os.getenv("DEFAULT_ORGANIZER_EMAIL", "")
    if not body.organizer_email and _organizer:
        logger.info("POST /bot/join — using DEFAULT_ORGANIZER_EMAIL=%s", _organizer)
    try:
        code = await bot_manager.join(
            body.meet_url,
            title=body.title,
            organizer_email=_organizer or None,
            attendee_emails=body.attendee_emails,
        )
    except RuntimeError as exc:
        msg = str(exc)
        # Capacity limit returns 503 (service unavailable, retry later)
        # Auth failure returns 403
        status_code = 503 if "capacity" in msg.lower() else 403
        logger.error("POST /bot/join — bot cannot start (%d): %s", status_code, msg)
        raise HTTPException(status_code=status_code, detail=msg)
    if code is None:
        logger.warning("POST /bot/join — bot already running for meet_code in url=%s", body.meet_url)
        raise HTTPException(status_code=409, detail="Bot already running for this meeting")
    logger.info("POST /bot/join — task launched for meet_code=%s", code)
    return {"meet_code": code, "status": "launching"}


@router.get("/bot/status")
async def get_bot_status():
    return bot_manager.status()


@router.delete("/bot/{meet_code}/stop")
async def stop_bot(
    meet_code: str,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_api_key),
):
    stopped = await bot_manager.stop(meet_code)
    if stopped:
        # Graceful stop runs the bot's own teardown (end_meeting + summary).
        return {"meet_code": meet_code, "status": "stopping"}

    # No live bot (already crashed/exited) — reconcile any meeting still marked
    # "active"/"pending" for this code so the UI doesn't show a stuck meeting.
    result = await session.execute(
        select(Meeting)
        .where(Meeting.meet_code == meet_code, Meeting.status != "completed")
        .order_by(desc(Meeting.id))
    )
    stale = result.scalars().first()
    if stale:
        stale.status = "completed"
        if stale.ended_at is None:
            stale.ended_at = datetime.utcnow()
        await session.commit()
        logger.info("stop_bot: no live bot for %s — marked stale meeting %d completed", meet_code, stale.id)
        return {"meet_code": meet_code, "status": "reconciled", "meeting_id": stale.id}

    raise HTTPException(status_code=404, detail="No active bot or open meeting for that meeting code")


@router.get("/bot/{meet_code}/fail-screenshot")
async def bot_fail_screenshot(meet_code: str):
    """Returns base64 screenshot saved when bot failed to join (for debugging)."""
    import base64 as _b64
    ss_path = Path(__file__).parent.parent / "uploads" / f"bot_fail_{meet_code}.png"
    if not ss_path.exists():
        raise HTTPException(status_code=404, detail="No failure screenshot found for this meeting code")
    data = _b64.b64encode(ss_path.read_bytes()).decode()
    return {"meet_code": meet_code, "screenshot": data, "path": str(ss_path)}


# ── Bot Login (remote-controlled headless browser) ─────────────────────────────


@router.post("/bot/login/open")
async def open_login_browser():
    """Open a visible Chrome window for Google login (local/non-headless only).
    In headless/server mode returns status='headless' so the frontend falls back
    to the screenshot-based login flow."""
    if os.getenv("HEADLESS", "false").lower() == "true":
        return {
            "status": "headless",
            "message": (
                "Server is running in headless mode — visible browser cannot be opened. "
                "Use the screenshot-based remote login instead."
            ),
        }
    if login_session.opening:
        return {"status": "already_opening", "message": "Login browser already open — log in and close it."}
    asyncio.create_task(login_session.open_browser())
    return {"status": "opening", "message": "A Chrome window has opened. Log in with any Google account, then close the window."}


@router.get("/bot/login/status")
async def login_open_status():
    """Returns whether the visible login browser is still open."""
    return {"opening": login_session.opening}


@router.post("/bot/login/start")
async def start_login():
    """Launch a headless browser pointed at Google login. Use /bot/login/screenshot to see it."""
    if login_session.active:
        b64 = await login_session.screenshot_b64()
        url = await login_session.current_url()
        return {"status": "already_active", "screenshot": b64, "url": url}
    try:
        await login_session.start()
    except Exception as exc:
        logger.error("Login session start failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    b64 = await login_session.screenshot_b64()
    url = await login_session.current_url()
    return {"status": "started", "screenshot": b64, "url": url}


@router.get("/bot/login/screenshot")
async def login_screenshot():
    """Return a base64 JPEG screenshot of the current headless browser page."""
    if not login_session.active:
        raise HTTPException(status_code=400, detail="No active login session — POST /bot/login/start first")
    b64 = await login_session.screenshot_b64()
    url = await login_session.current_url()
    return {"screenshot": b64, "url": url}


@router.post("/bot/login/click")
async def login_click(body: LoginClickRequest):
    """Click at (x, y) inside the headless browser."""
    if not login_session.active:
        raise HTTPException(status_code=400, detail="No active login session")
    await login_session.click(body.x, body.y)
    b64 = await login_session.screenshot_b64()
    url = await login_session.current_url()
    return {"screenshot": b64, "url": url}


@router.post("/bot/login/type")
async def login_type(body: LoginTypeRequest):
    """Type text into the currently focused element."""
    if not login_session.active:
        raise HTTPException(status_code=400, detail="No active login session")
    await login_session.type_text(body.text)
    b64 = await login_session.screenshot_b64()
    url = await login_session.current_url()
    return {"screenshot": b64, "url": url}


@router.post("/bot/login/key")
async def login_key(body: LoginKeyRequest):
    """Press a keyboard key (e.g. Enter, Tab, Backspace)."""
    if not login_session.active:
        raise HTTPException(status_code=400, detail="No active login session")
    await login_session.press_key(body.key)
    b64 = await login_session.screenshot_b64()
    url = await login_session.current_url()
    return {"screenshot": b64, "url": url}


@router.post("/bot/login/navigate")
async def login_navigate(body: LoginNavRequest):
    """Navigate the login browser to a URL."""
    if not login_session.active:
        raise HTTPException(status_code=400, detail="No active login session")
    await login_session.navigate(body.url)
    b64 = await login_session.screenshot_b64()
    url = await login_session.current_url()
    return {"screenshot": b64, "url": url}


@router.post("/bot/login/done")
async def finish_login():
    """Save the Google session cookies to disk and close the login browser."""
    if not login_session.active:
        raise HTTPException(status_code=400, detail="No active login session")
    await login_session.done()
    status = auth_status()
    logger.info("Login session finished — authenticated=%s", status["authenticated"])
    return {"status": "saved", "authenticated": status["authenticated"]}


@router.post("/bot/login/abort")
async def abort_login():
    """Close the login browser WITHOUT saving session (cancel path)."""
    if not login_session.active:
        return {"status": "not_active"}
    await login_session.abort()
    logger.info("Login session aborted — session NOT saved")
    return {"status": "aborted"}


# ── Calendar ───────────────────────────────────────────────────────────────────


@router.get("/calendar/status")
async def calendar_status():
    """Returns Calendar integration status and the next upcoming Meet event."""
    token_file = Path(__file__).parent.parent / "bot" / "calendar_token.json"
    if not token_file.exists():
        return {
            "configured": False,
            "message": "Not configured. Run  python -m bot.calendar_auth  on the server.",
            "next_event": None,
        }

    # Fetch the next upcoming Meet event in a thread — creds.refresh() and the
    # Google API client are blocking I/O. Running them on the async event loop
    # directly stalls ALL requests for the duration of the DNS/network timeout.
    def _fetch_next_event():
        import re as _re
        from datetime import datetime, timedelta, timezone
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(
            str(token_file), ["https://www.googleapis.com/auth/calendar.readonly"]
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_file.write_text(creds.to_json())

        service = build("calendar", "v3", credentials=creds)
        now = datetime.now(timezone.utc)
        result = service.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(hours=24)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=5,
        ).execute()

        _meet_re = _re.compile(r"https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}", _re.IGNORECASE)
        for event in result.get("items", []):
            has_meet = any(
                ep.get("entryPointType") == "video" and "meet.google.com" in ep.get("uri", "")
                for ep in event.get("conferenceData", {}).get("entryPoints", [])
            ) or any(
                _meet_re.search(event.get(f, "") or "")
                for f in ("description", "location", "summary")
            )
            if has_meet:
                start = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date", "")
                return {"title": event.get("summary", "Untitled"), "start": start}
        return None

    next_event = None
    try:
        next_event = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _fetch_next_event),
            timeout=8.0,
        )
    except asyncio.TimeoutError:
        logger.warning("calendar_status: timed out fetching next event (network unavailable?)")
    except Exception as exc:
        logger.warning("calendar_status: could not fetch next event: %s", exc)

    return {
        "configured": True,
        "message": "Calendar connected — bot will auto-join Meet events 2 min before start.",
        "next_event": next_event,
    }


# ── Summarize ──────────────────────────────────────────────────────────────────


@router.post("/meetings/{meeting_id}/summarize")
async def summarize_meeting(
    meeting_id: int,
    request: Request,
    body: SummarizeRequest = SummarizeRequest(),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_api_key),
):
    _check_rate_limit(f"summarize:{meeting_id}", max_calls=5, window_sec=60)
    meeting = await _get_meeting_or_404(meeting_id, session)

    # Return cached summary — never regenerate once produced
    if meeting.summary:
        logger.info("Summarize: returning cached summary for meeting %d (%d chars)", meeting_id, len(meeting.summary))
        return {"id": meeting.id, "summary": meeting.summary}

    seg_result = await session.execute(
        select(TranscriptSegment)
        .where(TranscriptSegment.meeting_id == meeting_id)
        .order_by(TranscriptSegment.created_at)
    )
    segments = seg_result.scalars().all()

    if not segments:
        raise HTTPException(status_code=400, detail="No transcript segments found")

    # Guard: skip summarize for meetings with almost no real content
    # (e.g. Google Meet crashed after 4 s, only system messages captured).
    _MIN_SEGMENTS = 3
    _MIN_CHARS = 100
    _quick_text = " ".join(s.text or "" for s in segments)
    if len(segments) < _MIN_SEGMENTS or len(_quick_text.strip()) < _MIN_CHARS:
        logger.info(
            "Summarize: skipping — transcript too short (segments=%d chars=%d) for meeting %d",
            len(segments), len(_quick_text.strip()), meeting_id,
        )
        return {"id": meeting.id, "summary": None}

    # ── Deduplicate caption + STT segments ────────────────────────────────────
    # Only deduplicate same speaker saying same thing (caption + STT both captured it).
    # Different speakers saying similar things must be kept — keying by (speaker, text)
    # prevents one speaker's utterance from silently dropping another's real content.
    seen_text: dict[str, object] = {}
    for seg in segments:
        text_key = " ".join((seg.text or "").lower().split())[:80]
        if not text_key:
            continue
        speaker_key = (seg.speaker_name or "Unknown").strip().lower()
        # Key = speaker + text fingerprint: only dedup same speaker, same text
        key = f"{speaker_key}::{text_key}"
        if key not in seen_text:
            seen_text[key] = seg
        else:
            existing = seen_text[key]
            existing_has_name = existing.speaker_name and existing.speaker_name != "Unknown"
            seg_has_name = seg.speaker_name and seg.speaker_name != "Unknown"
            if not existing_has_name and seg_has_name:
                seen_text[key] = seg

    clean_segments = sorted(seen_text.values(), key=lambda s: s.created_at)
    dedup_removed = len(segments) - len(clean_segments)
    if dedup_removed:
        logger.info(
            "Summarize: deduplicated %d duplicate segments (caption+STT overlap) for meeting %d",
            dedup_removed, meeting_id,
        )

    # ── Speaker participation stats ───────────────────────────────────────────
    from collections import Counter
    _BOT_NAMES = {"oneclarity ai", "oneclarity", "bot", "ai recorder", "meeting bot"}
    speaker_counts: Counter = Counter()
    for seg in clean_segments:
        name = (seg.speaker_name or "Unknown").strip()
        if name and name != "Unknown" and name.lower() not in _BOT_NAMES:
            # Use word count as proxy for talk time
            speaker_counts[name] += len((seg.text or "").split())

    speaker_stats_lines: list[str] = []
    if speaker_counts:
        total_words = sum(speaker_counts.values()) or 1
        for name, words in speaker_counts.most_common():
            pct = round(words / total_words * 100)
            speaker_stats_lines.append(f"  - {name}: ~{pct}% of spoken content")

    # Format transcript with confidence labels so the LLM can skip low-confidence
    # (crosstalk) lines when attributing decisions and action items (Step 2).
    # Also filter out noise: segments with fewer than 2 meaningful words are skipped.
    def _seg_is_noise(seg) -> bool:
        t = (seg.translated_text or seg.text or "").strip()
        if not t:
            return True
        words = [w for w in t.split() if len(w) > 1]
        return len(words) < 2

    transcript_text = "\n".join(
        f"{s.speaker_name or 'Unknown'} ({s.confidence or 'high'}): {s.translated_text or s.text}"
        for s in clean_segments
        if (s.speaker_name or "Unknown").lower() not in _BOT_NAMES
        and not _seg_is_noise(s)
    )

    logger.info(
        "Generating summary for meeting %d (%d segments, %d after dedup, speakers: %s)",
        meeting_id, len(segments), len(clean_segments),
        ", ".join(speaker_counts.keys()) or "none identified",
    )

    # Calculate duration — prefer DB timestamps, fall back to segment start/end times
    duration_minutes = 0
    if meeting.ended_at and meeting.created_at:
        duration_minutes = int((meeting.ended_at - meeting.created_at).total_seconds() / 60)
    if duration_minutes <= 0 and clean_segments:
        seg_times = [
            s.start_time for s in clean_segments if s.start_time is not None
        ] + [s.end_time for s in clean_segments if s.end_time is not None]
        if seg_times:
            duration_minutes = max(1, int((max(seg_times) - min(seg_times)) / 60))

    # Use meeting's organizer_email (set when Apps Script sends the bot)
    summary_email = meeting.organizer_email or ""

    # ── Attendee emails — merged from all available sources ──────────────────
    # Sources checked in order, all merged and deduplicated:
    #   1. meeting.attendee_emails — stored at join time (Google Calendar invite)
    #   2. body.attendee_emails   — passed in this request; the bot teardown now
    #      forwards its in-memory self._attendee_emails here as a fallback, so
    #      attendees are delivered even when the DB column is NULL.
    _raw_bcc_sources = []
    _attendee_sources: list[str] = []

    if meeting.attendee_emails:
        _db_list = [e for e in meeting.attendee_emails.split(",") if e.strip()]
        _raw_bcc_sources.extend(_db_list)
        if _db_list:
            _attendee_sources.append(f"db({len(_db_list)})")
            logger.debug("Summarize: meeting %d — loaded %d attendee(s) from db", meeting_id, len(_db_list))

    if body.attendee_emails:
        _req_list = [e for e in body.attendee_emails.split(",") if e.strip()]
        _raw_bcc_sources.extend(_req_list)
        if _req_list:
            _attendee_sources.append(f"request({len(_req_list)})")
            logger.debug("Summarize: meeting %d — loaded %d attendee(s) from request", meeting_id, len(_req_list))

    bcc_emails: list[str] = []
    _seen_bcc: set = set()
    for e in _raw_bcc_sources:
        e = e.strip()
        if e and e != summary_email and e not in _seen_bcc:
            _seen_bcc.add(e)
            bcc_emails.append(e)

    # Full merged attendee list (pre CC/BCC split below) — persisted onto
    # meeting.attendee_emails further down so push-summary sees every attendee
    # this call resolved, not just whatever was already in the DB column.
    all_attendee_emails = list(bcc_emails)

    # Recipient visibility: by default attendees go in CC so the summary visibly
    # reaches everyone who was invited (standard for meeting recaps, and makes it
    # obvious delivery wasn't "only to one person"). Set SUMMARY_BCC_ATTENDEES=true
    # to keep them hidden in BCC instead.
    _use_bcc = os.getenv("SUMMARY_BCC_ATTENDEES", "false").lower() == "true"
    cc_emails: list[str] = [] if _use_bcc else bcc_emails
    if not _use_bcc:
        bcc_emails = []

    if cc_emails or bcc_emails:
        logger.info(
            "Summarize: meeting %d → TO=%s CC=%d BCC=%d recipients (sources: %s)",
            meeting_id, summary_email,
            len(cc_emails), len(bcc_emails),
            ", ".join(_attendee_sources) if _attendee_sources else "none",
        )
    elif summary_email:
        logger.info(
            "Summarize: meeting %d → TO=%s only — no attendees found in db or request "
            "(manual join without attendees, calendar invite had no other accepted "
            "invitees, or all declined)",
            meeting_id, summary_email,
        )

    speaker_stats_text = (
        "Speaker participation:\n" + "\n".join(speaker_stats_lines)
        if speaker_stats_lines else ""
    )

    # Human-friendly meeting title fallback
    meeting_title_clean = (
        meeting.title
        if meeting.title and meeting.meet_code not in meeting.title
        else f"Google Meet — {meeting.created_at.strftime('%a %b %-d, %Y') if meeting.created_at else meeting.meet_code}"
    )

    # Clean participant names — exclude the meeting title itself if it leaked as a speaker name
    _title_lower = meeting_title_clean.lower().strip()
    _bot_names_lower = {n.lower() for n in _BOT_NAMES}
    known_participant_names = [
        n for n in (speaker_counts.keys() if speaker_counts else [])
        if n.lower().strip() != _title_lower
    ]

    # Merge in participants saved by the bot's DOM scraper (_scrape_participants).
    # This captures attendees who never unmuted — their names are in ai_participants
    # but have no transcript segments, so speaker_counts misses them entirely.
    #
    # Fuzzy dedup: "Rishabh" (from STT) + "Rishabh Tripathi" (from DOM) are the same
    # person. When first names collide, keep the longer (more complete) name.
    def _first_word(n: str) -> str:
        parts = n.strip().split()
        return parts[0].lower() if parts else ""

    try:
        _db_parts = await session.execute(
            select(Participant).where(Participant.meeting_id == meeting_id)
        )
        for _p in _db_parts.scalars().all():
            _n = (_p.name or "").strip()
            if not _n or _n.lower() in _bot_names_lower or _n.lower() == _title_lower:
                continue
            _n_first = _first_word(_n)
            _clash_idx = next(
                (i for i, existing in enumerate(known_participant_names)
                 if _first_word(existing) == _n_first),
                None,
            )
            if _clash_idx is None:
                known_participant_names.append(_n)
            elif len(_n) > len(known_participant_names[_clash_idx]):
                # Upgrade shorter name to full name (e.g. "Rishabh" → "Rishabh Tripathi")
                known_participant_names[_clash_idx] = _n
    except Exception as _pe:
        logger.warning("Could not load ai_participants for summary: %s", _pe)

    try:
        summary_result = await generate_and_email_summary(
            transcript=transcript_text,
            meeting_title=meeting_title_clean,
            meet_code=meeting.meet_code,
            to_email=summary_email,
            duration_minutes=duration_minutes,
            speaker_stats=speaker_stats_text,
            bcc_emails=bcc_emails,
            cc_emails=cc_emails,
            meeting_id=meeting_id,
            known_participants=known_participant_names or None,
        )
    except Exception as exc:
        logger.error("Summary failed for meeting %d: %s", meeting_id, exc)
        raise HTTPException(status_code=502, detail=f"AI summary failed: {exc}")

    summary = summary_result.summary
    meeting.summary = summary
    if all_attendee_emails:
        meeting.attendee_emails = ",".join(all_attendee_emails)
    await session.commit()
    try:
        session.add(AuditLog(
            entity_type="meeting", entity_id=meeting.id, action="summarize",
            changes={
                "summary_chars": len(summary or ""),
                "to_email": summary_email,
                "cc_count": len(cc_emails),
                "bcc_count": len(bcc_emails),
                "transcript_segments": len(clean_segments),
            },
        ))
        await session.commit()
    except Exception as _exc:
        logger.warning("Audit log failed for meeting summarize %d: %s", meeting.id, _exc)

    return {"id": meeting.id, "summary": summary}


# ── Node backend notify — in-app notification after summary is ready ──────────

_NODE_BACKEND_URL: str = os.getenv("NODE_BACKEND_URL", "").rstrip("/")
_NODE_INTERNAL_API_KEY: str = os.getenv("NODE_INTERNAL_API_KEY", "").strip()


async def _notify_node_summary(meeting: Meeting) -> None:
    """
    Best-effort call to Node so it can push an in-app notification to every
    resolved attendee. Never raises — push-summary has already succeeded and
    must not fail just because Node is unreachable; the direct SES email
    (sent earlier via generate_and_email_summary) already reached everyone
    regardless of whether this call succeeds.
    """
    if not _NODE_BACKEND_URL:
        logger.debug("push-summary: NODE_BACKEND_URL not set — skipping Node notify")
        return

    recipients: list[str] = []
    _seen: set = set()
    for e in [meeting.organizer_email or ""] + (meeting.attendee_emails or "").split(","):
        e = e.strip()
        if e and e not in _seen:
            _seen.add(e)
            recipients.append(e)

    if not recipients:
        return

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
        ) as client:
            resp = await client.post(
                f"{_NODE_BACKEND_URL}/api/meeting-bot/summary-notify",
                json={
                    "meeting_id": meeting.id,
                    "meet_code": meeting.meet_code,
                    "title": meeting.title,
                    "summary": meeting.summary or "",
                    "recipients": recipients,
                },
                headers=(
                    {"X-Internal-Api-Key": _NODE_INTERNAL_API_KEY}
                    if _NODE_INTERNAL_API_KEY else {}
                ),
            )
        if resp.status_code >= 400:
            logger.warning(
                "push-summary: Node notify returned %d for meeting_id=%d: %s",
                resp.status_code, meeting.id, resp.text[:300],
            )
        else:
            logger.info(
                "push-summary: Node notify → meeting_id=%d recipients=%d → %s",
                meeting.id, len(recipients), resp.json(),
            )
    except Exception as exc:
        logger.warning("push-summary: Node notify failed for meeting_id=%d: %s", meeting.id, exc)


# ── Meeting Summary — post-meeting upsert (1 row per meeting) ─────────────────

@router.post("/meetings/{meeting_id}/push-summary", status_code=200)
async def push_meeting_summary(
    meeting_id: int,
    body: PushSummaryRequest = PushSummaryRequest(),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_api_key),
):
    """
    After meeting ends + summary ready → upsert ONE row into ai_meeting_master.
    Safe to call multiple times — uses ON DUPLICATE KEY UPDATE (idempotent).
    person_transcript = [{speaker, text}, ...] ordered by time.

    hand_raises / chat_messages have no transactional table of their own —
    the bot accumulates them in memory during the meeting and passes them in
    this call's body (same pattern as SummarizeRequest.attendee_emails).
    """
    from sqlalchemy import text as _sql_text
    from collections import Counter as _Counter

    meeting = await _get_meeting_or_404(meeting_id, session)

    # ── 1. Fetch segments ordered by time ────────────────────────────────────
    seg_result = await session.execute(
        select(TranscriptSegment)
        .where(TranscriptSegment.meeting_id == meeting_id)
        .order_by(TranscriptSegment.start_time, TranscriptSegment.created_at)
    )
    segments = seg_result.scalars().all()
    if not segments:
        logger.warning("push-summary: meeting_id=%d has no segments — skipping", meeting_id)
        return {"meeting_id": meeting_id, "status": "skipped", "reason": "no_segments"}

    # ── 2. Build person_transcript — speaker + text only ─────────────────────
    _bot_names = {"oneclarity ai", "oneclarity", "bot", "ai recorder", "meeting bot"}
    person_transcript = [
        {"speaker": s.speaker_name or "Unknown", "text": s.text}
        for s in segments
        if s.text and s.text.strip()
        and (s.speaker_name or "").lower() not in _bot_names
    ]

    # ── 3. Dominant speaker_source across segments ────────────────────────────
    sources = [s.speaker_source for s in segments if s.speaker_source]
    dominant_source = _Counter(sources).most_common(1)[0][0] if sources else None

    # ── 4. Resolve user_id + tenant_id from organizer email ──────────────────
    user_id = tenant_id = None
    if meeting.organizer_email:
        try:
            _row = await session.execute(
                _sql_text(
                    "SELECT user_id, company_id AS tenant_id "
                    "FROM `user` WHERE email = :email LIMIT 1"
                ),
                {"email": meeting.organizer_email},
            )
            _u = _row.mappings().first()
            if _u:
                user_id   = _u["user_id"]
                tenant_id = _u["tenant_id"]
        except Exception as _ue:
            logger.warning("push-summary: user lookup failed for %s: %s", meeting.organizer_email, _ue)

    # ── 5. Resolve fallbacks — never push NULL for critical columns ──────────────
    resolved_title      = meeting.title or f"Meeting {meeting.meet_code}"
    resolved_ended_at   = meeting.ended_at or datetime.utcnow()
    resolved_status     = meeting.status or "completed"
    resolved_started_at = meeting.created_at
    meeting_date        = resolved_ended_at.date() if resolved_ended_at else (
                          resolved_started_at.date() if resolved_started_at else None
                          )

    logger.info(
        "push-summary: meeting_id=%d resolved → title=%r status=%s ended_at=%s",
        meeting_id, resolved_title, resolved_status,
        resolved_ended_at.isoformat() if resolved_ended_at else None,
    )

    # ── 6. Upsert — INSERT or UPDATE if meeting_id already exists ─────────────
    hand_raises   = body.hand_raises or []
    chat_messages = body.chat_messages or []
    await session.execute(
        _sql_text("""
            INSERT INTO ai_meeting_master (
                meeting_id, meet_code, title, meeting_status,
                meeting_started_at, meeting_ended_at, meeting_date,
                organizer_email, attendee_emails, summary,
                user_id, tenant_id,
                person_transcript, hand_raises, chat_messages, speaker_source,
                created_at
            ) VALUES (
                :meeting_id, :meet_code, :title, :meeting_status,
                :meeting_started_at, :meeting_ended_at, :meeting_date,
                :organizer_email, :attendee_emails, :summary,
                :user_id, :tenant_id,
                :person_transcript, :hand_raises, :chat_messages, :speaker_source,
                NOW()
            )
            ON DUPLICATE KEY UPDATE
                meet_code          = VALUES(meet_code),
                title              = VALUES(title),
                meeting_status     = VALUES(meeting_status),
                meeting_started_at = VALUES(meeting_started_at),
                meeting_ended_at   = VALUES(meeting_ended_at),
                meeting_date       = VALUES(meeting_date),
                organizer_email    = VALUES(organizer_email),
                attendee_emails    = VALUES(attendee_emails),
                summary            = VALUES(summary),
                user_id            = VALUES(user_id),
                tenant_id          = VALUES(tenant_id),
                person_transcript  = VALUES(person_transcript),
                hand_raises        = VALUES(hand_raises),
                chat_messages      = VALUES(chat_messages),
                speaker_source     = VALUES(speaker_source)
        """),
        {
            "meeting_id":         meeting_id,
            "meet_code":          meeting.meet_code,
            "title":              resolved_title,
            "meeting_status":     resolved_status,
            "meeting_started_at": resolved_started_at,
            "meeting_ended_at":   resolved_ended_at,
            "meeting_date":       meeting_date,
            "organizer_email":    meeting.organizer_email,
            "attendee_emails":    meeting.attendee_emails,
            "summary":            meeting.summary,
            "user_id":            user_id,
            "tenant_id":          tenant_id,
            "person_transcript":  json.dumps(person_transcript, ensure_ascii=False),
            "hand_raises":        json.dumps(hand_raises, ensure_ascii=False),
            "chat_messages":      json.dumps(chat_messages, ensure_ascii=False),
            "speaker_source":     dominant_source,
        },
    )
    await session.commit()

    logger.info(
        "push-summary: meeting_id=%d meet_code=%s → upserted into ai_meeting_master "
        "(%d transcript entries, %d hand-raises, %d chat messages, source=%s)",
        meeting_id, meeting.meet_code, len(person_transcript), len(hand_raises),
        len(chat_messages), dominant_source,
    )

    await _notify_node_summary(meeting)

    return {
        "meeting_id":        meeting_id,
        "meet_code":         meeting.meet_code,
        "transcript_count":  len(person_transcript),
        "hand_raise_count":  len(hand_raises),
        "chat_message_count": len(chat_messages),
        "status":            "ok",
    }


@router.post("/meetings/{meeting_id}/push-master", status_code=200)
async def push_meeting_master(
    meeting_id: int,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_api_key),
):
    """Deprecated — redirects to push-summary."""
    return await push_meeting_summary(meeting_id, PushSummaryRequest(), session, _)


# ── ai_meeting_master column audit ────────────────────────────────────────────

@router.get("/meetings/master/audit-empty", status_code=200)
async def audit_meeting_master_empty(
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_require_api_key),
):
    """
    Scan ai_meeting_master and report which columns are NULL per row.
    Returns a per-column null count + a list of affected meeting_ids per column.
    """
    from sqlalchemy import text as _sql_text

    tracked_columns = [
        "title", "meeting_status", "meeting_started_at", "meeting_ended_at",
        "meeting_date", "organizer_email", "attendee_emails", "summary",
        "user_id", "tenant_id", "person_transcript", "hand_raises",
        "chat_messages", "speaker_source",
    ]

    result = await session.execute(
        _sql_text("SELECT meeting_id, " + ", ".join(tracked_columns) + " FROM ai_meeting_master ORDER BY meeting_id")
    )
    rows = result.mappings().all()

    total = len(rows)
    column_nulls: dict = {col: [] for col in tracked_columns}

    for row in rows:
        for col in tracked_columns:
            val = row.get(col)
            if val is None or (isinstance(val, str) and not val.strip()):
                column_nulls[col].append(row["meeting_id"])

    report = {}
    for col in tracked_columns:
        null_ids = column_nulls[col]
        report[col] = {
            "null_count":   len(null_ids),
            "filled_count": total - len(null_ids),
            "fill_pct":     round((total - len(null_ids)) / total * 100, 1) if total else 0,
            "null_meeting_ids": null_ids[:50],  # cap at 50 to keep response readable
        }

    return {
        "total_rows": total,
        "columns":    report,
    }

