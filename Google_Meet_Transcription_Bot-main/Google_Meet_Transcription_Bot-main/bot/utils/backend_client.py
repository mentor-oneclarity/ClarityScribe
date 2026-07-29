import json
import os
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

from .logger import get_logger

load_dotenv()

logger = get_logger("bot")
BACKEND_URL: str = os.getenv("BACKEND_URL", "http://localhost:6050")
LOCAL_MODE: bool = os.getenv("LOCAL_MODE", "false").lower() == "true"


class BackendClient:
    """
    Thin async HTTP wrapper around the FastAPI backend.
    When LOCAL_MODE=true all write calls are no-ops (transcripts go to uploads/).
    """

    def __init__(
        self,
        local_transcript_path: Optional[Path] = None,
        local_mode: Optional[bool] = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=BACKEND_URL,
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0),
        )
        self._local_path = local_transcript_path
        self._local_mode = LOCAL_MODE if local_mode is None else local_mode

    # ── Meetings ───────────────────────────────────────────────────────────────

    async def start_meeting(
        self,
        meet_code: str,
        title: Optional[str] = None,
        organizer_email: Optional[str] = None,
        attendee_emails: Optional[str] = None,
    ) -> dict:
        if self._local_mode:
            logger.info("[local] start_meeting skipped")
            return {"id": 0}
        resp = await self._post(
            "/meetings/start",
            {
                "meet_code": meet_code,
                "title": title or f"Meeting {meet_code}",
                "organizer_email": organizer_email,
                "attendee_emails": attendee_emails,
            },
        )
        logger.info("Meeting started → id=%s", resp.get("id"))
        return resp

    async def end_meeting(self, meeting_id: int) -> dict:
        if self._local_mode:
            logger.info("[local] end_meeting skipped")
            return {}
        resp = await self._client.patch(f"/meetings/{meeting_id}/end")
        resp.raise_for_status()
        logger.info("Meeting ended → id=%d", meeting_id)
        return resp.json()

    # ── Participants ───────────────────────────────────────────────────────────

    async def add_participant(
        self, meeting_id: int, name: str, is_active: bool = True
    ) -> dict:
        if self._local_mode:
            logger.info("[local] participant: %s", name)
            return {}
        return await self._post(
            f"/meetings/{meeting_id}/participants",
            {"name": name, "is_active": is_active},
        )

    # ── Transcript ─────────────────────────────────────────────────────────────

    async def add_transcript(
        self,
        meeting_id: int,
        text: str,
        speaker_name: Optional[str] = None,
        source: str = "caption",
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        language_code: Optional[str] = None,
        speaker_source: Optional[str] = None,
        speaker_confidence: Optional[float] = None,
        alignment_overlap_ms: Optional[int] = None,
        is_overlapping_speech: Optional[bool] = None,
        secondary_speakers_json: Optional[list] = None,
        confidence: Optional[str] = None,
    ) -> dict:
        if self._local_mode:
            entry = {
                "speaker": speaker_name or "Unknown",
                "text": text,
                "source": source,
                "start": start_time,
                "end": end_time,
                "language_code": language_code,
                "speaker_source": speaker_source,
                "speaker_confidence": speaker_confidence,
            }
            logger.info("[local] transcript [%s] %s: %s", source, entry["speaker"], text[:80])
            if self._local_path:
                with open(self._local_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return {}
        payload: dict = {
            "speaker_name": speaker_name,
            "text": text,
            "source": source,
            "start_time": start_time,
            "end_time": end_time,
        }
        if language_code is not None:
            payload["language_code"] = language_code
        if speaker_source is not None:
            payload["speaker_source"] = speaker_source
        if speaker_confidence is not None:
            payload["speaker_confidence"] = speaker_confidence
        if alignment_overlap_ms is not None:
            payload["alignment_overlap_ms"] = alignment_overlap_ms
        if is_overlapping_speech is not None:
            payload["is_overlapping_speech"] = is_overlapping_speech
        if secondary_speakers_json is not None:
            payload["secondary_speakers_json"] = secondary_speakers_json
        if confidence is not None:
            payload["confidence"] = confidence
        return await self._post(f"/meetings/{meeting_id}/transcript", payload)

    # ── Summary ────────────────────────────────────────────────────────────────

    async def push_meeting_summary(
        self,
        meeting_id: int,
        hand_raises: Optional[list] = None,
        chat_messages: Optional[list] = None,
    ) -> dict:
        if self._local_mode:
            logger.info("[local] push_meeting_summary skipped")
            return {}
        _body: dict = {}
        if hand_raises:
            _body["hand_raises"] = hand_raises
        if chat_messages:
            _body["chat_messages"] = chat_messages
        resp = await self._client.post(
            f"/meetings/{meeting_id}/push-summary",
            json=_body if _body else None,
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0),
        )
        resp.raise_for_status()
        data = resp.json()
        logger.info(
            "push-summary: meeting_id=%d → upserted into ai_meeting_master "
            "(%d transcript entries, %d hand-raises, %d chat messages)",
            meeting_id, data.get("transcript_count", 0),
            data.get("hand_raise_count", 0), data.get("chat_message_count", 0),
        )
        return data

    async def push_meeting_master(self, meeting_id: int) -> dict:
        """Deprecated — calls push_meeting_summary."""
        return await self.push_meeting_summary(meeting_id)

    async def update_meeting_title(self, meeting_id: int, title: str) -> dict:
        if self._local_mode:
            logger.info("[local] update_meeting_title skipped")
            return {}
        resp = await self._client.patch(
            f"/meetings/{meeting_id}/title",
            json={"title": title},
        )
        resp.raise_for_status()
        return resp.json()

    async def update_meeting_emails(
        self,
        meeting_id: int,
        organizer_email: Optional[str] = None,
        attendee_emails: Optional[str] = None,
    ) -> dict:
        if self._local_mode:
            logger.info("[local] update_meeting_emails skipped")
            return {}
        body: dict = {}
        if organizer_email is not None:
            body["organizer_email"] = organizer_email
        if attendee_emails is not None:
            body["attendee_emails"] = attendee_emails
        if not body:
            return {}
        resp = await self._client.patch(f"/meetings/{meeting_id}/emails", json=body)
        resp.raise_for_status()
        data = resp.json()
        logger.info(
            "update_meeting_emails: meeting_id=%d → attendees saved (%s)",
            meeting_id, attendee_emails or "(none)",
        )
        return data

    async def summarize_meeting(self, meeting_id: int, attendee_emails: Optional[str] = None) -> dict:
        if self._local_mode:
            logger.info("[local] summarize_meeting skipped")
            return {}
        _body: dict = {}
        if attendee_emails:
            _body["attendee_emails"] = attendee_emails
            logger.info(
                "summarize_meeting: passing %d attendee(s) from bot memory as fallback for meeting %d",
                len([e for e in attendee_emails.split(",") if e.strip()]),
                meeting_id,
            )
        resp = await self._client.post(
            f"/meetings/{meeting_id}/summarize",
            json=_body if _body else None,
            timeout=httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0),
        )
        # 400 = no transcript segments (meeting was silent / nobody spoke).
        # This is not retryable — raise a specific error so the caller can
        # log it clearly instead of treating it as a transient failure.
        if resp.status_code == 400:
            logger.warning(
                "Summary skipped for meeting %d — no transcript (meeting was empty or silent). "
                "No email will be sent.",
                meeting_id,
            )
            raise ValueError(f"no_transcript:meeting_id={meeting_id}")
        resp.raise_for_status()
        data = resp.json()
        logger.info("Summary generated for meeting %d (%d chars)", meeting_id, len(data.get("summary") or ""))
        return data

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _post(self, path: str, payload: dict) -> dict:
        try:
            resp = await self._client.post(path, json=payload)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("HTTP %s for %s: %s", exc.response.status_code, path, exc.response.text)
            raise
        except Exception as exc:
            logger.error("Request failed for %s: %s", path, exc, exc_info=True)
            raise

    async def close(self) -> None:
        await self._client.aclose()
