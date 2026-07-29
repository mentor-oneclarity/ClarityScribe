from typing import Optional

from pydantic import BaseModel


class MeetingCreate(BaseModel):
    meet_code: str
    title: Optional[str] = None
    organizer_email: Optional[str] = None
    attendee_emails: Optional[str] = None  # comma-separated; BCC on summary email


class ParticipantUpsert(BaseModel):
    name: str
    is_active: bool = True


class TranscriptAdd(BaseModel):
    speaker_name: Optional[str] = None
    text: str
    source: str = "caption"
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    language_code: Optional[str] = None
    # Speaker attribution metadata (optional — older bots omit these)
    speaker_source: Optional[str] = None
    speaker_confidence: Optional[float] = None
    alignment_overlap_ms: Optional[int] = None
    is_overlapping_speech: Optional[bool] = None
    secondary_speakers_json: Optional[list] = None
    confidence: Optional[str] = None  # "high" | "low" (crosstalk quality flag)


class BotJoinRequest(BaseModel):
    meet_url: str
    title: Optional[str] = None
    organizer_email: Optional[str] = None  # summary TO: email
    attendee_emails: Optional[str] = None  # summary BCC: comma-separated attendee emails


class LoginClickRequest(BaseModel):
    x: int
    y: int


class LoginTypeRequest(BaseModel):
    text: str


class LoginKeyRequest(BaseModel):
    key: str


class LoginNavRequest(BaseModel):
    url: str


class MeetingEmailsUpdate(BaseModel):
    organizer_email: Optional[str] = None   # TO: on summary email
    attendee_emails: Optional[str] = None   # BCC: comma-separated

class SummarizeRequest(BaseModel):
    attendee_emails: Optional[str] = None   # extra BCC recipients (merged with stored ones)

class PushSummaryRequest(BaseModel):
    # Bot-collected events with no transactional table of their own — passed
    # straight through into ai_meeting_master at push-summary time.
    hand_raises: Optional[list] = None      # [{speaker, raised_at, lowered_at}, ...]
    chat_messages: Optional[list] = None    # [{sender, text, sent_at}, ...]

class SignupRequest(BaseModel):
    email: str
    password: str
    name: str


class LoginRequest(BaseModel):
    email: str
    password: str
