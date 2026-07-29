"""
Google Meet Playwright Bot — Full 3-strategy speaker resolution
──────────────────────────────────────────────────────────────
Joins a meeting, enables captions, and streams transcript + participant data
to the FastAPI backend via HTTP.

Speaker resolution cascade for every Sarvam STT segment:
  1. Text fuzzy match  (difflib ≥ 20%)
  2. Time overlap      (caption_timeline ±8 s window)
  3. First-seen order  (next unassigned participant name)
  4. Fallback          (keep "speaker_N" label as-is)
"""

import asyncio
import difflib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
    async_playwright,
)

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
import numpy as np

from bot.utils.audio_capture import CHUNK_SECONDS, SAMPLERATE, AudioCapturer, to_wav
from bot.utils.backend_client import BackendClient
from bot.utils.logger import get_logger
from bot.utils.sarvam_client import SarvamClient
from bot.utils.speaker_alignment import (
    build_alias_map,
    normalize_speaker_name,
    split_chunk_by_speaker,
)
from bot.utils.webrtc_audio import WEBRTC_INTERCEPTOR_JS, WebRTCAudioCapturer

try:
    from bot.utils.diarizer import SpeakerDiarizer as _SpeakerDiarizer  # optional
    _DIARIZER_AVAILABLE = True
except Exception:
    _SpeakerDiarizer = None
    _DIARIZER_AVAILABLE = False

load_dotenv()

logger = get_logger("bot")

PROFILE_DIR = str(Path(__file__).parent.parent / "bot_browser_profile")
HEADLESS: bool = os.getenv("HEADLESS", "false").lower() == "true"
# LOCAL_MODE=true → skips backend API calls; saves transcripts to uploads/transcript_<code>.jsonl
LOCAL_MODE: bool = os.getenv("LOCAL_MODE", "false").lower() == "true"
POLL_INTERVAL: float = 1.0

# Context audio prepended to each Sarvam chunk (4 s)
# Context audio prepended to sounddevice chunks.
# Set to 0 — overlapping context causes duplicate words in transcript.
# WebRTC path (primary) doesn't use context at all.
_CONTEXT_SEC: int = 0
_CONTEXT_SAMPLES: int = SAMPLERATE * _CONTEXT_SEC

# 3-strategy tuning
_TIMELINE_MAX: int = 300          # rolling window (≈ 5 min at 1 s poll)
_TIMELINE_WINDOW_SEC: float = 8.0 # strategy 2 ±window
_FUZZY_THRESHOLD: float = 0.20    # strategy 1 minimum ratio

# Caption-overlap attribution thresholds
_CAPTION_DOMINANT_RATIO: float = 0.70   # ≥70% overlap → single-speaker attribution
_CAPTION_MIN_OVERLAP_SEC: float = 0.3   # ignore captions with < 0.3s overlap
_CAPTION_SECONDARY_MIN_SEC: float = 0.5 # record secondary speaker if ≥ 0.5s overlap
_CAPTION_AMBIGUOUS_RATIO: float = 0.80  # if runner-up ≥ 80% of top overlap → captions
                                        # can't tell the speakers apart (crosstalk tie)
_TRACK_LEARN_MIN_CONF: float = 0.50     # only learn a track→speaker lock from a caption
                                        # match this confident or better
_CAPTION_STALE_FINALIZE_SEC: float = 5.0  # caption text unchanged this long → utterance
                                          # finished; finalize it into _caption_timeline
# ── Measured-speaker (WebRTC SSRC/CSRC + audioLevel) attribution ─────────────
_SSRC_SPEAKING_LEVEL: float = 0.02   # audioLevel ≥ this → the source is really speaking
_SSRC_TIMELINE_MAX: int = 6000       # rolling window of per-source level samples
_SSRC_LEARN_MIN_LEVEL: float = 0.05  # only learn source→name when clearly speaking

# ── System messages to ignore (Google Meet UI notifications, not speech) ────
_SYSTEM_MESSAGE_RE = re.compile(
    r"(You have joined the call"
    r"|There are \d+ other people"
    r"|You are the only one"
    r"|Your camera is (on|off)"
    r"|Your microphone is (on|off)"
    r"|turned on captions"
    r"|turned off captions"
    r"|is presenting|stopped presenting"
    r"|joined the call|left the call"
    r"|has joined|has left"
    r"|joined the meeting|left the meeting"
    r"|entered the meeting|exited the meeting"
    r"|You are now (muted|unmuted)"
    r"|is now (muted|unmuted)"
    r"|Waiting for (others|people|someone|everyone)"
    r"|No one else is (here|in this call)"
    r"|You're the only (one|person)"
    r"|Nobody else is here"
    r"|Everyone has left"
    r"|The call has ended"
    r"|Recording (started|stopped|in progress)"
    r"|Live (streaming|captions) (on|off|started|stopped)"
    r"|https?://)",
    re.IGNORECASE,
)

# ── UI garbage filter ─────────────────────────────────────────────────────────

_UI_GARBAGE_RE = re.compile(
    r"(language|format_size|Font size|Font colour|Open caption settings"
    r"|circle|settings\b"
    r"|arrow_downward|arrow_upward|arrow_forward|arrow_back"
    r"|close|more_vert|more_horiz|expand_more|expand_less"
    r"|chevron_right|chevron_left|check|done|add|remove"
    r"|mic_off|videocam_off|screen_share|stop_screen_share"
    r"|Stop sharing|Start sharing|Stop presenting|You are sharing"
    r"|You are presenting|Someone is presenting|is sharing their screen"
    r"|zoom_in|zoom_out|open_in_new|open_in_full|close_fullscreen"
    r"|Enter Full Screen|Open in new window|Zoom in|Zoom out"
    r"|thumb_up|thumb_down|emoji_|sentiment_)",
    re.IGNORECASE,
)
# Accepts any script: Latin (en), Devanagari (hi/mr), Malayalam, Bengali, etc.
# The original [A-Za-z]{2,} silently dropped all non-Latin transcripts (Hindi, Marathi, etc.)
_HAS_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)


def _is_garbage(text: str) -> bool:
    if not text or len(text.strip()) < 2:
        return True
    t = text.strip()
    if _SYSTEM_MESSAGE_RE.search(t):
        return True
    if _UI_GARBAGE_RE.search(t):
        return True
    if not _HAS_WORD_RE.search(t):
        return True
    # Reject single-word utterances shorter than 4 chars (noise like "um", "ah", "ok")
    _words = t.split()
    if len(_words) == 1 and len(_words[0]) < 4:
        return True
    # Participant name roster: "Name1 Name2 Name1 Name2 Name3" — Google Meet renders
    # the tile area as captions. Detect: 4+ words, no sentence punctuation, ≥85% capitalised.
    if not re.search(r'[.!?,;]', t):
        if len(_words) >= 4:
            _cap = sum(1 for w in _words if w and w[0].isupper())
            if _cap / len(_words) >= 0.85:
                return True
    return False


# ── Caption finaliser ─────────────────────────────────────────────────────────
#
# Google Meet updates captions incrementally:
#   tick 1: "Hello"
#   tick 2: "Hello how"
#   tick 3: "Hello how are you?"
#   tick 4: (cleared / new speaker)
#
# We buffer per-speaker and only SAVE when:
#   a) text gets much shorter (new sentence started), OR
#   b) speaker disappears for 2+ ticks (stopped talking)

class CaptionFinaliser:
    def __init__(self) -> None:
        self._pending: Dict[str, dict] = {}
        self._saved: Set[str] = set()

    def update(self, current: Dict[str, str]) -> list[tuple[str, str]]:
        """Call every poll tick with {speaker: latest_text}.
        Returns list of (speaker, text) pairs that are finalised."""
        to_save: list[tuple[str, str]] = []

        for spk in self._pending:
            self._pending[spk]["ticks_unseen"] += 1

        for speaker, text in current.items():
            text = text.strip()
            if _is_garbage(text):
                continue
            # Participant name tile: caption text IS the speaker's own name
            if text.lower() == speaker.lower():
                continue
            # Calendar event banner: "{speaker / meeting-title} in N minutes"
            if (text.lower().startswith(speaker.lower())
                    and re.search(r'\bin \d+ minutes?\s*$', text, re.IGNORECASE)):
                continue

            if speaker not in self._pending:
                self._pending[speaker] = {"text": text, "ticks_unseen": 0}
                continue

            prev = self._pending[speaker]["text"]
            self._pending[speaker]["ticks_unseen"] = 0

            if text == prev:
                continue

            if len(text) < len(prev) * 0.6:
                to_save.append((speaker, prev))
                self._pending[speaker]["text"] = text
            else:
                self._pending[speaker]["text"] = text

        done = [s for s, v in self._pending.items() if v["ticks_unseen"] >= 2]
        for speaker in done:
            text = self._pending.pop(speaker)["text"]
            if not _is_garbage(text):
                to_save.append((speaker, text))

        unique: list[tuple[str, str]] = []
        for speaker, text in to_save:
            # Use an 80-char normalised fingerprint as the dedup key so that
            # Google Meet's growing caption bubble (same opening, longer tail on
            # each pause) only produces ONE save instead of one per pause-cycle.
            _fp = " ".join(text.lower().split())[:80]
            key = f"{speaker}::{_fp}"
            if key not in self._saved:
                self._saved.add(key)
                unique.append((speaker, text))
                if len(self._saved) > 2000:
                    self._saved = set(list(self._saved)[1000:])

        return unique

    def flush(self) -> list[tuple[str, str]]:
        """Save everything still pending (call on bot shutdown)."""
        to_save = []
        for speaker, v in list(self._pending.items()):
            text = v["text"]
            if not _is_garbage(text):
                _fp = " ".join(text.lower().split())[:80]
                key = f"{speaker}::{_fp}"
                if key not in self._saved:
                    self._saved.add(key)
                    to_save.append((speaker, text))
        self._pending.clear()
        return to_save


# ── Bot ───────────────────────────────────────────────────────────────────────

class MeetBot:
    def __init__(
        self,
        meet_url: str,
        profile_dir: Optional[str] = None,
        title: Optional[str] = None,
        organizer_email: Optional[str] = None,
        attendee_emails: Optional[str] = None,
    ) -> None:
        self.meet_url = meet_url.strip()
        self.meet_code = self._extract_code(meet_url)
        self.meeting_id: Optional[int] = None
        self._local_transcript_path: Optional[Path] = None
        self.client = BackendClient()
        self.caption_buf = CaptionFinaliser()
        self.page: Optional[Page] = None
        self.context: Optional[BrowserContext] = None
        self._running = False
        self.sarvam = SarvamClient()
        self.audio = AudioCapturer()
        self.diarizer = _SpeakerDiarizer() if _DIARIZER_AVAILABLE else None
        self._last_active_speaker: Optional[str] = None
        self._profile_dir: str = profile_dir or PROFILE_DIR
        self._title: Optional[str] = title
        self._organizer_email: Optional[str] = organizer_email
        self._attendee_emails: Optional[str] = attendee_emails  # comma-sep; BCC on summary email
        self._browser = None  # non-persistent browser instance (guest mode)
        self._join_failure_reason: str = ""  # set by _join_meeting() before returning False
        self._meeting_end_streak: int = 0  # consecutive ticks where end condition detected
        self._stop_requested: bool = False  # set by request_stop() for graceful manual stop
        self._alone_ticks: int = 0        # consecutive ticks with no other participants visible
        self._has_seen_participants: bool = False  # True once any non-bot participant joins
        self._participants_first_seen_at: Optional[float] = None  # wall clock when first person joined
        # Wall-clock time of last confirmed human presence (DOM tile / caption / active speaker).
        # Used by Layer 4 in _count_live_participants to keep bot in a silent meeting.
        self._last_confirmed_presence_time: Optional[float] = None
        self._dom_zero_log_at: float = 0.0   # throttle DOM=0 log to once per 30s
        # Authoritative roster (Google Meet people-panel) state. When the panel can
        # be read it gives the TRUE count of people in the call — including muted
        # attendees — and drops to 0 the moment everyone actually leaves. This is
        # the primary signal; the DOM-tile / caption / grace-window layers are the
        # fallback for when the panel can't be opened/read.
        self._roster_reliable: bool = False         # True once we read the panel at least once
        self._roster_open_failures: int = 0         # consecutive failures opening the panel
        self._roster_panel_logged: bool = False
        self._roster_open_attempt_at: float = 0.0   # throttle panel-open clicks (every 15s max)
        # Minimum seconds participants must be present before "meeting started" is declared.
        # If everyone leaves before this threshold, treat as a false start and reset to
        # pre-meeting timeout so the bot keeps waiting for the real attendees.
        self._MIN_MEETING_DURATION_SEC: float = 30.0   # 30s — short enough to catch real false-starts

        # ── 3-strategy speaker resolution state ──────────────────────────────
        self._speaker_map: Dict[str, str] = {}
        # Labels in this set were resolved with high confidence (fuzzy ≥50%).
        # Low-confidence mappings (strategies 2 & 3) are NOT in this set and will
        # be re-evaluated when new participants appear.
        self._speaker_map_confident: set = set()

        # Rolling window of caption events with start/end times.
        # Each entry: {start_time, end_time, speaker, text}
        # Used for overlap-based STT speaker attribution.
        self._caption_timeline: List[dict] = []

        # Speakers currently visible in captions (not yet finalized).
        # speaker → {start_time, end_time, text}
        self._active_captions: Dict[str, dict] = {}

        # Last caption text finalized per speaker. Google Meet leaves the final
        # caption bubble on screen indefinitely; this prevents re-finalizing the
        # same frozen text every few seconds (which would create duplicates and
        # keep a departed speaker counted as "live").
        self._last_finalized_caption: Dict[str, str] = {}

        # ── Measured speaker attribution (WebRTC RTP source id + audioLevel) ──
        # The JS poller samples each receiver's getSynchronizationSources() every
        # 250ms. Each RTP source id (SSRC/CSRC) is stable per participant, so this
        # tells us WHO actually produced audio — the true signal that replaces
        # caption-overlap guessing.
        #   _source_speaker_map: canonical source-id → learned participant name
        #   _source_locked: sources bound by a CONFIDENT caption (DOM votes can't override)
        #   _source_name_votes: (source,name) → agreement count for DOM-based learning
        #   _measured_speaker_timeline: [{t_ms, track, source, level, name}] rolling window
        # A "canonical source" is a namespaced tuple ('c', csrc) or ('s', ssrc) so a
        # CSRC int can never alias an SSRC int in the same map.
        self._source_speaker_map: Dict[tuple, str] = {}
        self._source_locked: set = set()
        self._source_name_votes: Dict[tuple, int] = {}
        self._measured_speaker_timeline: List[dict] = []
        self._measured_signal_logged: bool = False  # one-time LIVE/DEAD log
        self._p0_hits: int = 0
        self._p0_misses: int = 0

        # Active speaker timeline — populated by DOM highlight detection every 500ms.
        # Each entry: {abs_time: float, speaker: str}
        # More reliable than captions for STT attribution (visual = who is actually speaking).
        self._active_speaker_timeline: List[dict] = []

        # All real participant names ever seen in DOM (for strategy 3).
        # List (not set) so insertion/join order is preserved for track-order assignment.
        self._known_participants: List[str] = []

        # 1d: alias_map — maps name variants to canonical form, rebuilt on each new participant
        self._alias_map: dict = {}

        # 1e: pending caption buffer — captions wait here up to _CAP_PENDING_TTL_SEC
        # for a matching STT chunk to arrive so they can be merged into one DB row.
        self._cap_pending: List[dict] = []  # [{speaker, text, abs_start, abs_end, queued_at}]
        # Must be > CHUNK_SEC (15s) so captions survive until their STT chunk arrives.
        # Too short → caption flushed before STT → two DB rows per utterance.
        self._CAP_PENDING_TTL_SEC: float = 22.0

        # Wall-clock time when the meeting was joined (for absolute timestamps).
        self.meeting_start_time: Optional[float] = None

        # ── Hand-raise + chat capture (accumulated in memory, flushed once at
        # teardown straight into ai_meeting_master — see _read_roster_others()
        # for hand-raise detection and _scrape_chat_messages() for chat) ─────
        self._hand_raise_state: Dict[str, float] = {}   # name -> raised_at (still up)
        self._hand_raise_events: List[dict] = []        # [{speaker, raised_at, lowered_at}]
        self._chat_messages: List[dict] = []            # [{sender, text, sent_at}]
        self._chat_panel_opened: bool = False

    # ── Calendar attendee lookup ─────────────────────────────────────────────

    async def _fetch_attendees_from_calendar(self) -> Optional[str]:
        """
        Look up the Google Calendar event that matches this meeting's meet_code
        and return a comma-separated string of attendee email addresses.
        Returns None if the calendar token is unavailable or no event is found.
        Called after the meeting DB record is created so we can patch attendee_emails.
        """
        token_file = Path(__file__).parent.parent.parent / "bot" / "calendar_token.json"
        if not token_file.exists():
            return None
        try:
            import concurrent.futures as _cf
            from datetime import datetime as _datetime, timedelta as _timedelta, timezone as _tz

            def _lookup():
                from google.auth.transport.requests import Request as _GRequest
                from google.oauth2.credentials import Credentials as _Creds
                from googleapiclient.discovery import build as _build

                creds = _Creds.from_authorized_user_file(
                    str(token_file), ["https://www.googleapis.com/auth/calendar.readonly"]
                )
                if creds.expired and creds.refresh_token:
                    creds.refresh(_GRequest())
                    token_file.write_text(creds.to_json())

                service = _build("calendar", "v3", credentials=creds)
                now = _datetime.now(_tz.utc)
                # Search events in a ±3h window around now
                result = service.events().list(
                    calendarId="primary",
                    timeMin=(now - _timedelta(hours=3)).isoformat(),
                    timeMax=(now + _timedelta(hours=3)).isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=20,
                ).execute()

                code = self.meet_code.lower()
                for event in result.get("items", []):
                    # Check all text fields for the meet_code
                    haystack = " ".join([
                        event.get("description") or "",
                        event.get("location") or "",
                        event.get("summary") or "",
                        str(event.get("conferenceData") or ""),
                    ]).lower()
                    if code not in haystack:
                        continue
                    # Collect attendee emails (skip organiser to avoid duplicate)
                    organizer_email = (event.get("organizer") or {}).get("email", "")
                    emails = [
                        a["email"]
                        for a in event.get("attendees") or []
                        if a.get("email") and a["email"] != organizer_email
                        and not a.get("resource")  # skip room resources
                    ]
                    return ",".join(emails) if emails else None
                return None

            loop = asyncio.get_running_loop()
            with _cf.ThreadPoolExecutor(max_workers=1) as pool:
                attendees = await asyncio.wait_for(
                    loop.run_in_executor(pool, _lookup), timeout=8.0
                )
            if attendees:
                logger.info(
                    "Calendar lookup: found %d attendee(s) for meet_code=%s",
                    len(attendees.split(",")), self.meet_code,
                )
            return attendees
        except Exception as exc:
            logger.debug("Calendar attendee lookup failed: %s", exc)
            return None

    # ── 3-Strategy speaker resolution ────────────────────────────────────────

    def _resolve_speaker_name(
        self,
        speaker_label: str,
        text: str,
        abs_start: float,
        abs_end: float,
        timeline_snapshot: List[dict],
    ) -> str:
        """
        Map a Sarvam speaker label ("speaker_1") to a real participant name.

        Strategy 1 — Text fuzzy match:
          Compare Sarvam segment text against caption_timeline entries using
          difflib.SequenceMatcher (threshold ≥ 20%).

        Strategy 2 — Time overlap match:
          Find caption_timeline entry closest to segment midpoint within ±8 s.

        Strategy 3 — First-seen order:
          Take the next unassigned name from _known_participants.

        Fallback: return the label as-is ("speaker_1").
        """
        # High-confidence cache hit — skip re-evaluation
        if speaker_label in self._speaker_map_confident:
            return self._speaker_map[speaker_label]

        # ── Strategy 1: text fuzzy match ─────────────────────────────────────
        best_ratio = 0.0
        best_speaker: Optional[str] = None
        text_lower = text.lower()

        for entry in timeline_snapshot:
            ratio = difflib.SequenceMatcher(
                None, text_lower, entry["text"].lower()
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_speaker = entry["speaker"]

        if best_ratio >= _FUZZY_THRESHOLD and best_speaker:
            self._speaker_map[speaker_label] = best_speaker
            if best_ratio >= 0.50:
                # High confidence — cache permanently
                self._speaker_map_confident.add(speaker_label)
                logger.info("S1 fuzzy %.0f%% (confident) → %s  [%r]",
                            best_ratio * 100, best_speaker, text[:60])
            else:
                logger.info("S1 fuzzy %.0f%% (low-conf) → %s  [%r]",
                            best_ratio * 100, best_speaker, text[:60])
            return best_speaker

        # ── Strategy 2: time overlap (closest caption midpoint ±8 s) ─────────
        midpoint = (abs_start + abs_end) / 2.0
        best_diff = float("inf")
        best_speaker = None

        for entry in timeline_snapshot:
            diff = abs(entry["abs_time"] - midpoint)
            if diff < best_diff and diff <= _TIMELINE_WINDOW_SEC:
                best_diff = diff
                best_speaker = entry["speaker"]

        if best_speaker:
            # Low-confidence — store but don't mark confident so it re-evaluates
            self._speaker_map[speaker_label] = best_speaker
            logger.info("S2 time Δ%.1fs (low-conf) → %s  [%r]",
                        best_diff, best_speaker, text[:60])
            return best_speaker

        # ── Strategy 3: first-seen order ─────────────────────────────────────
        already_assigned = set(self._speaker_map.values())
        for name in self._known_participants:
            if name not in already_assigned:
                # Low-confidence — store but don't mark confident
                self._speaker_map[speaker_label] = name
                logger.info("S3 first-seen (low-conf) → %s  for %s", name, speaker_label)
                return name

        # ── Fallback ─────────────────────────────────────────────────────────
        logger.warning("No resolution for %s — keeping label", speaker_label)
        return speaker_label

    # ── Run ───────────────────────────────────────────────────────────────────

    def request_stop(self) -> None:
        """Signal a GRACEFUL shutdown. The poll loops exit on their next tick and
        run() falls into `finally: _teardown()`, which ends the meeting and sends
        the summary email. Used by BotManager.stop() for manual UI stops so the
        summary is generated and the meeting is marked completed — unlike a hard
        task.cancel(), which would interrupt teardown mid-way."""
        logger.info("request_stop() — bot %s will finish current tick then tear down", self.meet_code)
        self._running = False
        self._stop_requested = True

    async def run(self) -> None:
        logger.info("=" * 60)
        logger.info("BOT STARTING — meet_code=%s", self.meet_code)
        logger.info("Profile dir: %s", self._profile_dir)
        logger.info("Headless: %s", HEADLESS)
        # ── System diagnostics — logged once at bot start ──────────────────
        _display = os.environ.get("DISPLAY", "not_set")
        _pulse_source = os.environ.get("PULSE_SOURCE", "not_set")
        logger.info("[diag] DISPLAY=%s  PULSE_SOURCE=%s  platform=%s", _display, _pulse_source, sys.platform)
        # Check PulseAudio health
        try:
            import subprocess as _sp
            _pa = _sp.run(["pactl", "get-default-sink"], capture_output=True, text=True, timeout=3)
            logger.info("[diag] PulseAudio default sink: %s", _pa.stdout.strip() or "UNKNOWN")
            _pa_src = _sp.run(["pactl", "get-default-source"], capture_output=True, text=True, timeout=3)
            logger.info("[diag] PulseAudio default source: %s", _pa_src.stdout.strip() or "UNKNOWN")
        except Exception as _e:
            logger.warning("[diag] PulseAudio check failed: %s — audio STT may not work", _e)
        logger.info("=" * 60)
        _is_linux = sys.platform.startswith("linux")

        if _is_linux:
            # Base args for all Linux/Docker modes
            _chromium_args = [
                "--use-fake-ui-for-media-stream",  # Bypass mic/cam permission dialogs
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1280,720",
                "--disable-extensions",
                "--disable-gpu",
            ]
            if HEADLESS:
                # Headless mode: fake device lets WebRTC receive incoming audio tracks.
                # Chrome doesn't have a real output device in headless, so the fake device
                # enables WebRTC track reception that the JS interceptor records.
                _chromium_args += [
                    "--use-fake-device-for-media-stream",
                    "--disable-software-rasterizer",
                ]
            # Headful (Xvfb) mode: DO NOT add --use-fake-device-for-media-stream.
            # That flag replaces Chrome's audio output with a silent fake device,
            # preventing incoming Meet audio from reaching PulseAudio/MeetingSink.
            # Without it, Chrome routes all participant audio to PulseAudio → ffmpeg captures all speakers.
        else:
            # macOS/Windows: no fake-device (causes WebRTC "network connection lost"),
            # no web-security flags (bot-detection signals on real Chrome builds)
            _chromium_args = [
                "--use-fake-ui-for-media-stream",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1280,720",
            ]

        # Log exact Chrome args so we can verify fake-device is absent in headful mode
        logger.info("[diag] Chrome args: %s", " ".join(_chromium_args))
        if "--use-fake-device-for-media-stream" in _chromium_args:
            logger.info("[diag] Audio mode: HEADLESS fake-device (WebRTC JS interceptor — active speaker only)")
        else:
            logger.info("[diag] Audio mode: HEADFUL PulseAudio (ffmpeg MeetingSink.monitor — ALL speakers)")

        # Stealth script — hides Playwright/Chromium automation signals
        _stealth_js = """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
            window.chrome = { runtime: {} };
        """

        # MutationObserver script — fires on every caption DOM change.
        # Eliminates 1-second polling lag: even 200ms speaker switches are captured.
        # Buffer is drained by _scrape_captions() on every poll tick.
        _caption_observer_js = """
        (function() {
            if (window.__captionObserverActive) return;
            window.__captionBuffer = [];
            window.__captionObserverActive = false;

            // Google Meet caption selectors — confirmed from live DevTools inspection.
            // STRATEGY: query individual per-speaker blocks first (most precise),
            // then fall back to the whole container.
            //
            // Confirmed live DOM structure (2026-06-10):
            //   div.a4cQT.P9KVBf          ← whole caption panel
            //     div.nMcdL.bj4p3b        ← ONE block per speaker (name + text)
            //       div.ygicle.VbkSUe     ← text content
            var CAPTION_SELECTORS = [
                // ── Confirmed working (from live DevTools) ──────────────────
                '.nMcdL.bj4p3b',      // individual speaker block — most precise
                '.nMcdL',             // same without second class (version fallback)
                '.ygicle.VbkSUe',     // caption text element inside block
                // ── Whole caption container (fallback) ──────────────────────
                '.a4cQT',             // confirmed — whole caption panel
                '.a4cQT.P9KVBf',      // with second class as seen in DevTools
                // ── Older / other known selectors ───────────────────────────
                '[jsname="tgaKEf"]',
                '[jsname="YSxPC"]',
                '.iTTPOb',
                '.TBMuR',
                '.CNusmb',
            ];

            function parseCaptionEl(el) {
                var raw = (el.innerText || el.textContent || '').trim();
                if (!raw || raw.length < 2) return null;

                var lines = raw.split('\\n').map(function(l) { return l.trim(); }).filter(Boolean);

                // If only one line, climb up to parent containers (up to 5 levels)
                // to find a block that has both speaker name and caption text.
                if (lines.length < 2) {
                    var parent = el.parentElement;
                    for (var d = 0; d < 5 && parent; d++) {
                        var pRaw = (parent.innerText || '').trim();
                        var pLines = pRaw.split('\\n').map(function(l) { return l.trim(); }).filter(Boolean);
                        if (pLines.length >= 2 && pLines[0].length <= 60) {
                            var pw = pLines[0].split(/\\s+/);
                            var pIsName = pw.length >= 1 && pw.length <= 5 &&
                                /^[A-Z]/.test(pw[0]) &&
                                pw.every(function(w) {
                                    return w.length > 0 && w[0] === w[0].toUpperCase() && w[0] !== w[0].toLowerCase();
                                });
                            if (pIsName) {
                                return { speaker: pLines[0], text: pLines.slice(1).join(' ') };
                            }
                        }
                        parent = parent.parentElement;
                    }
                }

                // Pattern 1: "Speaker Name: caption text"
                // Require first word to start with uppercase to avoid matching caption text.
                var colon = raw.indexOf(':');
                if (colon > 1 && colon < 50) {
                    var sp = raw.slice(0, colon).trim();
                    var words = sp.split(/\\s+/);
                    if (words.length >= 1 && words.length <= 5 && /^[A-Z]/.test(words[0])) {
                        return { speaker: sp, text: raw.slice(colon + 1).trim() };
                    }
                }

                // Pattern 2: multiline — first line = speaker name, rest = text
                if (lines.length >= 2) {
                    var first = lines[0];
                    var ws = first.split(' ');
                    var looksLikeName = ws.length <= 4 && ws.every(function(w) {
                        return w.length > 0 && w[0] === w[0].toUpperCase() && w[0] !== w[0].toLowerCase();
                    });
                    if (looksLikeName) {
                        return { speaker: first, text: lines.slice(1).join(' ') };
                    }
                }

                return { speaker: 'Unknown', text: raw };
            }

            function scanAndBuffer() {
                var now = Date.now();
                var found = false;

                // Primary: try known selectors
                for (var si = 0; si < CAPTION_SELECTORS.length; si++) {
                    var els = document.querySelectorAll(CAPTION_SELECTORS[si]);
                    if (!els.length) continue;
                    for (var ei = 0; ei < els.length; ei++) {
                        var entry = parseCaptionEl(els[ei]);
                        if (entry && entry.text && entry.text.length >= 2) {
                            window.__captionBuffer.push({
                                time: now,
                                speaker: entry.speaker,
                                text: entry.text
                            });
                            found = true;
                        }
                    }
                    if (found) break;
                }

                // Fallback: scan live caption panel.
                // Try aria-live="assertive" first — Google Meet uses this for speech captions.
                // Do NOT use aria-live="polite" — that is the notification area (join/leave)
                // which exists regardless of whether captions are enabled.
                if (!found) {
                    try {
                        var captionArea =
                            document.querySelector('[aria-live="assertive"]') ||
                            document.querySelector('[jscontroller="RVTGF"]') ||
                            document.querySelector('[jsname="eVSMFe"]') ||
                            document.querySelector('[role="log"]');
                        if (captionArea) {
                            var entry = parseCaptionEl(captionArea);
                            if (entry && entry.text && entry.text.length >= 2) {
                                window.__captionBuffer.push({
                                    time: now,
                                    speaker: entry.speaker,
                                    text: entry.text
                                });
                            }
                        }
                    } catch(e) {}
                }
                // Keep buffer bounded to last 1000 entries
                if (window.__captionBuffer.length > 1000) {
                    window.__captionBuffer = window.__captionBuffer.slice(-600);
                }
            }

            var rafPending = false;
            var _pendingMutations = [];

            // Use mutation records directly — selector-agnostic approach.
            // When Google Meet changes its CSS class names the selector list becomes
            // stale, but the mutation records still point to the exact DOM nodes that
            // changed. We walk up from each mutated node looking for a speaker+text
            // pair. This finds captions regardless of what CSS classes Google uses.
            var observer = new MutationObserver(function(mutations) {
                for (var mi = 0; mi < mutations.length; mi++) {
                    _pendingMutations.push(mutations[mi]);
                }
                // Cap the batch — avoid O(n²) walk on rapid DOM updates (e.g. scrolling)
                if (_pendingMutations.length > 150) {
                    _pendingMutations = _pendingMutations.slice(-80);
                }
                if (!rafPending) {
                    rafPending = true;
                    requestAnimationFrame(function() {
                        var now = Date.now();
                        var seenEls = [];
                        for (var mi = 0; mi < _pendingMutations.length; mi++) {
                            var target = _pendingMutations[mi].target;
                            var el = (target.nodeType === 3) ? target.parentElement : target;
                            for (var d = 0; d < 10 && el && el !== document.body; d++) {
                                var already = false;
                                for (var si = 0; si < seenEls.length; si++) {
                                    if (seenEls[si] === el) { already = true; break; }
                                }
                                if (already) { el = el.parentElement; continue; }
                                seenEls.push(el);
                                var entry = parseCaptionEl(el);
                                if (entry && entry.speaker !== 'Unknown' && entry.text.length >= 3) {
                                    window.__captionBuffer.push({ time: now, speaker: entry.speaker, text: entry.text });
                                    break;
                                }
                                el = el.parentElement;
                            }
                        }
                        _pendingMutations = [];
                        // Also do traditional selector scan as safety net
                        scanAndBuffer();
                        rafPending = false;
                    });
                }
            });

            function attach() {
                if (!document.body) return;
                try {
                    observer.disconnect();
                    observer.observe(document.body, {
                        childList: true, subtree: true, characterData: true
                    });
                    window.__captionObserverActive = true;
                } catch(e) {}
            }

            if (document.body) {
                attach();
            } else {
                document.addEventListener('DOMContentLoaded', attach);
            }

            // Reattach periodically in case Meet replaces the root DOM node
            setInterval(attach, 20000);
        })();
        """

        # In-call chat MutationObserver — mirrors _caption_observer_js above.
        # NOT verified against a live Meet DOM (no confirmed class names exist
        # for the chat panel yet, unlike the caption/people-panel selectors
        # elsewhere in this file). Uses text-content matching ("In-call
        # messages" heading) instead of CSS classes since that's far more
        # durable against Meet's frequent class-name churn — same principle
        # as the "People"/"Sign in" button matching used elsewhere.
        # TODO: confirm against a live meeting with real chat activity and
        # tighten the header/continuation parsing if needed.
        _chat_observer_js = r"""
        (function() {
            if (window.__chatObserverActive) return;
            window.__chatBuffer = [];
            window.__chatObserverActive = false;
            window.__chatLastSender = null;
            window.__chatSeenKeys = new Set();

            var TIME_RE = /\b\d{1,2}:\d{2}\s*(?:AM|PM)?\b/i;

            function findChatListRoot() {
                var heading = null;
                var all = document.querySelectorAll('h1,h2,h3,[role="heading"]');
                for (var i = 0; i < all.length; i++) {
                    var t = (all[i].innerText || '').trim().toLowerCase();
                    if (t === 'in-call messages' || t === 'chat with everyone' || t === 'chat') {
                        heading = all[i];
                        break;
                    }
                }
                if (!heading) return null;
                var panel = heading.closest('[role="complementary"]') || heading.parentElement;
                for (var d = 0; d < 6 && panel; d++) {
                    var list = panel.querySelector('[role="list"]');
                    if (list) return list;
                    panel = panel.parentElement;
                }
                return null;
            }

            function parseMessageEl(el) {
                var raw = (el.innerText || el.textContent || '').trim();
                if (!raw) return null;
                var lines = raw.split('\n').map(function(l) { return l.trim(); }).filter(Boolean);
                if (!lines.length) return null;

                // Header line: "Sender Name ... HH:MM AM/PM" — Meet only shows
                // this on the first message of a run from that sender.
                if (lines.length >= 2 && TIME_RE.test(lines[0]) && lines[0].length <= 60) {
                    var sender = lines[0].replace(TIME_RE, '').trim();
                    if (sender) {
                        window.__chatLastSender = sender;
                        return { sender: sender, text: lines.slice(1).join(' ') };
                    }
                }
                // No header — continuation message from the last known sender.
                if (window.__chatLastSender) {
                    return { sender: window.__chatLastSender, text: lines.join(' ') };
                }
                return { sender: 'Unknown', text: lines.join(' ') };
            }

            function scan() {
                var root = findChatListRoot();
                if (!root) return;
                var items = root.querySelectorAll('[role="listitem"]');
                for (var i = 0; i < items.length; i++) {
                    var msg = parseMessageEl(items[i]);
                    if (!msg || !msg.text) continue;
                    var key = msg.sender + '::' + msg.text;
                    if (window.__chatSeenKeys.has(key)) continue;
                    window.__chatSeenKeys.add(key);
                    window.__chatBuffer.push({ time: Date.now(), sender: msg.sender, text: msg.text });
                    if (window.__chatSeenKeys.size > 2000) {
                        window.__chatSeenKeys = new Set(Array.from(window.__chatSeenKeys).slice(-1000));
                    }
                }
                if (window.__chatBuffer.length > 1000) {
                    window.__chatBuffer = window.__chatBuffer.slice(-600);
                }
            }

            var rafPending = false;
            var observer = new MutationObserver(function() {
                if (rafPending) return;
                rafPending = true;
                requestAnimationFrame(function() {
                    scan();
                    rafPending = false;
                });
            });

            function attach() {
                if (!document.body) return;
                try {
                    observer.disconnect();
                    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
                    window.__chatObserverActive = true;
                } catch (e) {}
            }

            if (document.body) {
                attach();
            } else {
                document.addEventListener('DOMContentLoaded', attach);
            }
            setInterval(attach, 20000);
            // Safety-net poll in case the panel is hidden (display:none) and
            // stops firing mutation records while still receiving messages.
            setInterval(scan, 2000);
        })();
        """

        _ua = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            if _is_linux else
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        async with async_playwright() as pw:
            logger.info("[1/6] Launching Chromium (headless=%s) …", HEADLESS)
            try:
                _profile_populated = (
                    Path(self._profile_dir) / "Default" / "Cookies"
                ).exists()
                _guest_mode = os.getenv("BOT_GUEST_MODE", "false").lower() == "true"

                # On Linux/Docker: use launch + new_context (no persistent profile available).
                # On macOS/Windows local: use launch_persistent_context when login_once has
                # been run (profile populated), which gives Google a trusted browser identity
                # and avoids the accountchooser redirect loop after "Join now".
                if not _is_linux and _profile_populated and not _guest_mode:
                    logger.info(
                        "[1/6] Using persistent browser profile at %s "
                        "(avoids Google auth redirect on join)",
                        self._profile_dir,
                    )
                    self._browser = None
                    self.context = await pw.chromium.launch_persistent_context(
                        user_data_dir=self._profile_dir,
                        headless=HEADLESS,
                        args=_chromium_args,
                        viewport={"width": 1280, "height": 720},
                        permissions=["microphone", "camera"],
                        ignore_https_errors=True,
                        user_agent=_ua,
                    )
                else:
                    # Non-persistent: load session_state.json if available.
                    self._browser = await pw.chromium.launch(
                        headless=HEADLESS,
                        args=_chromium_args,
                    )
                    _session_file = Path(__file__).parent.parent / "session_state.json"
                    _storage_state = (
                        None if _guest_mode
                        else str(_session_file) if _session_file.exists() else None
                    )
                    if _guest_mode:
                        logger.info("[1/6] BOT_GUEST_MODE=true — joining as anonymous guest (no session)")
                    elif _storage_state:
                        logger.info("[1/6] Loading saved session from %s", _session_file)
                    else:
                        logger.info("[1/6] No saved session — bot will need to authenticate")

                    self.context = await self._browser.new_context(
                        viewport={"width": 1280, "height": 720},
                        permissions=["microphone", "camera"],
                        ignore_https_errors=True,
                        user_agent=_ua,
                        storage_state=_storage_state,
                    )

                await self.context.add_init_script(_stealth_js)
                await self.context.add_init_script(_caption_observer_js)
                await self.context.add_init_script(_chat_observer_js)
                await self.context.add_init_script(WEBRTC_INTERCEPTOR_JS)
                logger.info("[1/6] Chromium launched (stealth + caption + chat observers + WebRTC interceptor)")
            except Exception as exc:
                logger.error("[1/6] FAILED to launch Chromium: %s", exc, exc_info=True)
                return

            self.page = await self.context.new_page()
            self.page.set_default_timeout(30_000)

            # Pipe browser JS console.log/warn/error → Python logger
            def _on_console(msg):
                txt = msg.text
                if "[WebRTC capture] Started cycling recorder" in txt:
                    logger.info("JS: %s", txt)   # new track → always visible
                elif "[WebRTC capture]" in txt or "[caption observer]" in txt:
                    logger.debug("JS: %s", txt)  # install/cycle heartbeats → debug only
                elif msg.type == "error":
                    logger.warning("JS error: %s", txt)

            self.page.on("console", _on_console)
            logger.info("[2/6] New page created (JS console → bot.log enabled)")

            logger.info("[3/6] Navigating to meeting URL: %s", self.meet_url)
            try:
                await self._navigate_to_meet()
                logger.info("[3/6] Navigation OK — on Meet page")
            except RuntimeError as exc:
                logger.warning(
                    "[3/6] Auth redirect detected on navigation: %s — "
                    "_join_meeting() Step B will handle auto-login",
                    exc,
                )
                # Do NOT abort — _join_meeting() Step B handles this case

            # ── Join with retry ───────────────────────────────────────────────
            # If the meeting is not active yet (host hasn't joined), the bot waits
            # and retries automatically — no need to restart the bot manually.
            _MAX_JOIN_RETRIES = 20     # 20 × 30 s = 10 minutes maximum wait
            _JOIN_RETRY_SEC   = 30
            _joined = False

            logger.info("[4/6] Attempting to join meeting (up to %d retries, %ds apart) …",
                        _MAX_JOIN_RETRIES, _JOIN_RETRY_SEC)

            for _attempt in range(1, _MAX_JOIN_RETRIES + 1):
                self._join_failure_reason = ""
                if await self._join_meeting():
                    _joined = True
                    break

                _reason = self._join_failure_reason
                # Retryable reasons:
                #   meeting_not_found / meeting_ended — host hasn't joined yet, wait and retry
                #   auth_redirected — Google security re-check handled in Step E; retry immediately
                #   session_expired — meet.google.com COMPASS cookie expired; re-login and retry
                _retryable = _reason in ("meeting_not_found", "meeting_ended", "auth_redirected", "session_expired")

                if _retryable and _attempt < _MAX_JOIN_RETRIES:
                    if _reason == "auth_redirected":
                        # Step E already re-authenticated and navigated back to Meet.
                        # Retry the join immediately — no sleep, no extra navigation.
                        logger.info(
                            "[4/6] Auth redirect handled — retrying join immediately (attempt %d/%d) …",
                            _attempt + 1, _MAX_JOIN_RETRIES,
                        )
                    elif _reason == "session_expired":
                        # The COMPASS cookie or Meet session expired — bot was kicked to
                        # workspace.google.com. Re-authenticate and navigate back to Meet.
                        logger.warning(
                            "[4/6] Session expired (kicked to workspace.google.com) — "
                            "attempting re-login (attempt %d/%d) …",
                            _attempt + 1, _MAX_JOIN_RETRIES,
                        )
                        if await self._auto_login():
                            _post_auth_url = self.page.url
                            if "meet.google.com" not in _post_auth_url:
                                try:
                                    await self._navigate_to_meet()
                                except Exception as _nav_err:
                                    logger.warning("[4/6] Re-navigation after session re-login failed: %s", _nav_err)
                            logger.info("[4/6] Re-login succeeded — retrying join …")
                        else:
                            logger.error("[4/6] Re-login FAILED — cannot rejoin meeting")
                            break
                    else:
                        logger.info(
                            "[4/6] Meeting not active yet — waiting %ds before retry %d/%d …",
                            _JOIN_RETRY_SEC, _attempt + 1, _MAX_JOIN_RETRIES,
                        )
                        await asyncio.sleep(_JOIN_RETRY_SEC)
                        # Re-navigate to the meeting URL before next attempt
                        try:
                            await self._navigate_to_meet()
                        except Exception as _nav_err:
                            logger.warning("[4/6] Re-navigation failed: %s", _nav_err)
                else:
                    break

            if not _joined:
                try:
                    _ss_dir = Path(__file__).parent.parent.parent / "uploads"
                    _ss_dir.mkdir(exist_ok=True)
                    _ss_path = _ss_dir / f"bot_fail_{self.meet_code}.png"
                    await self.page.screenshot(path=str(_ss_path), full_page=False)
                    logger.error(
                        "[4/6] FAILED to join meeting after %d attempts — "
                        "screenshot: %s  URL: %s  reason: %s",
                        _attempt, _ss_path, self.page.url, self._join_failure_reason,
                    )
                except Exception as _sse:
                    logger.error("[4/6] FAILED to join meeting — URL: %s", self.page.url)
                await self._teardown()
                return

            logger.info("[5/6] Creating meeting record …")
            if LOCAL_MODE:
                # Local mode — no backend required; use a synthetic meeting_id
                import random
                self.meeting_id = random.randint(10000, 99999)
                self.meeting_start_time = time.time()
                self._local_transcript_path = (
                    Path(__file__).parent.parent.parent / "uploads"
                    / f"transcript_{self.meet_code}.jsonl"
                )
                self._local_transcript_path.parent.mkdir(exist_ok=True)
                self.client._local_path = self._local_transcript_path
                logger.info(
                    "[5/6] LOCAL MODE — synthetic meeting_id=%d  transcript → %s",
                    self.meeting_id, self._local_transcript_path,
                )
            else:
                try:
                    data = await self.client.start_meeting(
                        meet_code=self.meet_code,
                        title=self._title or f"Meeting {self.meet_code}",
                        organizer_email=self._organizer_email,
                        attendee_emails=self._attendee_emails,
                    )
                    self.meeting_id = data["id"]
                    self.meeting_start_time = time.time()
                    logger.info("[5/6] Backend meeting created — id=%d start_time=%.1f", self.meeting_id, self.meeting_start_time)
                    # If caller didn't supply attendee emails, try fetching from calendar
                    if not self._attendee_emails:
                        try:
                            _cal_attendees = await self._fetch_attendees_from_calendar()
                            if _cal_attendees:
                                self._attendee_emails = _cal_attendees
                                await self.client.update_meeting_emails(
                                    self.meeting_id, attendee_emails=_cal_attendees
                                )
                                logger.info(
                                    "[5/6] Attendee emails fetched from calendar and saved: %s",
                                    _cal_attendees[:80],
                                )
                        except Exception as _ce:
                            logger.debug("Calendar attendee fetch failed (non-fatal): %s", _ce)
                except Exception as exc:
                    logger.error("[5/6] FAILED to create meeting in backend: %s", exc, exc_info=True)
                    logger.warning(
                        "[5/6] Backend unreachable — falling back to local mode for this session. "
                        "Set LOCAL_MODE=true for persistent local behavior."
                    )
                    import random
                    self.meeting_id = random.randint(10000, 99999)
                    self.meeting_start_time = time.time()
                    self._local_transcript_path = (
                        Path(__file__).parent.parent.parent / "uploads"
                        / f"transcript_{self.meet_code}.jsonl"
                    )
                    self._local_transcript_path.parent.mkdir(exist_ok=True)
                    self.client._local_path = self._local_transcript_path
                    self.client._local_mode = True
                    logger.info(
                        "[5/6] FALLBACK LOCAL MODE — synthetic meeting_id=%d  transcript → %s",
                        self.meeting_id, self._local_transcript_path,
                    )

            logger.info("[6/6] Enabling captions …")
            await self._enable_captions()
            await self._set_caption_language_english_india()

            # Re-inject caption observer now that we're inside the meeting.
            # add_init_script already injected it, but re-running ensures it's
            # attached to the current live DOM (Meet may have fully re-rendered).
            try:
                await self.page.evaluate(_caption_observer_js)
                logger.info("[6/6] Caption MutationObserver re-injected into live page")
            except Exception as _oe:
                logger.warning("[6/6] Caption observer re-inject failed: %s", _oe)
            try:
                await self.page.evaluate(_chat_observer_js)
            except Exception as _oe:
                logger.debug("Chat observer re-inject failed: %s", _oe)

            # Open the Chat panel once so its DOM (and the message list the
            # observer targets) gets mounted. Meet shows only one side panel
            # at a time, so this closes People if it happened to be open —
            # but _ensure_people_panel_open() (every poll tick) reopens People
            # on its own next tick, so this costs at most a 1-tick roster gap.
            await self._open_chat_panel_once()

            import shutil as _shutil
            sarvam_key_set = bool(os.getenv("SARVAM_API_KEY", ""))
            ffmpeg_ok = _shutil.which("ffmpeg") is not None
            audio_device = os.getenv("AUDIO_DEVICE_INDEX", "default")
            logger.info("=" * 60)
            logger.info("BOT ACTIVE — meeting_id=%d code=%s", self.meeting_id, self.meet_code)
            logger.info("Sarvam STT: %s  ffmpeg: %s  Audio device: %s  Language: auto-detect",
                        "ENABLED" if sarvam_key_set else "DISABLED (no SARVAM_API_KEY)",
                        "OK" if ffmpeg_ok else "MISSING",
                        audio_device)
            if sarvam_key_set and not ffmpeg_ok:
                logger.error(
                    "STT WILL NOT WORK — ffmpeg is not on PATH. "
                    "Install it: brew install ffmpeg  (macOS)  "
                    "or: sudo apt install ffmpeg  (Ubuntu/Debian)"
                )
            if not sarvam_key_set:
                logger.error(
                    "STT DISABLED — SARVAM_API_KEY is not set. "
                    "Add it to your .env file: SARVAM_API_KEY=<your-key>"
                )
            logger.info("=" * 60)

            # Ensure mic and camera are OFF once inside the meeting.
            # Bots should be silent observers — no audio/video sent.
            # This is a second pass in case lobby muting failed.
            try:
                await self.page.keyboard.press("Control+d")  # mute mic
                await asyncio.sleep(0.3)
                await self.page.keyboard.press("Control+e")  # turn off camera
                logger.info("Bot mic+camera forced off via keyboard shortcuts (Ctrl+D, Ctrl+E)")
            except Exception as _ke:
                logger.debug("Keyboard mic/cam mute after join failed: %s", _ke)

            self._running = True
            try:
                results = await asyncio.gather(
                    self._poll_loop(),
                    self._audio_stt_loop(),
                    self._active_speaker_loop(),
                    self._participant_poller(),
                    return_exceptions=True,
                )
                for r in results:
                    if isinstance(r, Exception):
                        logger.error("Background task failed: %s", r, exc_info=r)
            finally:
                await self._teardown()

    # ── Auto-login ────────────────────────────────────────────────────────────

    async def _auto_login(self) -> bool:
        """
        Handle Google auth redirects:
          - accountchooser  → click on the bot account (already authenticated)
          - signin/identifier → fill email + password (session truly expired)
        Saves a fresh session_state.json on success.
        """
        email = os.getenv("BOT_GOOGLE_EMAIL", "")
        password = os.getenv("BOT_GOOGLE_PASSWORD", "")

        if not email or not password:
            logger.error("Auto-login FAILED: BOT_GOOGLE_EMAIL or BOT_GOOGLE_PASSWORD not set in env.")
            return False

        logger.info("Auto-login: starting for %s — current URL: %s", email, self.page.url)
        try:
            current = self.page.url

            # ── Case 0: Still on Meet but login required ────────────────────────
            # IMPORTANT: check page.url starts with meet.google.com, NOT just "in" —
            # accountchooser URLs can contain "meet.google.com" as a ?continue= param
            # value and would falsely trigger this branch.
            if self.page.url.startswith("https://meet.google.com"):
                logger.info("Auto-login: on Meet page — looking for Sign-in button …")
                for sel in ['button:has-text("Sign in")', 'a:has-text("Sign in")',
                            '[aria-label*="Sign in" i]']:
                    try:
                        el = await self.page.query_selector(sel)
                        if el and await el.is_visible():
                            await el.click()
                            await asyncio.sleep(4)
                            logger.info("Auto-login: clicked 'Sign in' on Meet page → URL: %s", self.page.url)
                            break
                    except Exception:
                        continue

                if self.page.url.startswith("https://meet.google.com"):
                    # Sign-in button opened a popup or Meet still requires auth — go directly
                    # to ServiceLogin with continue=meet_url so the full credential flow
                    # lands back on Meet after success.
                    import urllib.parse as _urlparse
                    _meet_continue = f"https://meet.google.com/{self.meet_code}"
                    _sl_url = (
                        "https://accounts.google.com/ServiceLogin"
                        f"?continue={_urlparse.quote(_meet_continue, safe='')}"
                    )
                    logger.info("Auto-login: still on Meet after Sign-in click — going to ServiceLogin with continue: %s", _sl_url)
                    await self.page.goto(_sl_url, wait_until="domcontentloaded", timeout=15_000)
                    await asyncio.sleep(3)
                    logger.info("Auto-login: reached sign-in page at %s", self.page.url)

                current = self.page.url  # refresh after navigation

            # ── Case 1: account chooser — account already exists, just click it ──
            if "accountchooser" in current or "AccountChooser" in current:
                logger.info("Auto-login: account chooser at %s", current[:120])

                # If the accountchooser URL has no `continue=` param (or continue doesn't
                # point to Meet), clicking the tile lands on myaccount.google.com causing
                # an infinite loop.  Fix: go to ServiceLogin?continue=meet_url — Google
                # will show the same account chooser but with the correct redirect target.
                import urllib.parse as _urlparse
                _current_url = self.page.url
                _has_meet_continue = (
                    "continue=" in _current_url
                    and "meet.google.com" in _current_url
                )
                if not _has_meet_continue:
                    _meet_continue = f"https://meet.google.com/{self.meet_code}"
                    _sl_url = (
                        "https://accounts.google.com/ServiceLogin"
                        f"?continue={_urlparse.quote(_meet_continue, safe='')}"
                    )
                    logger.info(
                        "Auto-login: accountchooser missing meet continue — going to "
                        "ServiceLogin?continue=meet_url: %s", _sl_url,
                    )
                    await self.page.goto(_sl_url, wait_until="domcontentloaded", timeout=15_000)
                    await asyncio.sleep(3)
                    logger.info("Auto-login: after ServiceLogin redirect — URL: %s", self.page.url)
                    current = self.page.url
                else:
                    logger.info("Auto-login: accountchooser already has continue=meet_url — clicking tile")

                # Try clicking the account tile by email text
                clicked = False
                for selector in [
                    f'[data-email="{email}"]',
                    f'div:has-text("{email}")',
                    f'li:has-text("{email}")',
                    f'[aria-label*="{email}"]',
                ]:
                    try:
                        el = await self.page.query_selector(selector)
                        if el and await el.is_visible():
                            await el.click()
                            await asyncio.sleep(4)
                            clicked = True
                            logger.info("Auto-login: clicked account tile '%s'", selector)
                            break
                    except Exception:
                        continue

                if not clicked:
                    # Fallback: click "Use another account" then do full sign-in
                    logger.warning("Auto-login: account tile not found — trying 'Use another account'")
                    for sel in ['button:has-text("Use another account")', 'li:has-text("Use another account")']:
                        try:
                            el = await self.page.query_selector(sel)
                            if el and await el.is_visible():
                                await el.click()
                                await asyncio.sleep(3)
                                break
                        except Exception:
                            continue
                    # Now fall through to email/password flow below
                    current = self.page.url

            # ── Case 2: any remaining Google auth page — smart field detection ──
            if "accounts.google.com" in self.page.url or "ServiceLogin" in self.page.url:
                logger.info("Auto-login: auth page after tile click — URL: %s", self.page.url)

                # Fill email only if the email field is actually present
                # (not present on challenge/pwd page after account tile click)
                # Wait for networkidle first — Google's v3 signin renders inputs via JS
                try:
                    await self.page.wait_for_load_state("networkidle", timeout=8_000)
                except Exception:
                    pass
                _EMAIL_SELECTORS_AUTO = [
                    'input[type="email"]',
                    'input[name="identifier"]',
                    '#identifierId',
                    'input[autocomplete="username"]',
                ]
                try:
                    _email_el = None
                    for _esel in _EMAIL_SELECTORS_AUTO:
                        try:
                            _email_el = await self.page.wait_for_selector(_esel, timeout=5_000)
                            if _email_el:
                                logger.info("Auto-login: found email field via %s — entering email …", _esel)
                                break
                        except Exception:
                            continue
                    if _email_el:
                        await _email_el.fill(email)
                        await self.page.keyboard.press("Enter")
                        await asyncio.sleep(3)
                        logger.info("Auto-login: post-email URL: %s", self.page.url)
                    else:
                        logger.info("Auto-login: no email field — skipping (already selected account)")
                except Exception:
                    logger.info("Auto-login: no email field — skipping (already selected account)")

                # Fill password — handles both identifier flow and challenge/pwd page
                try:
                    pwd_el = await self.page.wait_for_selector(
                        'input[type="password"]:not([aria-hidden="true"])', timeout=8_000
                    )
                    if pwd_el:
                        logger.info("Auto-login: entering password …")
                        await self.page.fill('input[type="password"]', password)
                        await self.page.keyboard.press("Enter")
                        await asyncio.sleep(5)  # extra second for slow challenge pages
                        _post_pwd_url = self.page.url
                        logger.info("Auto-login: post-password URL: %s", _post_pwd_url)
                        await self._take_debug_screenshot("post_password")

                        # ── Detect Google security challenges ─────────────────
                        # Google blocks logins from new IPs (e.g. Azure VM) with a
                        # phone/2FA challenge the bot cannot complete automatically.
                        if "accounts.google.com" in _post_pwd_url:
                            try:
                                _body = await self.page.evaluate(
                                    "() => document.body ? document.body.innerText : ''"
                                )
                                _body_lower = _body.lower()
                                _challenge_phrases = [
                                    "verify it's you", "confirm it's you",
                                    "verify your identity", "unusual activity",
                                    "this sign-in attempt", "suspicious",
                                    "check your phone", "2-step verification",
                                    "verification code", "google prompt",
                                    "enter the code", "recovery phone",
                                ]
                                _challenge_hit = next(
                                    (p for p in _challenge_phrases if p in _body_lower), None
                                )
                                if _challenge_hit:
                                    logger.error(
                                        "Auto-login: Google security challenge detected "
                                        "('%s') — URL: %s  "
                                        "Google is blocking login from this server IP. "
                                        "FIX: open the app UI → click 'Login Bot to Google' "
                                        "to create a session directly on the server, "
                                        "OR disable 2FA for %s in Google Workspace Admin, "
                                        "OR run 'python -m bot.login_once' on the Azure VM.",
                                        _challenge_hit, _post_pwd_url, email,
                                    )
                                    await self._take_debug_screenshot("security_challenge")
                                    return False
                            except Exception as _det_exc:
                                logger.warning("Auto-login: challenge detection failed: %s", _det_exc)

                        # ── Detect "challenge" in URL (2FA / re-auth pages) ───
                        if "accounts.google.com" in _post_pwd_url and any(
                            kw in _post_pwd_url for kw in ("challenge", "rejected", "interstitial")
                        ):
                            logger.error(
                                "Auto-login: Google challenge/rejection page after password — URL: %s  "
                                "This usually means 2FA is ON or Google blocked the IP. "
                                "FIX: use 'Login Bot to Google' in UI or run login_once on the VM.",
                                _post_pwd_url,
                            )
                            await self._take_debug_screenshot("login_challenge_url")
                            return False

                except Exception as exc:
                    # Google may have redirected directly to Meet without asking for password.
                    # Use startswith to avoid false positive when meet.google.com appears
                    # in the ?continue= query param of an accounts.google.com URL.
                    if self.page.url.startswith("https://meet.google.com"):
                        logger.info("Auto-login: Google redirected to Meet without password — success!")
                    else:
                        logger.error("Auto-login: password field not found — %s  URL: %s", exc, self.page.url)
                        await self._take_debug_screenshot("no_password_field")
                        return False

            # ── Detect Google security challenges (phone verify, suspicious login) ──
            _challenge_texts = [
                "Verify it's you", "verify it's you",
                "phone number", "recovery email",
                "Confirm your recovery email",
                "suspicious activity", "unusual activity",
                "This account was recently used", "2-Step Verification",
                "Enter the code", "Get a verification code",
                "Check your phone", "verify your identity",
            ]
            _page_text = ""
            try:
                _page_text = await self.page.evaluate("() => document.body.innerText")
            except Exception:
                pass
            for _challenge in _challenge_texts:
                if _challenge.lower() in _page_text.lower():
                    await self._take_debug_screenshot("google_security_challenge")
                    logger.error(
                        "Auto-login BLOCKED: Google security challenge detected (%r). "
                        "See screenshot: bot_google_security_challenge_%s.png. "
                        "Fix: run bot/login_once.py locally on the DEPLOYMENT SERVER, "
                        "export BOT_SESSION_B64, and restart. "
                        "Or set BOT_GUEST_MODE=true in .env for guest join.",
                        _challenge, self.meet_code,
                    )
                    return False

            # ── Dismiss post-login prompts ──────────────────────────────────────
            # Google shows various account-setup / security prompts after login.
            # Try up to 3 rounds in case prompts chain (e.g. recovery → welcome).
            for _round in range(3):
                _dismissed_any = False
                for selector in [
                    'button:has-text("Not now")',      'button:has-text("Skip")',
                    'button:has-text("Done")',          'button:has-text("Continue")',
                    'button:has-text("I agree")',       'button:has-text("Confirm")',
                    'button:has-text("Got it")',        'button:has-text("OK")',
                    'button:has-text("No thanks")',     'button:has-text("Remind me later")',
                    'button:has-text("Cancel")',        'button:has-text("Dismiss")',
                    '[aria-label="Dismiss"]',           '[aria-label="Close"]',
                ]:
                    try:
                        el = await self.page.query_selector(selector)
                        if el and await el.is_visible():
                            await el.click()
                            await asyncio.sleep(1)
                            _dismissed_any = True
                    except Exception:
                        continue
                if not _dismissed_any:
                    break  # no more prompts

            current = self.page.url
            logger.info("Auto-login: final URL after all steps: %s", current)
            if "accounts.google.com" in current and any(
                kw in current for kw in ("signin", "challenge", "accountchooser", "identifier", "rejected")
            ):
                await self._take_debug_screenshot("autologin_failed")
                logger.error(
                    "Auto-login FAILED: still on Google auth/challenge page — URL: %s  "
                    "Causes: wrong password | 2FA enabled | Google blocked server IP. "
                    "Screenshots saved to /app/uploads/bot_post_password_*.png and "
                    "bot_security_challenge_*.png for diagnosis. "
                    "Fix: open app UI → 'Login Bot to Google' to authenticate from the server.",
                    current,
                )
                return False

            logger.info("Auto-login: SUCCESS — URL: %s", current)

            # Save fresh session
            try:
                new_state = await self.context.storage_state()
                _session_file = Path(__file__).parent.parent.parent / "bot" / "session_state.json"
                _session_file.write_text(json.dumps(new_state))
                logger.info("Auto-login: session_state.json saved (%d cookies)", len(new_state.get("cookies", [])))
            except Exception as exc:
                logger.warning("Auto-login: could not save session: %s", exc)

            return True

        except Exception as exc:
            logger.error("Auto-login UNEXPECTED ERROR: %s", exc, exc_info=True)
            return False

    # ── Navigation ────────────────────────────────────────────────────────────

    async def _navigate_to_meet(self) -> None:
        logger.info("Opening: %s", self.meet_url)
        await self.page.goto(self.meet_url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(3)
        current = self.page.url
        # myaccount.google.com redirect = Google completed a mid-session re-auth
        # with no continue URL. Navigate to Meet again — second attempt always lands.
        # Google sometimes redirects to myaccount or accountchooser after navigating to Meet.
        # myaccount = re-auth with no continue URL; accountchooser = needs account selection.
        # Both are resolved by: (for accountchooser) clicking the account tile, then
        # retrying the Meet navigation once more.
        if "myaccount.google.com" in current or "accountchooser" in current or "AccountChooser" in current:
            logger.warning("_navigate_to_meet: auth redirect (%s) — re-authenticating …", current)
            await self._auto_login()  # handles accountchooser tile click; no-op on myaccount
            await self.page.goto(self.meet_url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(4)
            current = self.page.url
            logger.info("_navigate_to_meet: returned to %s title=%s", current, await self._safe_title())
        if "meet.google.com" not in current:
            logger.error(
                "Redirected away from Meet right after navigation — URL: %s",
                current,
            )
            raise RuntimeError(f"Auth redirect detected: {current}")

    async def _click_join_button(self) -> bool:
        logger.info("Join step E: Clicking Ask to join / Join now button …")
        for _sel, _t in [
            ('button:has-text("Ask to join")',  5000),
            ('button:has-text("Join now")',     5000),
            ('button:has-text("Join")',         3000),
            ('[aria-label="Ask to join"]',      3000),
            ('[aria-label="Join now"]',         3000),
            ('button[jsname="Qx7uuf"]',         3000),
            ('button[jsname="V67aGc"]',         3000),
            ('[jsname="V67aGc"]',               3000),
            ('div[jsname="Qx7uuf"]',            3000),
        ]:
            try:
                el = await self.page.wait_for_selector(_sel, timeout=_t, state="attached")
                if not el:
                    continue
                await el.click(force=True)
                logger.info("Join step E: Clicked '%s'", _sel)
                return True
            except Exception:
                continue

        try:
            clicked = await self.page.evaluate("""
                () => {
                    const btns = Array.from(document.querySelectorAll('button'));
                    const joinBtn = btns.find(b => {
                        const t = (b.innerText || b.textContent || '').trim().toLowerCase();
                        return t === 'ask to join' || t === 'join now' || t === 'join';
                    });
                    if (joinBtn) { joinBtn.click(); return joinBtn.innerText.trim(); }
                    return null;
                }
            """)
            if clicked:
                logger.info("Join step E: Join button clicked via JS fallback (text='%s')", clicked)
                return True
            btns = await self.page.evaluate(
                "() => Array.from(document.querySelectorAll('button'))"
                ".map(b => b.innerText.trim()).filter(t => t.length > 0)"
            )
            logger.warning("Join step E: No join button found. Visible buttons: %s", btns)
            await self._take_debug_screenshot("join_btn_not_found")
            return False
        except Exception as _je:
            logger.debug("JS join fallback failed: %s", _je)
            return False

    # ── Join flow ─────────────────────────────────────────────────────────────

    async def _join_meeting(self) -> bool:
        logger.info("Join flow started — URL: %s", self.page.url)
        try:
            # Step A: Dismiss consent dialogs
            logger.info("Join step A: Checking for consent/cookie dialogs …")
            dismissed = await self._try_click(
                ['button:has-text("Accept all")', 'button:has-text("I agree")',
                 'button:has-text("Accept")', '[aria-label="Accept all"]'],
                timeout=2000,
            )
            if dismissed:
                logger.info("Join step A: Consent dialog dismissed")
            else:
                logger.info("Join step A: No consent dialog found (OK)")
            await asyncio.sleep(1)

            # Step B: Check auth redirect — handle based on which Google page we landed on
            logger.info("Join step B: Checking URL for auth redirect — current: %s", self.page.url)
            if "myaccount.google.com" in self.page.url:
                logger.warning("Join step B: myaccount redirect (logged in, no continue URL) — navigating back to Meet …")
                await self._navigate_to_meet()
            elif any(d in self.page.url for d in ("accounts.google.com", "workspace.google.com")):
                logger.warning("Join step B: Session expired — attempting auto-login …")
                if not await self._auto_login():
                    logger.error("Join step B: Auto-login FAILED — cannot join meeting")
                    return False
                logger.info("Join step B: Auto-login succeeded — navigating back to Meet …")
                await self._navigate_to_meet()

            # B2: Detect what the Meet page is actually showing
            await asyncio.sleep(2)
            page_state = await self._detect_meet_page_state()
            logger.info("Join step B: page_state=%s  URL=%s", page_state, self.page.url)

            if page_state == "page_closed":
                logger.error("Join step B: Browser/page was closed unexpectedly")
                return False
            if page_state == "off_meet":
                logger.error("Join step B: Not on Meet page — URL: %s", self.page.url)
                return False
            if page_state == "login_required":
                logger.warning("Join step B: Meet page shows login required — attempting auto-login …")
                if not await self._auto_login():
                    logger.error("Join step B: Auto-login FAILED")
                    await self._take_debug_screenshot("login_required")
                    return False
                await self._navigate_to_meet()
                await asyncio.sleep(2)
            if page_state == "security_check":
                logger.error(
                    "Join step B: Google security check detected — "
                    "re-run 'python -m bot.login_once' to re-authenticate manually."
                )
                await self._take_debug_screenshot("security_check")
                return False
            if page_state == "in_meeting":
                logger.info("Join step B: already inside meeting — skipping join flow")
                return True

            if page_state in ("meeting_not_found", "meeting_ended"):
                logger.warning(
                    "Join step B: Meeting not active (page_state=%s) — "
                    "host may not have joined yet", page_state,
                )
                await self._take_debug_screenshot(page_state)
                self._join_failure_reason = page_state
                return False

            logger.info("Join step B: URL OK — on Meet page")

            # Take a debug screenshot of the lobby so we can see what the page looks like
            await self._take_debug_screenshot("lobby_state")

            # Step B3: Fill display name (required for guest lobbies; authenticated
            # lobbies may skip this step entirely — don't waste time waiting).
            # Use 1.5 s per selector (7.5 s max) instead of 5 s (25 s max).
            _bot_name = os.getenv("BOT_DISPLAY_NAME", "OneClarity AI")
            _name_filled = False
            _name_selectors = [
                'input[jsname="YPqjbf"]',          # Meet's primary name input
                'input[aria-label="Your name"]',
                'input[aria-label*="name" i]',
                'input[placeholder="Your name"]',
                'input[placeholder*="name" i]',
                'input[type="text"]:visible',
            ]
            for _name_sel in _name_selectors:
                try:
                    _name_el = await self.page.wait_for_selector(_name_sel, timeout=1500)
                    if not _name_el:
                        continue
                    # Use force=True because Meet sometimes marks the input as
                    # not-visible (CSS transition) even though it's in the DOM.
                    await _name_el.fill(_bot_name, force=True)
                    logger.info("Join step B3: Filled display name '%s' via '%s'", _bot_name, _name_sel)
                    await asyncio.sleep(0.8)  # let Join button activate
                    _name_filled = True
                    break
                except Exception:
                    continue

            if not _name_filled:
                # Dump all visible inputs for debugging
                try:
                    _inputs = await self.page.evaluate(
                        "() => Array.from(document.querySelectorAll('input')).map(i => "
                        "({type:i.type, name:i.name, placeholder:i.placeholder, "
                        "ariaLabel:i.getAttribute('aria-label'), jsname:i.getAttribute('jsname'), "
                        "visible:i.offsetParent!==null}))"
                    )
                    logger.warning("Join step B3: Name input NOT found. Visible inputs: %s", _inputs)
                except Exception:
                    pass
                await self._take_debug_screenshot("name_field_missing")
                # Do NOT abort — maybe it's a signed-in lobby that doesn't need a name

            # Step C: Turn off microphone in lobby
            # Industry standard: bots join muted — no audio sent to the meeting
            logger.info("Join step C: Muting microphone …")
            mic_off = await self._try_click([
                'button[aria-label*="microphone" i][aria-pressed="true"]',
                '[jsname="BOHaEe"]',
                'button[aria-label="Turn off microphone (ctrl+d)"]',
                'button[data-is-muted="false"][aria-label*="mic" i]',
            ], timeout=1500)

            if not mic_off:
                # JS fallback — click any visible mic toggle button
                try:
                    mic_off = await self.page.evaluate("""
                        () => {
                            const btns = Array.from(document.querySelectorAll('button'));
                            const micBtn = btns.find(b => {
                                const label = (b.getAttribute('aria-label') || b.innerText || '').toLowerCase();
                                const pressed = b.getAttribute('aria-pressed');
                                return (label.includes('mic') || label.includes('mute')) &&
                                       (pressed === 'true' || pressed === null);
                            });
                            if (micBtn) { micBtn.click(); return true; }
                            return false;
                        }
                    """)
                except Exception:
                    pass

            # Keyboard shortcut fallback (Ctrl+D = toggle mic in Meet)
            if not mic_off:
                try:
                    await self.page.keyboard.press("Control+d")
                    mic_off = True
                except Exception:
                    pass

            logger.info("Join step C: Mic muted=%s", mic_off)

            # Step D: Turn off camera in lobby
            # Industry standard: bots join with NO video — they appear as a clean avatar
            logger.info("Join step D: Turning off camera …")
            cam_off = await self._try_click([
                'button[aria-label*="camera" i][aria-pressed="true"]',
                '[jsname="R3RNx"]',
                'button[aria-label="Turn off camera (ctrl+e)"]',
                'button[data-is-muted="false"][aria-label*="camera" i]',
            ], timeout=1500)

            if not cam_off:
                # JS fallback — click any visible camera toggle button
                try:
                    cam_off = await self.page.evaluate("""
                        () => {
                            const btns = Array.from(document.querySelectorAll('button'));
                            const camBtn = btns.find(b => {
                                const label = (b.getAttribute('aria-label') || b.innerText || '').toLowerCase();
                                const pressed = b.getAttribute('aria-pressed');
                                return (label.includes('camera') || label.includes('video') ||
                                        label.includes('videocam')) &&
                                       (pressed === 'true' || pressed === null);
                            });
                            if (camBtn) { camBtn.click(); return true; }
                            return false;
                        }
                    """)
                except Exception:
                    pass

            # Keyboard shortcut fallback (Ctrl+E = toggle camera in Meet)
            if not cam_off:
                try:
                    await self.page.keyboard.press("Control+e")
                    cam_off = True
                except Exception:
                    pass

            logger.info("Join step D: Camera off=%s", cam_off)

            await asyncio.sleep(1)

            if not await self._is_page_alive():
                logger.error("Join step D→E: Page closed before join click")
                return False

            joined = await self._click_join_button()
            if joined:
                logger.info("Join step E: Join button clicked — waiting 12s for meeting to load …")
                await asyncio.sleep(12)
                post_url = self.page.url
                logger.info("Join step E: Post-join URL: %s", post_url)
                if "meet.google.com" not in post_url:
                    await self._take_debug_screenshot("post_join_redirect")
                    if any(d in post_url for d in (
                        "accountchooser", "AccountChooser",
                        "accounts.google.com", "workspace.google.com",
                    )):
                        logger.warning(
                            "Join step E: auth redirect after 'Join now' — re-authenticating and retrying …"
                        )
                        if await self._auto_login():
                            # With the continue= fix, auto_login may have landed us directly
                            # on Meet.  Only re-navigate if we're NOT already there — calling
                            # _navigate_to_meet() when already on Meet reloads the page and
                            # triggers the auth redirect loop again.
                            _post_auth_url = self.page.url
                            logger.info("Join step E: post-auth URL: %s", _post_auth_url)
                            if "meet.google.com" not in _post_auth_url:
                                try:
                                    await self._navigate_to_meet()
                                except Exception as _nav_err:
                                    logger.warning("Join step E: re-navigation after auth failed: %s", _nav_err)
                            else:
                                logger.info(
                                    "Join step E: already on Meet after auth (continue= worked) "
                                    "— skipping re-navigation"
                                )

                            if await self._verify_in_meeting():
                                logger.info("Join step E: Auth redirect handled and bot is already inside the meeting")
                                return True

                            logger.info(
                                "Join step E: Auth redirect handled, still on Meet page — attempting second join click …"
                            )
                            await asyncio.sleep(3)
                            if await self._click_join_button():
                                await asyncio.sleep(12)
                                if await self._verify_in_meeting():
                                    logger.info(
                                        "Join step E: Auth redirect handled and bot is inside meeting after second click"
                                    )
                                    return True
                                logger.info(
                                    "Join step E: Second join click did not enter meeting. URL=%s title=%s",
                                    self.page.url,
                                    await self._safe_title(),
                                )

                            await self._take_debug_screenshot("auth_redirect_after_join")
                            self._join_failure_reason = "auth_redirected"
                        else:
                            logger.error("Join step E: Unexpected redirect to %s after join click", post_url)
                        return False
                    else:
                        logger.error("Join step E: Unexpected redirect to %s after join click", post_url)
                        return False
            else:
                logger.warning("Join step E: Could not click any join button — checking if already inside")

            # Step F: Verify inside meeting
            logger.info("Join step F: Verifying we are inside the meeting …")
            result = await self._verify_in_meeting()
            if result:
                logger.info("Join step F: SUCCESS — bot is inside the meeting")
            else:
                logger.error("Join step F: FAILED — could not confirm entry into meeting. Title: %s  URL: %s",
                             await self._safe_title(), self.page.url)
            return result

        except RuntimeError:
            raise
        except Exception as exc:
            logger.error("Join flow UNEXPECTED ERROR: %s", exc, exc_info=True)
            return False

    async def _verify_in_meeting(self) -> bool:
        """Return True only when in-meeting controls are actually visible."""
        if not await self._is_page_alive():
            logger.error("_verify_in_meeting: page is closed — cannot verify")
            return False

        # Bail immediately if Google has redirected away from Meet
        if "meet.google.com" not in self.page.url:
            _redir_url = self.page.url
            if "workspace.google.com" in _redir_url or "accounts.google.com" in _redir_url:
                logger.error(
                    "Session expired — redirected to %s (COMPASS/Meet cookie expired). "
                    "Will attempt re-login and retry join.",
                    _redir_url,
                )
                self._join_failure_reason = "session_expired"
            else:
                logger.error(
                    "Not on Meet page during verify — redirected to %s. "
                    "Google session likely expired; re-authenticate via the UI.",
                    _redir_url,
                )
            return False

        # These elements only exist once you're inside an active call
        in_meeting_selectors = [
            'button[aria-label*="Leave call" i]',
            'button[aria-label*="leave call" i]',
            '[jsname="CQylAd"]',
            'button[aria-label*="Turn off microphone" i]',
            'button[aria-label*="Turn on microphone" i]',
            'button[aria-label*="microphone" i][aria-pressed]',
            '[data-allocation-index]',
            '[jsname="EzdCnc"]',
            'div[jscontroller="kAPMuc"]',
            '[data-self-name]',
        ]
        for selector in in_meeting_selectors:
            if not await self._is_page_alive():
                logger.error("Page closed during verify — was checking selector: %s", selector)
                return False
            # Stop early if page navigated away while we were checking
            if "meet.google.com" not in self.page.url:
                _mid_url = self.page.url
                if "workspace.google.com" in _mid_url or "accounts.google.com" in _mid_url:
                    logger.error(
                        "Page navigated away during verify — session expired, now at: %s. "
                        "Will attempt re-login and retry join.",
                        _mid_url,
                    )
                    self._join_failure_reason = "session_expired"
                else:
                    logger.error("Page navigated away during verify — now at: %s", _mid_url)
                return False
            try:
                el = await self.page.wait_for_selector(selector, timeout=8000)
                if el and await el.is_visible():
                    logger.info("In-meeting confirmed (selector: %s)", selector)
                    return True
            except PWTimeout:
                continue
            except Exception:
                continue

        # JS fallback: check for the Material Icons "call_end" button text —
        # Meet renders the leave-call button as an icon with no aria-label on some
        # browser/OS combinations (macOS Playwright Chromium).
        try:
            found = await self.page.evaluate("""
                () => {
                    const btns = Array.from(document.querySelectorAll('button'));
                    return btns.some(b => {
                        const t = (b.innerText || b.textContent || '').trim().toLowerCase();
                        return t === 'call_end' || t === 'hang up' || t === 'leave';
                    });
                }
            """)
            if found:
                logger.info("In-meeting confirmed via JS fallback (call_end icon button)")
                return True
        except Exception as _je:
            logger.debug("JS verify fallback failed: %s", _je)

        logger.error(
            "Could NOT confirm entry into meeting. Final URL: %s  Title: %s",
            self.page.url,
            await self._safe_title(),
        )
        await self._take_debug_screenshot("verify_failed")
        return False

    async def _detect_captions_enabled(self) -> bool:
        """Return True when the Meet UI appears to have captions enabled."""
        # Fast path: if the caption timeline has entries, captions are definitely
        # working — skip the JS check entirely and don't risk a toggle via keyboard 'c'.
        if self._caption_timeline or self._active_captions:
            return True
        try:
            return await self.page.evaluate("""
                () => {
                    const normalize = text => (text || '').trim().toLowerCase();

                    const captionOnPatterns = [
                        'turn off captions',
                        'captions on',
                        'cc on',
                        'turn off cc',
                        'captions are on',
                    ];

                    const buttonCandidates = Array.from(
                        document.querySelectorAll('button,div[role="button"],span[role="button"]')
                    );
                    for (const el of buttonCandidates) {
                        const label = [
                            el.getAttribute('aria-label'),
                            el.getAttribute('data-tooltip'),
                            el.innerText,
                        ].filter(Boolean).join(' ').toLowerCase();
                        if (!label.includes('captions') && !label.includes('cc')) continue;
                        if (captionOnPatterns.some(pattern => label.includes(pattern))) {
                            return true;
                        }
                        if (el.getAttribute('aria-pressed') === 'true') {
                            return true;
                        }
                    }

                    // Only check CSS-class selectors that are specific to caption panels.
                    // Do NOT check [aria-live] here — those elements exist in Google Meet
                    // regardless of caption state (notification area for join/leave events)
                    // and cause false positives that prevent the bot from re-trying enable.
                    const captionSelectors = [
                        '.a4cQT',
                        '.iTTPOb',
                        '[jsname="YSxPC"]',
                        '.TBMuR',
                        '.iOzk7',
                        '.CNusmb',
                        '[jsname="tgaKEf"]',
                    ];
                    for (const sel of captionSelectors) {
                        const el = document.querySelector(sel);
                        if (el) {
                            return true;
                        }
                    }

                    return false;
                }
            """)
        except Exception:
            return False

    # ── Caption diagnostic dump ───────────────────────────────────────────────

    async def _dump_caption_area_html(self) -> None:
        """Log raw HTML of candidate caption containers to diagnose stale selectors.
        Call when caption_timeline==0 for too long in an active meeting."""
        try:
            dump = await self.page.evaluate("""
                () => {
                    const info = {};
                    const candidates = {
                        'aria-live-polite':    '[aria-live="polite"]',
                        'aria-live-assertive': '[aria-live="assertive"]',
                        'role-log':            '[role="log"]',
                        'jsname-eVSMFe':       '[jsname="eVSMFe"]',
                        'jsctrl-RVTGF':        '[jscontroller="RVTGF"]',
                        'a4cQT':               '.a4cQT',
                        'iTTPOb':              '.iTTPOb',
                        'nMcdL':               '.nMcdL',
                    };
                    for (const [key, sel] of Object.entries(candidates)) {
                        const els = document.querySelectorAll(sel);
                        if (els.length) {
                            info[key] = Array.from(els).slice(0, 2).map(e => ({
                                tag: e.tagName,
                                cls: e.className.slice(0, 80),
                                text: (e.innerText || '').trim().slice(0, 120),
                                html: e.outerHTML.slice(0, 300),
                            }));
                        }
                    }
                    return info;
                }
            """)
            logger.warning(
                "Caption diagnostic dump (caption_timeline=0, need to update selectors): %s",
                dump,
            )
        except Exception as exc:
            logger.debug("Caption diagnostic dump failed: %s", exc)

    # ── Enable captions ───────────────────────────────────────────────────────

    async def _set_caption_language_english_india(self) -> bool:
        """
        After captions are on, switch caption language to English (India).

        Previous implementation failed because:
        - CSS selectors like aria-label*="caption language" don't exist in Google Meet's DOM
        - No screenshots were taken so failures were undiagnosable
        - More Options DID open but the menu item selectors were all wrong

        This version uses JavaScript text-matching (not CSS guessing) and takes
        diagnostic screenshots at every step so failures can be debugged.
        """
        logger.info("Caption language: attempting to set English (India) …")
        await asyncio.sleep(2)

        # ── Diagnostic: dump all caption-related buttons visible on page ──────
        try:
            caption_buttons = await self.page.evaluate("""
                () => Array.from(document.querySelectorAll('button, [role="button"]'))
                    .map(b => ({
                        label:   b.getAttribute('aria-label'),
                        jsname:  b.getAttribute('jsname'),
                        pressed: b.getAttribute('aria-pressed'),
                        tooltip: b.getAttribute('data-tooltip'),
                    }))
                    .filter(b => (b.label || b.tooltip || '').toLowerCase().includes('caption'))
            """)
            logger.info("Caption language: caption-related buttons on page: %s", caption_buttons)
        except Exception as _de:
            logger.debug("Caption language: button dump failed: %s", _de)

        await self._take_debug_screenshot("caption_lang_1_before")

        # ── Step 1: open More Options menu ────────────────────────────────────
        mo_clicked = await self._try_click([
            'button[jsname="NakZHc"]',
            'button[aria-label*="More options" i]',
            'button[data-tooltip*="More options" i]',
        ], timeout=4000)

        if not mo_clicked:
            logger.warning("Caption language: More Options button not found — cannot set language")
            return False

        await asyncio.sleep(1.5)
        await self._take_debug_screenshot("caption_lang_2_more_options_open")

        # Dump all visible menu items so we know what Google Meet actually shows
        try:
            menu_items = await self.page.evaluate("""
                () => Array.from(document.querySelectorAll(
                    '[role="menuitem"], [role="option"], li[jsname], li[data-index]'
                ))
                .map(el => ({
                    label:  el.getAttribute('aria-label'),
                    jsname: el.getAttribute('jsname'),
                    text:   el.textContent?.trim()?.substring(0, 80),
                }))
                .filter(el => el.label || el.text)
            """)
            logger.info("Caption language: More Options menu items → %s", menu_items)
        except Exception as _me:
            logger.warning("Caption language: menu item dump failed: %s", _me)

        # ── Step 2: click caption-related item using JS text matching ─────────
        # CSS selectors failed before because Google Meet uses internal text that
        # varies. JS text matching is locale-independent and version-resilient.
        caption_item_clicked = await self.page.evaluate("""
            () => {
                const KEYWORDS = ['caption', 'subtitle', 'cc', 'live caption'];
                const items = Array.from(document.querySelectorAll(
                    '[role="menuitem"], [role="option"], li[jsname], li[data-index]'
                ));
                const match = items.find(el => {
                    const label = (el.getAttribute('aria-label') || '').toLowerCase();
                    const text  = (el.textContent || '').toLowerCase().trim();
                    return KEYWORDS.some(k => label.includes(k) || text.includes(k));
                });
                if (match) { match.click(); return match.textContent?.trim() || 'clicked'; }
                return null;
            }
        """)

        if not caption_item_clicked:
            logger.warning(
                "Caption language: no caption item found in More Options menu — "
                "check screenshot caption_lang_2_more_options_open for what is available"
            )
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

        logger.info("Caption language: clicked menu item → %r", caption_item_clicked)
        await asyncio.sleep(1.5)
        await self._take_debug_screenshot("caption_lang_3_settings_panel")

        # ── Step 3: dump available language options (for diagnosis) ───────────
        try:
            lang_options = await self.page.evaluate("""
                () => Array.from(document.querySelectorAll(
                    'li, [role="option"], [role="menuitem"], option'
                ))
                .map(el => ({
                    label: el.getAttribute('aria-label'),
                    value: el.getAttribute('data-value') || el.getAttribute('value'),
                    text:  el.textContent?.trim()?.substring(0, 60),
                }))
                .filter(el => (el.text || el.label || '').toLowerCase().includes('english'))
            """)
            logger.info("Caption language: available English options → %s", lang_options)
        except Exception:
            pass

        # ── Step 4: select English (India) via JS text matching ───────────────
        selected = await self.page.evaluate("""
            () => {
                const candidates = Array.from(document.querySelectorAll(
                    'li, [role="option"], [role="menuitem"], option'
                ));
                const match = candidates.find(el => {
                    const label = (el.getAttribute('aria-label') || '').toLowerCase();
                    const text  = (el.textContent || '').toLowerCase();
                    const val   = (
                        el.getAttribute('data-value') || el.getAttribute('value') || ''
                    ).toLowerCase();
                    return label.includes('india') || text.includes('india') ||
                           val === 'en-in' || val.includes('en-in');
                });
                if (match) { match.click(); return match.textContent?.trim() || 'clicked'; }
                return null;
            }
        """)

        if selected:
            logger.info("Caption language: ✅ set to English (India) — element: %r", selected)
            await asyncio.sleep(1)
            return True

        # ── Step 5: CSS fallback (in case JS matching missed it) ──────────────
        css_selected = await self._try_click([
            'li[aria-label*="English (India)" i]',
            'li[aria-label*="India" i]',
            '[data-value="en-IN"]',
            '[data-value="en-in"]',
            'option[value="en-IN"]',
        ], timeout=2000)

        if css_selected:
            logger.info("Caption language: ✅ set to English (India) via CSS fallback")
            return True

        await self._take_debug_screenshot("caption_lang_4_failed")
        logger.warning(
            "Caption language: ❌ English (India) option not found — "
            "check screenshots caption_lang_3_settings_panel and caption_lang_4_failed "
            "to see what language options Google Meet is showing"
        )
        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass
        return False

    async def _enable_captions(self) -> bool:
        logger.info("Enabling captions …")
        await asyncio.sleep(3)

        if await self._detect_captions_enabled():
            logger.info("Captions are already enabled; skipping enable flow")
            return True

        # Try button click first
        enabled = await self._try_click([
            'button[aria-label*="captions" i][aria-pressed="false"]',
            'button[aria-label="Turn on captions (c)"]',
            'button[aria-label*="Turn on captions" i]',
            '[jsname="r8qRAd"]',
        ], timeout=4000)

        if enabled:
            await asyncio.sleep(1.5)
            if await self._detect_captions_enabled():
                logger.info("Captions enabled via button click")
                return True
            logger.warning("Button click appeared to fire but captions still not detected")

        # Try More Options menu
        opened = await self._try_click([
            'button[aria-label*="More options" i]',
            '[jsname="NakZHc"]',
        ], timeout=3000)
        if opened:
            await asyncio.sleep(1)
            await self._try_click([
                'li[aria-label*="captions" i]',
                'span:has-text("Turn on captions")',
            ], timeout=2000)
            await asyncio.sleep(1.5)
            if await self._detect_captions_enabled():
                logger.info("Captions enabled via More Options")
                return True
            logger.warning("More Options path fired but captions still not detected")

        # Keyboard shortcut 'c' — only when captions are confirmed OFF.
        # 'c' is a toggle: pressing it when captions are already ON turns them OFF.
        try:
            if await self._detect_captions_enabled():
                logger.info("Captions are already enabled before keyboard toggle; skipping")
                return True
            await self.page.keyboard.press("c")
            await asyncio.sleep(1.5)
            if await self._detect_captions_enabled():
                logger.info("Captions toggled ON via keyboard shortcut 'c'")
                return True
            logger.warning("Keyboard shortcut 'c' fired but captions still not detected")
        except Exception as exc:
            logger.warning("Keyboard shortcut 'c' failed: %s", exc)

        logger.warning("Caption enable attempt completed but captions are not confirmed enabled")
        return False

    # ── Poll loop ─────────────────────────────────────────────────────────────

    # When the meeting was already active and everyone LEFT → leave after 30 s.
    # When the bot joined early (before anyone arrived) → wait 10 min for people to join.
    # The right threshold is chosen at runtime based on _has_seen_participants.
    _ALONE_LEAVE_TICKS_AFTER_MEETING  = 30    # seconds — meeting ended, everyone gone
    _ALONE_LEAVE_TICKS_BEFORE_MEETING = 600   # seconds — bot is early, waiting for attendees

    async def _poll_loop(self) -> None:
        logger.info("Poll loop started (interval=%.1fs)", POLL_INTERVAL)
        consecutive_errors = 0
        tick = 0
        _no_caption_ticks = 0

        while self._running:
            tick += 1
            try:
                had_captions = await self._scrape_captions()
                await self._scrape_participants()
                await self._scrape_chat_messages()
                consecutive_errors = 0

                # Dump caption area HTML once at 90s if timeline is still empty
                # in an active meeting — this tells us what CSS classes Google Meet
                # is using NOW so we can update stale selectors.
                if (
                    tick == 90
                    and not self._caption_timeline
                    and self._has_seen_participants
                ):
                    await self._dump_caption_area_html()

                if had_captions:
                    _no_caption_ticks = 0
                else:
                    _no_caption_ticks += 1
                    if _no_caption_ticks % 30 == 0:
                        if await self._detect_captions_enabled():
                            logger.info(
                                "Poll tick=%d — captions appear enabled but no text yet; waiting",
                                tick,
                            )
                            _no_caption_ticks = 0
                        else:
                            logger.warning(
                                "Poll tick=%d — no captions for %d ticks, retrying enable …",
                                tick, _no_caption_ticks,
                            )
                            enabled = await self._enable_captions()
                            if enabled:
                                _no_caption_ticks = 0
                    elif _no_caption_ticks % 15 == 0:
                        logger.warning(
                            "Poll tick=%d — no captions for %d consecutive ticks  URL=%s",
                            tick, _no_caption_ticks, self.page.url,
                        )

                # ── Alone detection: leave when everyone else has gone ────────
                # Only start checking after tick 10 (let DOM stabilise after join)
                if tick > 10:
                    live = await self._count_live_participants()
                    if live == 0:
                        self._alone_ticks += 1

                        # ── False-start detection ─────────────────────────────
                        # If participants appeared for less than _MIN_MEETING_DURATION_SEC
                        # and then all left, treat it as a false start (early joiner who
                        # left before the real meeting began).  Reset to the long pre-meeting
                        # timeout so the bot keeps waiting for the rest of the team.
                        # Example: one person joins at T+10s to test the link, then leaves.
                        # Without this, the bot exits 30s later before everyone else arrives.
                        if (
                            self._has_seen_participants
                            and self._participants_first_seen_at is not None
                        ):
                            time_with_participants = time.time() - self._participants_first_seen_at
                            if time_with_participants < self._MIN_MEETING_DURATION_SEC:
                                logger.info(
                                    "Poll tick=%d — all left after only %.0fs (< %.0fs min) — "
                                    "treating as false start, resetting to pre-meeting timeout",
                                    tick, time_with_participants, self._MIN_MEETING_DURATION_SEC,
                                )
                                self._has_seen_participants = False
                                self._participants_first_seen_at = None

                        _leave_after = (
                            self._ALONE_LEAVE_TICKS_AFTER_MEETING
                            if self._has_seen_participants
                            else self._ALONE_LEAVE_TICKS_BEFORE_MEETING
                        )
                        if self._alone_ticks == 1:
                            _phase = "after meeting" if self._has_seen_participants else "waiting for attendees"
                            logger.info(
                                "Poll tick=%d — no other participants visible (%s). "
                                "Will leave after %d ticks if nobody joins.",
                                tick, _phase, _leave_after,
                            )
                        elif self._alone_ticks % 30 == 0:
                            logger.warning(
                                "Poll tick=%d — still alone (%d/%d ticks, seen_participants=%s)",
                                tick, self._alone_ticks, _leave_after,
                                self._has_seen_participants,
                            )
                        if self._alone_ticks >= _leave_after:
                            _reason = (
                                "All participants left — meeting ended"
                                if self._has_seen_participants
                                else "Nobody joined within timeout — bot leaving early-join"
                            )
                            logger.info(
                                "%s (alone for %d ticks) — bot leaving",
                                _reason, self._alone_ticks,
                            )
                            self._running = False
                            break
                    elif live > 0:
                        if not self._has_seen_participants:
                            self._participants_first_seen_at = time.time()
                            logger.info(
                                "Poll tick=%d — first participant(s) detected (%d). "
                                "Switching to post-meeting timeout (%ds). "
                                "Will reset to pre-meeting if they leave within %.0fs.",
                                tick, live, self._ALONE_LEAVE_TICKS_AFTER_MEETING,
                                self._MIN_MEETING_DURATION_SEC,
                            )
                        self._has_seen_participants = True
                        if self._alone_ticks > 0:
                            logger.info(
                                "Poll tick=%d — %d participant(s) joined/rejoined "
                                "(was alone for %d ticks)",
                                tick, live, self._alone_ticks,
                            )
                        self._alone_ticks = 0

                # ── Silence notification ──────────────────────────────────────
                # If the meeting has started and there has been no speech (no
                # finalized captions) for ≥10 minutes, log a warning every 2 min.
                if self._has_seen_participants and self.meeting_start_time:
                    _elapsed_now = time.time() - self.meeting_start_time
                    _last_speech = (
                        self._caption_timeline[-1]["end_time"]
                        if self._caption_timeline else 0.0
                    )
                    _silence_sec = _elapsed_now - _last_speech
                    _SILENCE_WARN_SEC = 600.0   # 10 minutes
                    _SILENCE_REPEAT_SEC = 120.0  # re-log every 2 min
                    if _silence_sec >= _SILENCE_WARN_SEC and tick % int(_SILENCE_REPEAT_SEC) == 0:
                        logger.warning(
                            "SILENCE ALERT — meeting %d: NO speech detected for %.0f min "
                            "(last caption at t=%.0fs). Participants are muted / reading. "
                            "Bot will stay in the call (10-min grace window active).",
                            self.meeting_id,
                            _silence_sec / 60,
                            _last_speech,
                        )

            except Exception as exc:
                consecutive_errors += 1
                exc_type = type(exc).__name__
                logger.error("Poll error #%d (tick=%d) [%s]: %s", consecutive_errors, tick, exc_type, exc)
                if consecutive_errors == 1 or consecutive_errors % 5 == 0:
                    logger.debug("Poll error stack trace:", exc_info=True)
                if consecutive_errors >= 20:
                    logger.error("Too many consecutive poll errors (%d) — stopping bot", consecutive_errors)
                    break

            if tick % 30 == 0:
                live_now = await self._count_live_participants()
                _timeout_now = (
                    self._ALONE_LEAVE_TICKS_AFTER_MEETING
                    if self._has_seen_participants
                    else self._ALONE_LEAVE_TICKS_BEFORE_MEETING
                )
                logger.info(
                    "Poll heartbeat tick=%d — meeting_id=%s live_participants=%d "
                    "has_seen_participants=%s alone_ticks=%d/%d caption_timeline=%d",
                    tick, self.meeting_id, max(live_now, 0),
                    self._has_seen_participants, self._alone_ticks, _timeout_now,
                    len(self._caption_timeline),
                )
                # Measured-speaker (SSRC) health — lets you SEE if true-source
                # attribution is working or silently inert (audioLevel not exposed).
                logger.info(
                    "Measured-speaker status — signal=%s p0_hits=%d p0_misses=%d "
                    "sources_mapped=%s timeline=%d",
                    "LIVE" if self._measured_signal_logged else "none-yet",
                    self._p0_hits, self._p0_misses,
                    {f"{k[0]}:{k[1]}": v for k, v in list(self._source_speaker_map.items())[:8]},
                    len(self._measured_speaker_timeline),
                )

            if await self._is_meeting_ended():
                logger.info("Meeting end detected at tick=%d — shutting down", tick)
                self._running = False
                break

            await asyncio.sleep(POLL_INTERVAL)

    # ── Meeting-end detection ─────────────────────────────────────────────────

    # Number of consecutive ticks an end condition must be seen before we shut down.
    # This prevents a brief network blip (which shows "Rejoin") from killing the bot.
    _MEETING_END_STREAK_REQUIRED = 3

    # Text patterns that look like meeting-end but are actually transient network issues.
    _TRANSIENT_TEXTS = {
        "You lost your network connection",
        "Trying to reconnect",
        "Reconnecting",
        "Poor connection",
    }

    async def _is_meeting_ended(self) -> bool:
        try:
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=2000)
            except Exception:
                pass

            page_url = self.page.url

            # URL drifted away from Meet entirely
            if self.meet_code not in page_url and "meet.google.com" not in page_url:
                logger.warning("URL changed away from Meet: %s", page_url)
                self._meeting_end_streak += 1
                if self._meeting_end_streak >= self._MEETING_END_STREAK_REQUIRED:
                    logger.info("Confirmed off Meet page for %d ticks — ended", self._meeting_end_streak)
                    return True
                logger.info("Off-Meet URL tick %d/%d", self._meeting_end_streak, self._MEETING_END_STREAK_REQUIRED)
                return False

            # Use JS body-text scan — far more reliable than individual Playwright
            # text= selectors which require exact matches on specific DOM elements.
            try:
                body_text, is_transient, is_ended, matched = await self.page.evaluate("""
                    () => {
                        const body = (document.body ? document.body.innerText : '').toLowerCase();

                        const transient = [
                            'you lost your network connection',
                            'trying to reconnect',
                            'reconnecting',
                            'poor connection',
                        ];
                        const ended = [
                            "you've left the meeting",
                            "the meeting has ended",
                            "your meeting is over",
                            "return to home screen",
                            "this meeting has been ended by the host",
                            "the call has ended",
                            "meeting has ended",
                            "you can't join this video call",
                            "can't join this video call",
                            "rejoin",
                        ];

                        const isTransient = transient.some(p => body.includes(p));
                        let matchedPhrase = null;
                        const isEnded = ended.some(p => {
                            if (body.includes(p)) { matchedPhrase = p; return true; }
                            return false;
                        });

                        return [body.slice(0, 200), isTransient, isEnded, matchedPhrase];
                    }
                """)
            except Exception as _je:
                logger.debug("End-detection JS failed: %s", _je)
                self._meeting_end_streak = 0
                return False

            if is_transient:
                logger.warning("Transient network issue on page — not ending meeting")
                self._meeting_end_streak = 0
                return False

            if is_ended:
                self._meeting_end_streak += 1
                if self._meeting_end_streak >= self._MEETING_END_STREAK_REQUIRED:
                    logger.info("Meeting end confirmed (%r) after %d ticks", matched, self._meeting_end_streak)
                    return True
                logger.info("Meeting end indicator %r (tick %d/%d)", matched, self._meeting_end_streak, self._MEETING_END_STREAK_REQUIRED)
                return False

            self._meeting_end_streak = 0
            return False
        except Exception:
            return False

    async def _ensure_people_panel_open(self) -> None:
        """Open Google Meet's people/participants panel if it isn't already open.

        In full-screen / 1:1 layouts Meet renders NO video tiles, so tile-based
        counting always sees 0. The people panel, by contrast, lists everyone in
        the call (including muted people who never turn on video) as
        [role="listitem"] rows. Keeping it open makes the existing DOM readers
        (Layer 1, _scrape_participants) authoritative. Best-effort + idempotent."""
        try:
            # Single in-page JS call: returns "open" if the panel already shows
            # participant rows, "clicked" if we just clicked the People button, or
            # "none" if no button was found. Using evaluate (not wait_for_selector)
            # keeps this INSTANT — it must never block the 1s poll loop.
            _throttle_ok = (time.time() - self._roster_open_attempt_at) >= 15.0
            res = await self.page.evaluate(
                """(throttleOk) => {
                    const chipSel = '[data-self-name],[jsname="EzdCnc"],[jsname="tgaKEf"],' +
                                    '.zWfAFd,.KV1GEc,.dwSJse,.XWGOtd';
                    for (const it of document.querySelectorAll('[role="listitem"]')) {
                        if (it.querySelector(chipSel)) return {state: 'open'};
                    }
                    if (!throttleOk) return {state: 'wait'};
                    // Find the People / Show-everyone toolbar button and click it.
                    // Match icon buttons too: Meet often renders the people icon as
                    // an <i> with text "people" inside a button with no clear label.
                    const cands = Array.from(document.querySelectorAll(
                        'button,[role="button"]'
                    ));
                    const labels = [];
                    for (const b of cands) {
                        const l = (b.getAttribute('aria-label') || '').toLowerCase();
                        const icon = (b.querySelector('i') ? b.querySelector('i').textContent : '') || '';
                        const ic = icon.trim().toLowerCase();
                        if (l) labels.push(l);
                        if (l === 'people' || l.includes('show everyone')
                            || l.includes('participant')
                            || (l.includes('people') && !l.includes('add'))
                            || ic === 'people' || ic === 'people_alt' || ic === 'group') {
                            b.click();
                            return {state: 'clicked', label: l || ('icon:' + ic)};
                        }
                    }
                    return {state: 'none', labels: labels.slice(0, 25)};
                }""",
                _throttle_ok,
            )
            state = (res or {}).get("state")
            if state == "open":
                self._roster_open_failures = 0
            elif state == "clicked":
                self._roster_open_attempt_at = time.time()
                await asyncio.sleep(0.6)  # let the panel render its rows
                self._roster_open_failures = 0
                if not self._roster_panel_logged:
                    self._roster_panel_logged = True
                    logger.info("People panel: clicked open via %s — roster now readable", res.get("label"))
            elif state == "none":
                self._roster_open_attempt_at = time.time()
                self._roster_open_failures += 1
                # Log the available button labels a few times so we can see why the
                # People button isn't matching in this Meet build (then go quiet).
                if self._roster_open_failures <= 3:
                    logger.warning(
                        "People panel: no People button found (attempt %d) — toolbar labels=%s",
                        self._roster_open_failures, res.get("labels"),
                    )
        except Exception as exc:
            self._roster_open_failures += 1
            logger.debug("People panel open failed: %s", exc)

    async def _open_chat_panel_once(self) -> None:
        """Click Google Meet's Chat toolbar button one time so the chat
        panel's DOM gets mounted — the chat MutationObserver (injected via
        add_init_script) then keeps capturing new messages regardless of
        whether the panel stays visually open afterward. Best-effort: chat
        capture never blocks the bot if the button can't be found."""
        if self._chat_panel_opened:
            return
        try:
            res = await self.page.evaluate(
                """() => {
                    const cands = Array.from(document.querySelectorAll('button,[role="button"]'));
                    for (const b of cands) {
                        const l = (b.getAttribute('aria-label') || '').toLowerCase();
                        const icon = (b.querySelector('i') ? b.querySelector('i').textContent : '') || '';
                        const ic = icon.trim().toLowerCase();
                        if (l.includes('chat with everyone') || l === 'chat'
                            || (l.includes('chat') && !l.includes('unchat'))
                            || ic === 'chat' || ic === 'insert_comment' || ic === 'comment') {
                            b.click();
                            return { state: 'clicked', label: l || ('icon:' + ic) };
                        }
                    }
                    return { state: 'none' };
                }"""
            )
            if (res or {}).get("state") == "clicked":
                self._chat_panel_opened = True
                logger.info("Chat panel: opened once via %s (mounts chat DOM for observer)", res.get("label"))
                await asyncio.sleep(0.8)
            else:
                logger.warning("Chat panel: no Chat button found — chat capture unavailable this meeting")
        except Exception as exc:
            logger.debug("Chat panel open failed: %s", exc)

    async def _scrape_chat_messages(self) -> None:
        """Drain window.__chatBuffer (filled by the chat MutationObserver) into
        self._chat_messages. Called every poll tick from _poll_loop(), mirrors
        the Tier-1 buffer drain in _scrape_captions(). Best-effort — never
        raises, never blocks the bot."""
        if self.meeting_start_time is None:
            return
        try:
            entries = await self.page.evaluate(
                "() => { var b = window.__chatBuffer || []; window.__chatBuffer = []; return b; }"
            )
        except Exception as exc:
            logger.debug("Chat buffer drain failed: %s", exc)
            return
        if not entries:
            return
        bot_name_lc = os.getenv("BOT_DISPLAY_NAME", "OneClarity AI").lower()
        for entry in entries:
            sender = (entry.get("sender") or "Unknown").strip()
            text = (entry.get("text") or "").strip()
            if not text or sender.lower() == bot_name_lc:
                continue
            sent_at = round(time.time() - self.meeting_start_time, 2)
            self._chat_messages.append({"sender": sender, "text": text, "sent_at": sent_at})
            logger.info("Chat [%s]: %s", sender, text[:80])

    async def _read_roster_others(self) -> Optional[int]:
        """Return the number of OTHER (non-bot) participants from Meet's people
        panel, or None if the panel can't be read reliably.

        Counts muted attendees (they still appear as rows) and falls to 0 the
        instant everyone leaves — so it fixes BOTH 'bot leaves while people are
        muted' AND 'bot lingers 10 min after the meeting actually ended'."""
        try:
            bot_name = os.getenv("BOT_DISPLAY_NAME", "OneClarity AI").lower()
            result = await self.page.evaluate(
                r"""() => {
                    // Identify the PEOPLE panel STRUCTURALLY rather than via brittle
                    // class names: group every role="listitem" by its nearest
                    // role="list" ancestor, then pick the group whose rows carry
                    // participant name-chips. Chat messages / caption rows also use
                    // listitem but never carry these chips, so they're excluded.
                    const chipSel = '[data-self-name],[jsname="EzdCnc"],[jsname="tgaKEf"],' +
                                    '.zWfAFd,.KV1GEc,.dwSJse,.XWGOtd';
                    const items = Array.from(document.querySelectorAll('[role="listitem"]'));
                    if (!items.length) return { scoped: false, names: [] };
                    const groups = new Map();
                    for (const it of items) {
                        const root = it.closest('[role="list"]') || it.parentElement;
                        if (!root) continue;
                        if (!groups.has(root)) groups.set(root, []);
                        groups.get(root).push(it);
                    }
                    let best = null, bestScore = 0;
                    for (const [, its] of groups) {
                        let score = 0;
                        for (const it of its) if (it.querySelector(chipSel)) score++;
                        if (score > bestScore) { bestScore = score; best = its; }
                    }
                    if (!best || bestScore === 0) return { scoped: false, names: [] };
                    // Hand-raise indicator: an icon/aria-label on the row itself.
                    // Google Meet renders this as a Material Symbols ligature
                    // ("back_hand"/"front_hand") or an aria-label containing
                    // "raised hand" — checked both ways since the exact markup
                    // is not documented and can change between Meet builds.
                    const handRe = /raised (their |a )?hand/i;
                    function rowHandRaised(it) {
                        const lbl = it.getAttribute('aria-label') || '';
                        if (handRe.test(lbl)) return true;
                        const icons = it.querySelectorAll('i');
                        for (const ic of icons) {
                            const t = (ic.textContent || '').trim().toLowerCase();
                            if (t === 'back_hand' || t === 'front_hand') return true;
                        }
                        return false;
                    }
                    const out = [];
                    const raised = [];
                    const seen = new Set();
                    for (const it of best) {
                        let n = '';
                        const chip = it.querySelector(chipSel);
                        if (chip) n = (chip.innerText || chip.textContent || '').trim();
                        if (!n) {
                            const lbl = it.getAttribute('aria-label') || '';
                            n = lbl.split('\n')[0].trim();
                        }
                        if (!n) n = (it.innerText || '').split('\n')[0].trim();
                        n = n.trim();
                        if (n && !seen.has(n.toLowerCase())) {
                            seen.add(n.toLowerCase());
                            out.push(n);
                            if (rowHandRaised(it)) raised.push(n);
                        }
                    }
                    return { scoped: true, names: out, raised: raised };
                }"""
            )
            # Only a scoped read of the identified people panel is trustworthy.
            if not result or not result.get("scoped"):
                return None  # panel not identifiable → caller falls back to Layers 1-4
            names = result.get("names") or []
            others = [
                n for n in names
                if self._is_real_name(n)
                and n.lower() != bot_name
                and n.strip().lower() not in ("you", "you (you)")
            ]
            raised_raw = result.get("raised") or []
            raised_others = [
                n for n in raised_raw
                if self._is_real_name(n)
                and n.lower() != bot_name
                and n.strip().lower() not in ("you", "you (you)")
            ]
            self._update_hand_raise_state(raised_others)
            return len(others)
        except Exception as exc:
            logger.debug("Roster read failed: %s", exc)
            return None

    def _update_hand_raise_state(self, currently_raised: List[str]) -> None:
        """Diff this tick's raised-hand names (from _read_roster_others) against
        the last known state and record raise/lower events with meeting-relative
        timestamps into self._hand_raise_events. Any hand still up at teardown
        is closed out there with lowered_at=None."""
        if self.meeting_start_time is None:
            return
        now = round(time.time() - self.meeting_start_time, 2)
        current_set = set(currently_raised)

        for name in current_set:
            if name not in self._hand_raise_state:
                self._hand_raise_state[name] = now
                logger.info("Hand raised: %s @ %.1fs", name, now)

        lowered = [name for name in self._hand_raise_state if name not in current_set]
        for name in lowered:
            raised_at = self._hand_raise_state.pop(name)
            self._hand_raise_events.append(
                {"speaker": name, "raised_at": raised_at, "lowered_at": now}
            )
            logger.info("Hand lowered: %s (raised %.1fs → %.1fs)", name, raised_at, now)

    async def _count_live_participants(self) -> int:
        """Return count of OTHER real humans currently in the meeting.

        Layer 0 — Authoritative roster: read Meet's people panel (counts muted
                  people, drops to 0 the moment everyone leaves). Primary signal.
                  Falls through to the layers below only when the panel can't be
                  read, so there is no regression where it previously worked.

        Three-layer defence against Google Meet DOM virtualization:
          Layer 1 — DOM: union of tile names, people-panel rows, and name chips
                    filtered through _is_real_name() so UI strings / bot name
                    are always excluded.
          Layer 2 — Caption recency: if DOM returns 0 but a named speaker had
                    a finalized caption within the last 90 s, treat as live=1.
                    Covers temporary tile virtualization during screen-share /
                    presentation-mode transitions.
          Layer 3 — Known-participants guard: if DOM returns 0 but we've seen
                    real participants before AND there was recent caption activity
                    (within 120 s), stay in the meeting.

        Returns -1 if the DOM itself cannot be read (page loading/crash).
        """
        # ── Layer 0: authoritative people-panel roster ───────────────────────
        # Make sure the panel is open, then read the true count. This counts
        # muted people AND falls to 0 the instant everyone leaves.
        await self._ensure_people_panel_open()
        _roster = await self._read_roster_others()
        if _roster is not None:
            self._roster_reliable = True
            if _roster > 0:
                self._last_confirmed_presence_time = time.time()
            return _roster

        try:
            bot_name = os.getenv("BOT_DISPLAY_NAME", "OneClarity AI").lower()
            # JS returns a deduplicated list of raw name strings from 3 DOM sources.
            raw_names: list[str] = await self.page.evaluate(f"""
                () => {{
                    const botName = {repr(bot_name)};
                    const names = new Set();

                    function addName(raw) {{
                        if (!raw) return;
                        // Strip aria-label suffixes: "'s video", "(presenter)", "is sharing…"
                        let n = (raw || '').trim()
                            .replace(/['']s\\s+(?:video|screen|audio|camera)[^]*/gi, '')
                            .replace(/\\s+(?:is|are|has)\\s+[^]*/gi, '')
                            .replace(/\\s*\\(.*\\)[^]*$/g, '')
                            .trim();
                        if (n.length >= 2 && n.length <= 80) names.add(n);
                    }}

                    // Source 1: participant tile containers (video grid)
                    document.querySelectorAll('[data-participant-id]').forEach(tile => {{
                        addName(tile.getAttribute('aria-label'));
                        addName(tile.getAttribute('data-display-name'));
                        // name chip text inside the tile
                        const chip = tile.querySelector(
                            '[data-self-name],[jsname="EzdCnc"],[jsname="tgaKEf"],' +
                            '.zWfAFd,.KF4T6b,.cS7aqe,.dwAOEb,.NjCFcc'
                        );
                        if (chip) addName(chip.innerText || chip.textContent);
                    }});

                    // Source 2: people-panel list rows (visible when panel is open)
                    document.querySelectorAll(
                        '[role="listitem"] [jscontroller],' +
                        '.rua5Nb .zWfAFd,.rua5Nb .KF4T6b'
                    ).forEach(el => addName(el.innerText || el.textContent));

                    // Source 3: any aria-label containing "'s video" or "’s video"
                    // (Meet uses both straight and curly apostrophe — match both)
                    document.querySelectorAll('[aria-label]').forEach(el => {{
                        const lbl = el.getAttribute('aria-label') || '';
                        if (/['’]s\\s+video/i.test(lbl)) {{
                            addName(lbl);
                        }}
                    }});

                    return Array.from(names);
                }}
            """) or []

            # Filter through Python's _is_real_name() — reuses all existing rules
            # (blocklist, garbage filter, action-suffix check, etc.)
            bot_name_lower = bot_name.lower()
            real_names = [
                n for n in raw_names
                if self._is_real_name(n) and n.lower() != bot_name_lower
            ]
            count = len(set(real_names))  # unique humans

            if count > 0:
                self._last_confirmed_presence_time = time.time()

            if count == 0:
                # ── Layer 2: caption recency guard ───────────────────────────
                # Google Meet can temporarily hide all tiles during presentation
                # mode or screen-share transitions.  If a named speaker had a
                # finalized caption within the last 90 s, they are still present.
                # Also checks _active_captions (not-yet-finalized) because at
                # join time the caption_timeline is still empty while active
                # captions already have real participant names.
                _CAPTION_RECENCY_SEC = 90.0
                _bot_name_lower = os.getenv("BOT_DISPLAY_NAME", "OneClarity AI").lower()
                if self.meeting_start_time:
                    _elapsed = time.time() - self.meeting_start_time
                    # Check finalized caption timeline
                    _recent_speakers = [
                        e for e in self._caption_timeline[-30:]
                        if e.get("speaker") and e["speaker"] != "Unknown"
                        and e["speaker"].lower() != _bot_name_lower
                        and (_elapsed - e["end_time"]) < _CAPTION_RECENCY_SEC
                    ]
                    # Also check still-active captions (not yet finalized)
                    _active_real = [
                        spk for spk in self._active_captions
                        if spk and spk != "Unknown"
                        and spk.lower() != _bot_name_lower
                        and self._is_real_name(spk)
                    ]
                    if _recent_speakers:
                        last = _recent_speakers[-1]
                        _ago = _elapsed - last["end_time"]
                        _now = time.time()
                        if _now - self._dom_zero_log_at >= 30.0:
                            self._dom_zero_log_at = _now
                            logger.info(
                                "_count_live_participants: DOM=0 but '%s' captioned %.0fs ago "
                                "— tile virtualization suspected, treating as live=1 (throttled log)",
                                last["speaker"], _ago,
                            )
                        self._last_confirmed_presence_time = time.time()
                        return 1
                    if _active_real:
                        _now = time.time()
                        if _now - self._dom_zero_log_at >= 30.0:
                            self._dom_zero_log_at = _now
                            logger.info(
                                "_count_live_participants: DOM=0 but %d real speaker(s) "
                                "visible in active captions %s — treating as live (throttled log)",
                                len(_active_real), _active_real,
                            )
                        self._last_confirmed_presence_time = time.time()
                        return len(_active_real)

                # ── Layer 3: known-participants + active-speaker guard ────────
                # If we've ever seen participants and the active-speaker timeline
                # shows activity in the last 120 s, don't trust DOM=0 alone.
                _ACTIVE_RECENCY_SEC = 120.0
                if self._has_seen_participants and self._active_speaker_timeline and self.meeting_start_time:
                    _elapsed = time.time() - self.meeting_start_time
                    _recent_active = [
                        e for e in self._active_speaker_timeline[-20:]
                        if (_elapsed - e["abs_time"]) < _ACTIVE_RECENCY_SEC
                        and e.get("speaker") and e["speaker"] != "Unknown"
                    ]
                    if _recent_active:
                        last_a = _recent_active[-1]
                        _ago_a = _elapsed - last_a["abs_time"]
                        logger.warning(
                            "_count_live_participants: DOM=0 but active-speaker '%s' seen %.0fs ago "
                            "— treating as live=1  known_participants=%s",
                            last_a["speaker"], _ago_a, self._known_participants,
                        )
                        self._last_confirmed_presence_time = time.time()
                        return 1

                # ── Layer 4: last-confirmed-presence grace window (10 min) ────
                # If all DOM/caption/active-speaker signals are dead but we had
                # confirmed presence within the last 10 minutes, the meeting is
                # likely just silent (everyone muted/reading).  Stay in the call.
                # The grace window does NOT auto-reset — it consumes naturally so
                # the bot still leaves after a true meeting end.
                # Grace window after the last DOM/caption/active-speaker signal.
                # Tunable via BOT_SILENCE_GRACE_SEC. Default 120s (was 600s) so the
                # bot leaves ~2 min after everyone actually leaves instead of ~10.
                # When the people-panel roster (Layer 0) is readable it makes this
                # path moot — roster drops to 0 immediately on departure.
                _SILENCE_GRACE_SEC = float(os.getenv("BOT_SILENCE_GRACE_SEC", "120"))
                _bot_name_lower_l4 = os.getenv("BOT_DISPLAY_NAME", "OneClarity AI").lower()
                _real_known = [p for p in self._known_participants if p.lower() != _bot_name_lower_l4]
                if _real_known and self._last_confirmed_presence_time is not None:
                    _since = time.time() - self._last_confirmed_presence_time
                    if _since < _SILENCE_GRACE_SEC:
                        _now = time.time()
                        if _now - self._dom_zero_log_at >= 30.0:
                            self._dom_zero_log_at = _now
                            logger.warning(
                                "_count_live_participants: DOM=0 all guards expired — "
                                "SILENT MEETING: last confirmed presence %.0fs ago (grace=%.0fs). "
                                "Participants %s likely muted. Bot staying in (throttled log).",
                                _since, _SILENCE_GRACE_SEC, _real_known,
                            )
                        return len(_real_known)

                # Genuine zero — log candidates for production debugging
                logger.debug(
                    "_count_live_participants=0  raw_names=%s  known_participants=%s  "
                    "has_seen=%s  caption_timeline_len=%d",
                    raw_names or "[]",
                    self._known_participants,
                    self._has_seen_participants,
                    len(self._caption_timeline),
                )

            return count

        except Exception as exc:
            logger.debug("_count_live_participants DOM read failed: %s", exc)
            return -1

    # ── Caption overlap-based speaker resolution ──────────────────────────────

    async def _flush_stale_captions(self, flush_all: bool = False) -> None:
        """
        Insert any pending captions that have waited > _CAP_PENDING_TTL_SEC without
        being matched by an STT chunk (1e).  Called from the caption path every tick
        and from teardown (flush_all=True) to drain the buffer completely.
        """
        if not self._cap_pending or self.meeting_id is None:
            return
        now = time.time()
        to_flush = []
        keep = []
        for cap in self._cap_pending:
            if flush_all or (now - cap["queued_at"]) > self._CAP_PENDING_TTL_SEC:
                to_flush.append(cap)
            else:
                keep.append(cap)
        self._cap_pending = keep
        for cap in to_flush:
            try:
                await self.client.add_transcript(
                    meeting_id=self.meeting_id,
                    text=cap["text"],
                    speaker_name=cap["speaker"],
                    source="caption",
                    start_time=round(cap["abs_start"], 2) if cap["abs_start"] is not None else None,
                    end_time=round(cap["abs_end"], 2) if cap["abs_end"] is not None else None,
                    confidence="high",
                )
                logger.debug("Flushed stale caption [speaker=%s]: %s", cap["speaker"], cap["text"][:80])
            except Exception as exc:
                logger.error("Failed to flush stale caption: %s — text=%r", exc, cap["text"][:60])

    # ── Measured speaker attribution (WebRTC RTP source + audioLevel) ──────────
    @staticmethod
    def _canonical_source(csrc, ssrc) -> Optional[tuple]:
        """Single source-id selection used EVERYWHERE (ingest, dominant window,
        chunk fallback) so the learned key and the lookup key can never drift.
        Prefer CSRC (the true contributing source); else SSRC. Namespaced so a
        CSRC int can't alias an SSRC int."""
        if csrc is not None:
            return ("c", csrc)
        if ssrc is not None:
            return ("s", ssrc)
        return None

    def _ingest_speaker_samples(self, samples: list) -> None:
        """Fold JS audio-level samples into the measured timeline. Learn
        source→name ONLY from SINGLE-SPEAKER ticks (exactly one source above the
        speaking threshold in a 250ms tick) — never during crosstalk, since the
        DOM active-speaker name is a single global value and would otherwise be
        mis-stamped onto a second speaker's source. DOM learning also requires
        repeated agreement and never overrides a caption-locked source."""
        bot_lc = os.getenv("BOT_DISPLAY_NAME", "OneClarity AI").lower()
        by_tick: Dict[float, list] = {}
        for s in samples:
            src = self._canonical_source(s.get("csrc"), s.get("ssrc"))
            if src is None:
                continue
            try:
                level = float(s.get("level") or 0.0)
            except (TypeError, ValueError):
                level = 0.0
            name = (s.get("name") or "").strip() or None
            t = s.get("t")
            self._measured_speaker_timeline.append({
                "t_ms": t, "track": s.get("track"),
                "source": src, "level": level, "name": name,
            })
            if t is not None:
                by_tick.setdefault(t, []).append((src, level, name))

        # Learn only from ticks where a SINGLE source is clearly speaking AND no
        # other source has meaningful energy — so the one global DOM active-speaker
        # name is unambiguously this source's owner. Requiring the others to be
        # below the speaking floor (a clear gap) avoids binding A's name to B when
        # A momentarily dips between syllables during near-overlap.
        for _t, items in by_tick.items():
            speaking = [(src, lvl, nm) for (src, lvl, nm) in items if lvl >= _SSRC_LEARN_MIN_LEVEL]
            if len(speaking) != 1:
                continue
            others_quiet = all(
                lvl < _SSRC_SPEAKING_LEVEL
                for (src, lvl, nm) in items
                if src != speaking[0][0]
            )
            if not others_quiet:
                continue
            src, _lvl, nm = speaking[0]
            if not nm or nm.lower() == bot_lc or not self._is_real_name(nm):
                continue
            self._bind_source_name(src, nm, confident=False)

        if len(self._measured_speaker_timeline) > _SSRC_TIMELINE_MAX:
            self._measured_speaker_timeline = (
                self._measured_speaker_timeline[-_SSRC_TIMELINE_MAX // 2:]
            )

    def _dominant_source_for_window(
        self, chunk_ts_ms: Optional[float], chunk_dur_sec: float,
        track_id: Optional[str] = None,
    ) -> Optional[tuple]:
        """Return the canonical source id with the most audio energy during the
        chunk's window [ts-dur, ts]. Restricted to the chunk's own track when
        known, so two simultaneously-active tracks never collapse onto one
        source. Returns None on a near-tie so attribution defers to captions."""
        if not chunk_ts_ms or not self._measured_speaker_timeline:
            return None
        start_ms = chunk_ts_ms - (chunk_dur_sec * 1000.0) - 500.0  # small lead-in
        energy: Dict[tuple, float] = {}
        for e in self._measured_speaker_timeline:
            t = e.get("t_ms")
            if t is None or t < start_ms or t > chunk_ts_ms:
                continue
            if track_id and e.get("track") and e["track"] != track_id:
                continue
            if e["level"] >= _SSRC_SPEAKING_LEVEL:
                energy[e["source"]] = energy.get(e["source"], 0.0) + e["level"]
        if not energy:
            return None
        ranked = sorted(energy.items(), key=lambda x: x[1], reverse=True)
        if len(ranked) >= 2 and ranked[1][1] >= 0.80 * ranked[0][1]:
            return None  # measured near-tie → not confident enough; defer to captions
        return ranked[0][0]

    def _name_for_source(self, source: Optional[tuple]) -> Optional[str]:
        """Learned source-id → participant name (normalized), or None if unknown."""
        if source is None:
            return None
        name = self._source_speaker_map.get(source)
        if not name:
            return None
        return normalize_speaker_name(name, self._alias_map)

    def _bind_source_name(self, source: Optional[tuple], name: Optional[str],
                          confident: bool) -> None:
        """Bind a canonical source id to a participant name.
          confident=True  (from a confident caption): locks the source, may evict
                          a weaker (unlocked) binding that owns the same name, and
                          overrides a previous DOM-learned name.
          confident=False (DOM single-speaker tick): requires 3 agreeing
                          observations and never overrides a caption-locked source."""
        if source is None or not name:
            return
        bot_lc = os.getenv("BOT_DISPLAY_NAME", "OneClarity AI").lower()
        name = normalize_speaker_name(name, self._alias_map)
        if not name or name.lower() == bot_lc or not self._is_real_name(name):
            return
        # Bijection: a real name belongs to one source. Release a weaker holder.
        for _src, _nm in list(self._source_speaker_map.items()):
            if _nm == name and _src != source:
                if confident and _src not in self._source_locked:
                    del self._source_speaker_map[_src]
                else:
                    return  # name already owned (locked, or we're not confident)
        if confident:
            if self._source_speaker_map.get(source) != name:
                self._source_speaker_map[source] = name
                self._source_locked.add(source)
                logger.info("Source lock learned (caption): source=%s → %s", source, name)
            return
        # DOM single-speaker-tick vote — never override a caption-locked source.
        if source in self._source_locked:
            return
        key = (source, name)
        self._source_name_votes[key] = self._source_name_votes.get(key, 0) + 1
        if self._source_name_votes[key] >= 3 and self._source_speaker_map.get(source) != name:
            self._source_speaker_map[source] = name
            logger.info("Source learned (DOM x%d): source=%s → %s",
                        self._source_name_votes[key], source, name)

    def _resolve_speaker_from_captions(
        self, abs_start: float, abs_end: float
    ) -> dict:
        """
        Find the best speaker name for an STT chunk using caption timeline overlap.

        Search window: [abs_start - 2s, abs_end + 2s]

        Returns a dict with:
            speaker          – resolved name or None
            source           – "caption" | "mixed_caption" | "no_caption"
            confidence       – 0.0–1.0
            overlap_ms       – int (ms of best speaker's overlap with search window)
            is_overlapping   – True when multiple speakers share the chunk window
            secondary        – [{speaker, overlap_ms}, ...]
        """
        _empty = {
            "speaker": None, "source": "no_caption", "confidence": 0.0,
            "overlap_ms": 0, "is_overlapping": False, "ambiguous": False,
            "secondary": [],
        }
        if not self.meeting_start_time:
            return _empty

        search_start = abs_start - 2.0
        search_end = abs_end + 2.0
        chunk_span = max(0.1, search_end - search_start)

        # Merge overlap per speaker from finalized timeline + still-active captions
        overlap_by_speaker: Dict[str, float] = {}

        for entry in self._caption_timeline:
            spk = entry["speaker"]
            o = max(0.0, min(search_end, entry["end_time"]) - max(search_start, entry["start_time"]))
            if o >= _CAPTION_MIN_OVERLAP_SEC:
                overlap_by_speaker[spk] = overlap_by_speaker.get(spk, 0.0) + o

        now = time.time() - self.meeting_start_time
        for spk, cap in self._active_captions.items():
            o = max(0.0, min(search_end, now) - max(search_start, cap["start_time"]))
            if o >= _CAPTION_MIN_OVERLAP_SEC:
                overlap_by_speaker[spk] = overlap_by_speaker.get(spk, 0.0) + o

        if not overlap_by_speaker:
            return _empty

        # Bot never speaks — remove it so it can never win attribution.
        # Prevents roster artifacts (where bot has identical overlap to real speakers)
        # from falsely attributing a participant's speech to the bot.
        _bot_lc = os.getenv("BOT_DISPLAY_NAME", "OneClarity AI").lower()
        overlap_by_speaker = {s: o for s, o in overlap_by_speaker.items() if s.lower() != _bot_lc}
        if not overlap_by_speaker:
            return _empty

        ranked = sorted(overlap_by_speaker.items(), key=lambda x: x[1], reverse=True)
        best_speaker, best_overlap = ranked[0]
        best_ratio = best_overlap / chunk_span

        secondary = [
            {"speaker": s, "overlap_ms": int(o * 1000)}
            for s, o in ranked[1:]
            if o >= _CAPTION_SECONDARY_MIN_SEC
        ]
        is_overlapping = len(secondary) > 0

        # Crosstalk ambiguity: when the runner-up's overlap is near-equal to the
        # top speaker's, caption timing CANNOT tell them apart — both caption
        # bubbles span the same chunk window, so the highest-overlap pick is just
        # whichever speaker happens to be first in dict order. Flag it so the
        # caller disambiguates via stable per-track identity instead of guessing.
        is_ambiguous = (
            len(ranked) >= 2
            and ranked[0][1] > 0
            and ranked[1][1] >= _CAPTION_AMBIGUOUS_RATIO * ranked[0][1]
        )

        logger.info(
            "Caption attribution: chunk=[%.1f,%.1f] overlaps=%s → best=%s ratio=%.2f overlapping=%s ambiguous=%s",
            abs_start, abs_end,
            {s: round(o, 2) for s, o in ranked},
            best_speaker, best_ratio, is_overlapping, is_ambiguous,
        )

        # Case A: clear dominant speaker
        if best_ratio >= _CAPTION_DOMINANT_RATIO and not is_overlapping:
            return {
                "speaker": best_speaker,
                "source": "caption",
                "confidence": round(best_ratio, 4),
                "confidence_label": "high",   # 1c: quality flag for summarizer
                "overlap_ms": int(best_overlap * 1000),
                "is_overlapping": False,
                "ambiguous": False,
                "secondary": [],
            }

        # Case B/C: multiple speakers (crosstalk) → confidence_label="low" (1c)
        return {
            "speaker": best_speaker if best_ratio >= 0.35 else None,
            "source": "mixed_caption",
            "confidence": round(best_ratio, 4),
            "confidence_label": "low",        # 1c: crosstalk — don't treat as firm decisions
            "overlap_ms": int(best_overlap * 1000),
            "is_overlapping": is_overlapping,
            "ambiguous": is_ambiguous,
            "secondary": secondary,
        }

    def _find_speaker_by_text(self, stt_text: str, threshold: float = 0.35) -> Optional[str]:
        """
        Fuzzy-match STT text against finalized caption entries to recover speaker name.
        Used as Priority ② when time-overlap (_resolve_speaker_from_captions) returns None.
        Returns the speaker name from the caption entry with the highest similarity score,
        or None if no entry exceeds `threshold`.
        """
        if not stt_text or not self._caption_timeline:
            return None
        best_score, best_speaker = 0.0, None
        stt_lower = stt_text.lower()
        for entry in self._caption_timeline:
            spk = entry.get("speaker", "")
            if not spk or spk == "Unknown":
                continue
            cap_lower = entry.get("text", "").lower()
            if not cap_lower:
                continue
            score = difflib.SequenceMatcher(None, stt_lower, cap_lower).ratio()
            if score > best_score and score >= threshold:
                best_score = score
                best_speaker = spk
        if best_speaker:
            logger.info("STT [text-match] speaker=%s score=%.2f text=%.60s", best_speaker, best_score, stt_text)
        return best_speaker

    # ── STT audio loop ────────────────────────────────────────────────────────
    #
    # Mode A — WebRTC (deployment + local): intercepts browser audio tracks.
    #   Each track = one participant → speaker name known, no diarization needed.
    #   Works headless/Docker with zero system audio device.
    #
    # Mode B — sounddevice fallback (local only): captures mic/loopback.
    #   Speaker assigned from caption timeline (last active speaker).
    #   Used only when WebRTC yields no data after WEBRTC_WAIT_SEC seconds.

    _WEBRTC_WAIT_SEC = 20   # wait this long for WebRTC tracks before falling back

    async def _audio_stt_loop(self) -> None:
        if not self.sarvam.api_key:
            logger.warning("SARVAM_API_KEY not set — audio STT disabled")
            return

        # Audio capture strategy:
        # HEADLESS=true: WebRTC interceptor captures per-track audio directly from JS.
        #   --use-fake-device-for-media-stream allows WebRTC to receive incoming audio.
        #   PulseAudio path is skipped (no audio goes to PulseAudio in headless mode).
        # HEADLESS=false (local dev): run both WebRTC + PulseAudio in parallel.
        if HEADLESS:
            logger.info("STT loop: headless mode — WebRTC only (proven working in Docker)")
            await self._webrtc_stt_loop()
        else:
            logger.info("STT loop: headful mode — WebRTC + PulseAudio in parallel")
            results = await asyncio.gather(
                self._webrtc_stt_loop(),
                self._sounddevice_stt_loop(),
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, Exception):
                    logger.error("STT loop error: %s", r, exc_info=r)
        await self.sarvam.close()

    async def _webrtc_stt_loop(self) -> None:
        """WebRTC per-track audio — captures active speaker with known name."""
        webrtc = WebRTCAudioCapturer(self.page)
        webrtc_got_data = False
        webrtc_start = time.time()
        # Fallback track_id → participant_name mapping.
        # Google Meet's SFU provides only ~3 audio slots for any size meeting.
        # Slots are assigned in join order — first joiner → slot 0, etc.
        # Since _known_participants is now an ordered List, available[0] always
        # picks the earliest-joining unassigned participant (correct order).
        _track_assignment: dict = {}

        logger.info("WebRTC STT: starting (waiting %ds for tracks …)", self._WEBRTC_WAIT_SEC)

        try:
            async for _chunk in webrtc.chunks():
                if not self._running:
                    break
                if self.meeting_id is None or self.meeting_start_time is None:
                    continue

                participant_name = _chunk.get("participant_name")
                wav_bytes = _chunk.get("wav")
                if not wav_bytes:
                    continue
                # Keep None when absent — coercing to a literal "unknown" would
                # disable the measured-window track filter and collapse all
                # no-track chunks onto one P4 assignment. All track_id uses below
                # are guarded by `if track_id`.
                track_id = _chunk.get("track_id") or None
                _chunk_ssrc = _chunk.get("ssrc")
                _chunk_csrc = _chunk.get("csrc")
                _chunk_level = _chunk.get("level") or 0.0
                _chunk_ts_ms = _chunk.get("ts")

                webrtc_got_data = True

                # Pull the latest per-source audio-level samples and fold them into
                # the measured speaker timeline + source-id→name learning. This is
                # the signal that replaces caption-overlap guessing.
                try:
                    _samples = await webrtc.drain_speaker_samples()
                    if _samples:
                        self._ingest_speaker_samples(_samples)
                        # One-time LIVE log the first time we see real audio energy —
                        # tells operators the measured signal actually works on this
                        # Meet build (vs audioLevel not being negotiated → stays caption-only).
                        if not self._measured_signal_logged:
                            _max_lvl = max((float(s.get("level") or 0) for s in _samples), default=0.0)
                            if _max_lvl >= _SSRC_SPEAKING_LEVEL:
                                self._measured_signal_logged = True
                                logger.info(
                                    "Measured speaker signal LIVE — WebRTC audioLevel present "
                                    "(max=%.3f, sources tracked=%d). SSRC attribution active.",
                                    _max_lvl, len({e["source"] for e in self._measured_speaker_timeline}),
                                )
                except Exception as _se:
                    logger.debug("speaker-sample ingest failed: %s", _se)

                # WebRTC delivers audio AFTER buffering it — chunk arrive time ≈ audio END.
                # Compute the actual audio window from the WAV duration so that caption
                # overlap lookups and _cap_pending merge use the correct time range.
                _chunk_arrive = time.time() - self.meeting_start_time
                try:
                    import io as _io, wave as _wv
                    with _wv.open(_io.BytesIO(wav_bytes)) as _wf:
                        _chunk_dur = _wf.getnframes() / float(_wf.getframerate())
                except Exception:
                    _chunk_dur = 5.0
                abs_end = _chunk_arrive                      # actual audio end
                abs_start = max(0.0, abs_end - _chunk_dur)  # actual audio start

                # ── Measured speaker (SSRC/CSRC) for THIS chunk ───────────────
                # Prefer the source that dominated the chunk's audio window (from
                # the 250ms samples); fall back to the boundary ssrc on the chunk.
                _measured_src = self._dominant_source_for_window(_chunk_ts_ms, _chunk_dur, track_id)
                if _measured_src is None and _chunk_level >= _SSRC_SPEAKING_LEVEL:
                    # Same canonical selection as the timeline so keys never drift.
                    _measured_src = self._canonical_source(_chunk_csrc, _chunk_ssrc)
                _measured_speaker = self._name_for_source(_measured_src)

                # Ensure participant list is fresh before speaker resolution
                try:
                    await self._scrape_participants()
                except Exception:
                    pass

                # If WebRTC JS returned no name, try a direct DOM active-speaker call now.
                # This covers the case where _getActiveSpeakerName() inside the JS recorder
                # polled too early (before the speaking indicator appeared in the DOM).
                if not participant_name:
                    try:
                        _dom_spk = await self.page.evaluate("() => window.__getActiveSpeakerName ? window.__getActiveSpeakerName() : null")
                        if _dom_spk and _dom_spk != "Unknown":
                            participant_name = _dom_spk
                            logger.debug("WebRTC: resolved participant_name from DOM fallback: %s", participant_name)
                    except Exception:
                        pass

                result = await self.sarvam.transcribe(wav_bytes)
                if not result:
                    continue

                transcript = result.get("transcript", "").strip()
                if not transcript or _is_garbage(transcript):
                    continue

                lang = result.get("language_code", "unknown")

                # ── Speaker resolution for WebRTC STT ───────────────────────
                # Priority order (per spec):
                #   1. Caption overlap — highest overlap duration wins
                #   2. Active DOM majority vote (who was highlighted in Meet)
                #   3. WebRTC track participant name (validated against known list)
                #   4. Track cache / join order
                #   5. Unknown Speaker
                bot_name_str = os.getenv("BOT_DISPLAY_NAME", "OneClarity AI")
                bot_name_lower = bot_name_str.lower()
                _meeting_title_lower = (self._title or "").lower().strip()

                # Init attribution metadata
                _attr_source: Optional[str] = None
                _attr_confidence: Optional[float] = None
                _attr_overlap_ms: Optional[int] = None
                _attr_is_overlapping: Optional[bool] = None
                _attr_secondary: Optional[list] = None
                speaker: Optional[str] = None       # must be initialised here — P0.5 reads it
                _crosstalk_unresolved = False

                # Wait briefly on early chunks for captions to appear
                _is_early = abs_start < 60.0
                if _is_early and not self._caption_timeline and not self._active_captions:
                    for _ in range(6):
                        await asyncio.sleep(0.5)
                        if self._caption_timeline or self._active_captions:
                            break

                # ── Priority 0.5: Active DOM majority vote (tight window) ─────
                # Uses the same data-active-speaker DOM signal that makes participant
                # scraping accurate. Window is tight (chunk ± 2s) so only entries
                # that overlap this utterance are counted — prevents a loud earlier
                # speaker from swamping the vote.
                if not speaker and self._active_speaker_timeline:
                    _dom_votes: Dict[str, float] = {}
                    _prev_t: Optional[float] = None
                    _prev_n: Optional[str] = None
                    for _entry in self._active_speaker_timeline:
                        _t = _entry["abs_time"]
                        _n = _entry["speaker"]
                        if abs_start - 2.0 <= _t <= abs_end + 2.0:
                            _dur = (_t - _prev_t) if (_prev_t is not None and _prev_n == _n) else 0.5
                            _dom_votes[_n] = _dom_votes.get(_n, 0) + _dur
                        _prev_t = _t
                        _prev_n = _n
                    if _dom_votes:
                        _dom_winner = max(_dom_votes, key=lambda k: _dom_votes[k])
                        _dom_total = sum(_dom_votes.values())
                        _dom_share = _dom_votes[_dom_winner] / _dom_total if _dom_total else 0
                        if _dom_share >= 0.6:
                            # Clear majority (≥60%) — high confidence
                            speaker = _dom_winner
                            _attr_source = "active_dom_primary"
                            _attr_confidence = round(_dom_share, 2)
                            logger.info(
                                "STT [P0.5-active-dom] %s share=%.0f%% votes=%s",
                                speaker, _dom_share * 100,
                                {k: round(v, 1) for k, v in _dom_votes.items()},
                            )

                # ── Priority 1: Caption overlap ───────────────────────────────
                # Each WebRTC track carries ONE participant's audio. Captions are
                # the ground truth for *who* spoke, but when two people talk in the
                # same chunk window their caption bubbles overlap near-equally and
                # the highest-overlap pick degenerates to "first speaker in dict
                # order" — so every track gets attributed to the same person.
                #
                # Strategy:
                #   • Confident (single dominant) caption match → trust it, AND
                #     learn track_id → speaker so this track is recognised later.
                #   • Ambiguous (crosstalk tie) → don't guess from captions. Use
                #     this track's previously-learned identity; if unknown, fall
                #     through to bijective per-track assignment (Priority 4) so two
                #     tracks can never collapse onto one speaker.
                cap_result = self._resolve_speaker_from_captions(abs_start, abs_end)
                _cap_speaker = cap_result["speaker"]
                _cap_ambiguous = cap_result.get("ambiguous", False)

                # ── Priority 0: MEASURED speaker (WebRTC RTP source + audioLevel) ─
                # The chunk's own RTP source id IS the true speaker — no guessing.
                # Available once this source has been bound to a name (learned from
                # a clean single-speaker window, below). This is what makes crosstalk
                # attribution correct: each source = one person, by measurement.
                # SELF-CORRECTION: if a CONFIDENT, non-ambiguous caption disagrees
                # with the measured binding, trust the caption and relearn the
                # source — so a wrong early binding can never become permanent.
                _cap_confident = (
                    bool(_cap_speaker) and not _cap_ambiguous
                    and cap_result.get("confidence", 0) >= _TRACK_LEARN_MIN_CONF
                )
                if _measured_speaker and _measured_speaker.lower() != bot_name_lower:
                    if _cap_confident and _cap_speaker != _measured_speaker:
                        self._bind_source_name(_measured_src, _cap_speaker, confident=True)
                        speaker = _cap_speaker
                        _attr_source = "caption_corrected"
                        _attr_confidence = cap_result.get("confidence")
                        _attr_overlap_ms = cap_result.get("overlap_ms")
                        _attr_is_overlapping = cap_result.get("is_overlapping")
                        _attr_secondary = cap_result.get("secondary") or None
                        logger.info(
                            "STT [P0→corrected] source=%s measured=%s but confident "
                            "caption=%s — relearned source", _measured_src,
                            _measured_speaker, _cap_speaker,
                        )
                    else:
                        speaker = _measured_speaker
                        _attr_source = "measured_ssrc"
                        _attr_confidence = cap_result.get("confidence")
                        _attr_overlap_ms = cap_result.get("overlap_ms")
                        _attr_is_overlapping = cap_result.get("is_overlapping")
                        _attr_secondary = cap_result.get("secondary") or None
                        self._p0_hits += 1
                        logger.info(
                            "STT [P0-measured] %s (source=%s level=%.3f) — true RTP source",
                            speaker, _measured_src, _chunk_level,
                        )
                elif _measured_src is not None:
                    self._p0_misses += 1   # had a source but no name yet (observability)

                if not speaker and _cap_speaker and not _cap_ambiguous:
                    # Clear single-speaker window — trust the caption.
                    speaker = _cap_speaker
                    _attr_source = cap_result["source"]
                    _attr_confidence = cap_result["confidence"]
                    _attr_overlap_ms = cap_result["overlap_ms"]
                    _attr_is_overlapping = cap_result["is_overlapping"]
                    _attr_secondary = cap_result["secondary"] or None
                    logger.info(
                        "STT [P1-caption] %s conf=%.2f overlap=%dms overlapping=%s",
                        speaker, _attr_confidence, _attr_overlap_ms, _attr_is_overlapping,
                    )
                    # Bind the chunk's RTP source to this confident name so all
                    # future chunks on that source attribute by measurement (P0).
                    if cap_result["confidence"] >= _TRACK_LEARN_MIN_CONF:
                        self._bind_source_name(_measured_src, speaker, confident=True)
                    # Also learn this track's owner, but never steal a name already
                    # locked to a different track.
                    if track_id and cap_result["confidence"] >= _TRACK_LEARN_MIN_CONF:
                        _clash = next(
                            (t for t, n in _track_assignment.items()
                             if n == speaker and t != track_id),
                            None,
                        )
                        if _clash is None and _track_assignment.get(track_id) != speaker:
                            _track_assignment[track_id] = speaker
                            logger.info(
                                "Track lock learned: %s → %s (conf=%.2f)",
                                track_id[:8], speaker, cap_result["confidence"],
                            )

                elif not speaker and _cap_speaker and _cap_ambiguous:
                    # Crosstalk — captions can't separate the speakers. Prefer this
                    # track's learned identity over the unreliable first-ranked pick.
                    if track_id and track_id in _track_assignment:
                        speaker = _track_assignment[track_id]
                        _attr_source = "track_locked"
                        _attr_confidence = cap_result["confidence"]
                        _attr_overlap_ms = cap_result["overlap_ms"]
                        _attr_is_overlapping = True
                        _attr_secondary = cap_result["secondary"] or None
                        logger.info(
                            "STT [P1b-track-locked] %s (crosstalk; track %s)",
                            speaker, track_id[:8],
                        )
                    else:
                        # No learned identity yet → don't let caption/DOM bias
                        # (P1.5/P2) collapse this onto the first speaker. Force the
                        # bijective per-track assignment in Priority 4.
                        _crosstalk_unresolved = True

                # ── Priority 1.5: Last active caption speaker ─────────────────
                # When P1 finds no time-overlap, the most-recently-captioned speaker
                # is still more reliable than a wide DOM vote. Use it if the caption
                # is within 4s of the audio start (tightened from 8s to avoid stale
                # attribution from previous speakers).
                # Skipped for unresolved crosstalk (it carries the same first-speaker
                # bias we are trying to avoid).
                if not speaker and not _crosstalk_unresolved and self._last_active_speaker:
                    _last_cap_time = (
                        self._caption_timeline[-1]["end_time"]
                        if self._caption_timeline
                        else 0.0
                    )
                    if abs_start - _last_cap_time < 4.0:
                        speaker = normalize_speaker_name(
                            self._last_active_speaker, self._alias_map
                        )
                        _attr_source = "last_caption"
                        logger.info("STT [P1.5-last-caption] %s", speaker)

                # ── Priority 2: Active DOM majority vote (wider fallback) ────────
                # Fires only when P0.5 had no clear majority (< 60%) — use a wider
                # window (6s) to catch edge cases like brief silence between chunks.
                if not speaker and not _crosstalk_unresolved and self._active_speaker_timeline:
                    _window_as = 6.0
                    _votes: Dict[str, float] = {}
                    _prev_time: Optional[float] = None
                    _prev_name: Optional[str] = None
                    for entry in self._active_speaker_timeline:
                        t = entry["abs_time"]
                        n = entry["speaker"]
                        if abs_start - _window_as <= t <= abs_end + _window_as:
                            duration = (t - _prev_time) if (_prev_time and _prev_name == n) else 0.5
                            _votes[n] = _votes.get(n, 0) + duration
                        _prev_time = t
                        _prev_name = n
                    if _votes:
                        best_as = max(_votes, key=lambda k: _votes[k])
                        speaker = best_as
                        _attr_source = "active_dom"
                        logger.info(
                            "STT [P2-active-dom-fallback] %s votes=%s",
                            speaker, {k: round(v, 1) for k, v in _votes.items()},
                        )

                # ── Priority 3: WebRTC track participant name ──────────────────
                if not speaker and participant_name:
                    # Reject invalid names: meeting titles, bot name, "You" = bot view
                    _pn = participant_name
                    if (
                        '/' in _pn
                        or bot_name_lower in _pn.lower()
                        or _pn.strip().lower() in ("you", "unknown")
                        or len(_pn) > 40
                        or (_meeting_title_lower and _pn.lower().strip() == _meeting_title_lower)
                    ):
                        _pn = None
                    # Cross-check against known participants using fuzzy/partial match.
                    # WebRTC often returns first-name only ("Rishabh") while _known_participants
                    # has the full name ("Rishabh Tripathi") — exact match would reject it.
                    if _pn and self._known_participants:
                        _real = [p for p in self._known_participants if p.lower() != bot_name_lower]
                        if _real:
                            _pn_lower = _pn.lower()
                            # 1. Exact match
                            _matched = next((p for p in _real if p.lower() == _pn_lower), None)
                            # 2. WebRTC name is prefix of known full name ("Rishabh" → "Rishabh Tripathi")
                            if not _matched:
                                _matched = next((p for p in _real if p.lower().startswith(_pn_lower + " ")), None)
                            # 3. Known first name matches WebRTC name (reverse prefix)
                            if not _matched:
                                _matched = next((p for p in _real if _pn_lower.startswith(p.lower().split()[0] + " ") or p.lower().split()[0] == _pn_lower), None)
                            if _matched:
                                _pn = _matched  # upgrade to canonical full name
                            else:
                                logger.debug("Rejecting WebRTC name '%s' — no fuzzy match in %s", _pn, _real)
                                _pn = None
                    if _pn:
                        speaker = _pn
                        _attr_source = "webrtc"
                        logger.info("STT [P3-webrtc] %s", speaker)

                # ── Priority 4: Track cache / join order ──────────────────────
                if not speaker and track_id:
                    if track_id in _track_assignment:
                        speaker = _track_assignment[track_id]
                        _attr_source = "track_cache"
                        logger.debug("STT [P4-track-cache] %s", speaker)
                    else:
                        _real = [p for p in self._known_participants if p.lower() != bot_name_lower]
                        _assigned = set(_track_assignment.values())
                        _available = [p for p in _real if p not in _assigned]
                        if _available:
                            speaker = _available[0]
                            _track_assignment[track_id] = speaker
                            _attr_source = "track_cache"
                            logger.info("STT [P4-track-order] %s → track %s", speaker, track_id[:8])

                # ── Fallback: last active speaker (only if recent) ────────────
                if not speaker and self._last_active_speaker and self._caption_timeline:
                    last_cap_end = self._caption_timeline[-1]["end_time"] if self._caption_timeline else 0
                    if abs(time.time() - self.meeting_start_time - last_cap_end) < 30:
                        speaker = self._last_active_speaker
                        _attr_source = "last_active"

                # ── Fallback: only participant ────────────────────────────────
                if not speaker:
                    _real = [p for p in self._known_participants if p != bot_name_str]
                    if len(_real) == 1:
                        speaker = _real[0]
                        _attr_source = "single_participant"

                # 1d: normalise resolved speaker name
                speaker = normalize_speaker_name(speaker, self._alias_map)

                # 1c: determine confidence label from crosstalk detection
                _confidence_label: str = cap_result.get("confidence_label", "high")

                # 1a: split chunk by speaker using word-level timestamps
                stt_words = result.get("word_timestamps") or result.get("segments") or []
                sub_segments = split_chunk_by_speaker(
                    stt_words=stt_words,
                    speaker_timeline=self._active_speaker_timeline,
                    chunk_start=abs_start,
                    chunk_end=abs_end,
                )
                # Fallback: treat whole chunk as one segment if split returned nothing
                if not sub_segments:
                    sub_segments = [{
                        "speaker": speaker,
                        "text": transcript,
                        "start_time": abs_start,
                        "end_time": abs_end,
                    }]

                logger.info(
                    "STT [webrtc] meeting=%s t=[%.1f,%.1f] lang=%s speaker=%s source=%s conf=%s "
                    "sub_segs=%d: %s",
                    self.meeting_id, abs_start, abs_end, lang,
                    speaker or "Unknown", _attr_source or "unknown",
                    f"{_attr_confidence:.2f}" if _attr_confidence is not None else "n/a",
                    len(sub_segments), transcript[:80],
                )

                for sub in sub_segments:
                    # Per-sub-segment caption overlap gives the exact speaker name for
                    # each 1-3s window.  This is more accurate than the chunk-level P1
                    # for 15s chunks that contain multiple speakers.
                    _sub_cap = self._resolve_speaker_from_captions(
                        sub["start_time"], sub["end_time"]
                    )
                    sub_text = sub["text"].strip()
                    if not sub_text:
                        continue

                    # Only trust the sub-window caption when it is NOT ambiguous —
                    # an ambiguous (crosstalk) caption pick is "first speaker in dict
                    # order" and must never override a measured/locked chunk speaker.
                    _sub_cap_ok = _sub_cap["speaker"] if not _sub_cap.get("ambiguous") else None

                    # When the chunk was resolved by MEASUREMENT (RTP source) or a
                    # track lock, that speaker is authoritative for ambiguous
                    # sub-windows — rank it above fuzzy text matching.
                    _measured_authoritative = _attr_source in (
                        "measured_ssrc", "caption_corrected", "track_locked"
                    )

                    # Priority ②: fuzzy-match STT text only when we have no clean
                    # sub-window caption AND we're not already measurement-resolved.
                    _text_speaker = None
                    if not _sub_cap_ok and not _measured_authoritative:
                        _text_speaker = self._find_speaker_by_text(sub_text)

                    if _measured_authoritative:
                        _sub_choice = (
                            _sub_cap_ok           # ① clean sub-window caption (most precise)
                            or speaker            # ② measured/locked chunk speaker (true source)
                            or _text_speaker
                            or sub.get("speaker")
                        )
                    else:
                        _sub_choice = (
                            _sub_cap_ok           # ① clean sub-window caption
                            or _text_speaker      # ② fuzzy text match
                            or speaker            # ③ chunk-level fallback
                            or sub.get("speaker") # ④ DOM split speaker
                        )
                    sub_speaker = normalize_speaker_name(_sub_choice, self._alias_map)

                    # 1e: try to match a pending caption to produce a merged row
                    matched_cap = None
                    for i, cap in enumerate(self._cap_pending):
                        if cap["abs_start"] is None or cap["abs_end"] is None:
                            continue
                        overlap = (
                            min(cap["abs_end"], sub["end_time"])
                            - max(cap["abs_start"], sub["start_time"])
                        )
                        if overlap > 0.5:
                            matched_cap = self._cap_pending.pop(i)
                            break

                    if matched_cap:
                        # Merge: clean sub-window caption → already-resolved sub_speaker
                        # (which now respects measurement/locks) → pending cap name.
                        # Never let an AMBIGUOUS caption pick win here either.
                        merge_speaker = _sub_cap_ok or sub_speaker or _text_speaker or matched_cap["speaker"]
                        try:
                            await self.client.add_transcript(
                                meeting_id=self.meeting_id,
                                text=sub_text,
                                speaker_name=merge_speaker,
                                source="merged",
                                start_time=round(sub["start_time"], 2),
                                end_time=round(sub["end_time"], 2),
                                language_code=lang,
                                speaker_source=_attr_source,
                                speaker_confidence=_attr_confidence,
                                alignment_overlap_ms=_attr_overlap_ms,
                                is_overlapping_speech=_attr_is_overlapping,
                                secondary_speakers_json=_attr_secondary,
                                confidence=_confidence_label,
                            )
                            logger.info(
                                "STT [merged] speaker=%s conf=%s: %s",
                                merge_speaker, _confidence_label, sub_text[:80],
                            )
                        except Exception as exc:
                            logger.error("Failed to save merged STT: %s", exc)
                    else:
                        # No matching caption — insert as standalone STT row
                        try:
                            await self.client.add_transcript(
                                meeting_id=self.meeting_id,
                                text=sub_text,
                                speaker_name=sub_speaker,
                                source="sarvam_stt",
                                start_time=round(sub["start_time"], 2),
                                end_time=round(sub["end_time"], 2),
                                language_code=lang,
                                speaker_source=_attr_source,
                                speaker_confidence=_attr_confidence,
                                alignment_overlap_ms=_attr_overlap_ms,
                                is_overlapping_speech=_attr_is_overlapping,
                                secondary_speakers_json=_attr_secondary,
                                confidence=_confidence_label,
                            )
                            logger.info(
                                "STT [sarvam_stt] speaker=%s conf=%s: %s",
                                sub_speaker, _confidence_label, sub_text[:80],
                            )
                        except Exception as exc:
                            logger.error("Failed to save WebRTC STT: %s", exc)

        except Exception as exc:
            logger.error("WebRTC STT loop error: %s", exc, exc_info=True)
        finally:
            webrtc.stop()
            if webrtc_got_data:
                logger.info("WebRTC STT loop ended (captured data)")
            else:
                logger.warning("WebRTC STT loop ended with no data")

    async def _sounddevice_stt_loop(self) -> None:
        """PulseAudio capture — records ALL participants' audio from MeetingSink.monitor.
        Runs in parallel with WebRTC to catch ALL speakers via PulseAudio.
        Uses ffmpeg (not sounddevice) so PulseAudio source names work correctly.
        Industry standard: Recall.ai, Fireflies all use ffmpeg for audio capture."""
        import shutil as _shutil
        import io as _io
        import wave as _wave

        # MUST be MeetingSink.monitor — that is Chrome's audio output sink's monitor.
        # "default" resolves to SilentMic.monitor (the silent bot mic), not speech audio.
        pulse_source = os.getenv("PULSE_SOURCE", "MeetingSink.monitor")
        chunk_sec = 15  # seconds per STT chunk

        if not _shutil.which("ffmpeg"):
            logger.error("ffmpeg not found — PulseAudio STT disabled")
            return

        logger.info("PulseAudio STT: starting ffmpeg capture from source=%s", pulse_source)

        # ── Pre-flight: verify ffmpeg can open the PulseAudio source ─────────
        try:
            _probe = await asyncio.create_subprocess_exec(
                "ffmpeg", "-f", "pulse", "-i", pulse_source, "-t", "0.1",
                "-f", "null", "-",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, _probe_err = await asyncio.wait_for(_probe.communicate(), timeout=5)
            _probe_text = _probe_err.decode(errors="replace")
            if _probe.returncode == 0 or "size=" in _probe_text or "audio" in _probe_text.lower():
                logger.info("[diag] PulseAudio preflight OK — source '%s' is accessible", pulse_source)
            else:
                logger.warning("[diag] PulseAudio preflight FAILED for source '%s' — error: %s",
                               pulse_source, _probe_text[:300])
        except Exception as _pe:
            logger.warning("[diag] PulseAudio preflight exception: %s", _pe)

        _silent_chunk_count = 0
        _audio_chunk_count = 0

        while self._running:
            if self.meeting_id is None or self.meeting_start_time is None:
                await asyncio.sleep(1)
                continue

            abs_start = time.time() - self.meeting_start_time

            try:
                # ffmpeg captures chunk_sec seconds from PulseAudio → 16kHz mono WAV
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y",
                    "-f", "pulse",
                    "-i", pulse_source,
                    "-t", str(chunk_sec),
                    "-ar", "16000",
                    "-ac", "1",
                    "-f", "wav",
                    "pipe:1",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                wav_bytes, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=chunk_sec + 10
                )
            except asyncio.TimeoutError:
                logger.warning("PulseAudio STT: ffmpeg capture timed out")
                continue
            except Exception as exc:
                logger.error("PulseAudio STT: ffmpeg error: %s", exc)
                await asyncio.sleep(2)
                continue

            chunk_kb = len(wav_bytes) / 1024 if wav_bytes else 0
            # WAV header = 44 bytes. A 15s silent WAV at 16kHz = ~480KB.
            # < 10KB almost always means PulseAudio got no audio from Chrome.
            if not wav_bytes or len(wav_bytes) < 4096:
                _silent_chunk_count += 1
                if _silent_chunk_count % 3 == 1:
                    logger.warning(
                        "[diag] PulseAudio chunk #%d too small (%.1fKB) — "
                        "Chrome may not be routing audio to MeetingSink. "
                        "Check: HEADLESS=%s, DISPLAY=%s, --use-fake-device in Chrome args?",
                        _silent_chunk_count, chunk_kb, HEADLESS, os.environ.get("DISPLAY", "not_set")
                    )
                continue

            _audio_chunk_count += 1
            logger.info("[diag] PulseAudio chunk %.1fKB (silent_so_far=%d, audio_chunks=%d)",
                        chunk_kb, _silent_chunk_count, _audio_chunk_count)

            abs_end = time.time() - self.meeting_start_time

            result = await self.sarvam.transcribe(wav_bytes)
            if not result:
                continue

            transcript = result.get("transcript", "").strip()
            if not transcript or _is_garbage(transcript):
                continue

            lang = result.get("language_code", "unknown")

            # ── Speaker attribution for PulseAudio (all-speaker audio) ──────
            # Priority: caption overlap → last caption speaker → active DOM → single participant
            _p_attr_source: Optional[str] = None
            _p_attr_confidence: Optional[float] = None
            _p_attr_overlap_ms: Optional[int] = None
            _p_is_overlapping: Optional[bool] = None
            _p_secondary: Optional[list] = None

            speaker: Optional[str] = None
            cap_result = self._resolve_speaker_from_captions(abs_start, abs_end)
            if cap_result["speaker"]:
                speaker = cap_result["speaker"]
                _p_attr_source = cap_result["source"]
                _p_attr_confidence = cap_result["confidence"]
                _p_attr_overlap_ms = cap_result["overlap_ms"]
                _p_is_overlapping = cap_result["is_overlapping"]
                _p_secondary = cap_result["secondary"] or None

            # P1.5: last caption speaker — more reliable than DOM names
            if not speaker and self._last_active_speaker:
                _last_cap_time = (
                    self._caption_timeline[-1]["end_time"]
                    if self._caption_timeline else 0.0
                )
                if abs_start - _last_cap_time < 8.0:
                    speaker = normalize_speaker_name(
                        self._last_active_speaker, self._alias_map
                    )
                    _p_attr_source = "last_caption"
                    logger.info("STT [pulse P1.5-last-caption] %s", speaker)

            # P1.75: text similarity match — fuzzy match STT text against caption history
            if not speaker:
                _txt_spk = self._find_speaker_by_text(transcript)
                if _txt_spk:
                    speaker = normalize_speaker_name(_txt_spk, self._alias_map)
                    _p_attr_source = "text_match"

            if not speaker and self._active_speaker_timeline:
                _votes: Dict[str, float] = {}
                _prev_time: Optional[float] = None
                _prev_name: Optional[str] = None
                for entry in self._active_speaker_timeline:
                    t = entry["abs_time"]
                    n = entry["speaker"]
                    if abs_start - 15.0 <= t <= abs_end + 2.0:
                        duration = (t - _prev_time) if (_prev_time and _prev_name == n) else 0.5
                        _votes[n] = _votes.get(n, 0) + duration
                    _prev_time = t
                    _prev_name = n
                if _votes:
                    speaker = max(_votes, key=lambda k: _votes[k])
                    _p_attr_source = "active_dom"

            if not speaker:
                speaker = self._last_active_speaker
                if speaker:
                    _p_attr_source = "last_active"

            if not speaker:
                bot_name = os.getenv("BOT_DISPLAY_NAME", "OneClarity AI")
                real = [p for p in self._known_participants if p != bot_name]
                if len(real) == 1:
                    speaker = real[0]
                    _p_attr_source = "single_participant"

            logger.info(
                "STT [pulse] meeting=%s t=[%.1f,%.1f] lang=%s speaker=%s source=%s: %s",
                self.meeting_id, abs_start, abs_end, lang,
                speaker or "Unknown", _p_attr_source or "unknown", transcript[:80],
            )
            try:
                await self.client.add_transcript(
                    meeting_id=self.meeting_id,
                    text=transcript,
                    speaker_name=speaker,
                    source="sarvam_stt",
                    start_time=round(abs_start, 2),
                    end_time=round(abs_end, 2),
                    speaker_source=_p_attr_source,
                    speaker_confidence=_p_attr_confidence,
                    alignment_overlap_ms=_p_attr_overlap_ms,
                    language_code=lang,
                    is_overlapping_speech=_p_is_overlapping,
                    secondary_speakers_json=_p_secondary,
                )
            except Exception as exc:
                logger.error("Failed to save PulseAudio STT: %s", exc)

        logger.info("PulseAudio STT loop ended")

    # ── Caption scraping ──────────────────────────────────────────────────────
    #
    # Primary: JS evaluation (resilient to Meet CSS class changes).
    # Fallback: CSS selector loop (legacy).
    # Maintains _caption_timeline for STT speaker resolution strategies 1 & 2.

    CAPTION_SELECTORS = [
        ".a4cQT", ".iTTPOb", "[jsname='YSxPC']", ".TBMuR",
        ".iOzk7", ".CNusmb", "[jsname='tgaKEf']",
    ]

    # JS that tries every known caption container pattern and returns raw text blocks
    _CAPTION_JS = """
    () => {
        const SELECTORS = [
            '.a4cQT',
            '.iTTPOb',
            '[jsname="YSxPC"]',
            '.TBMuR',
            '[jscontroller="kAPMuc"] > div',
            '.iOzk7',
            '.CNusmb',
            '[jsname="tgaKEf"]',
            '[aria-live="polite"] > div',
            '[aria-live="assertive"] > div',
        ];
        for (const sel of SELECTORS) {
            const els = [...document.querySelectorAll(sel)];
            if (!els.length) continue;
            const texts = els
                .map(e => (e.innerText || e.textContent || '').trim())
                .filter(t => t.length > 2);
            if (texts.length) return {selector: sel, texts};
        }
        return null;
    }
    """

    async def _scrape_captions(self) -> bool:
        """
        Scrape captions via 3-tier fallback:
          1. MutationObserver buffer (real-time, ~16ms lag, catches rapid speaker switches)
          2. JS DOM scan (resilient to CSS class renames)
          3. CSS selector loop (legacy fallback)
        Returns True if any captions were found this tick.
        """
        current: Dict[str, str] = {}
        matched_selector: Optional[str] = None

        # ── Tier 1: MutationObserver buffer ───────────────────────────────────
        # Drains all caption events buffered since last tick.
        # Multiple rapid speaker changes are all captured here.
        try:
            entries = await self.page.evaluate(
                "() => { var b = window.__captionBuffer || []; "
                "window.__captionBuffer = []; return b; }"
            )
            if entries:
                matched_selector = "mutation_observer"
                for entry in entries:
                    speaker = (entry.get("speaker") or "Unknown").strip()
                    text = (entry.get("text") or "").strip()
                    if not text or _is_garbage(text):
                        continue
                    if _is_garbage(speaker):
                        speaker = "Unknown"
                    # Keep the longest text seen for each speaker in this tick
                    if speaker not in current or len(text) > len(current[speaker]):
                        current[speaker] = text
        except Exception as exc:
            logger.debug("Caption observer drain failed: %s", exc)

        # ── Tier 2: JS DOM scan (when observer not yet attached or page just loaded) ─
        if not current:
            try:
                js_result = await self.page.evaluate(self._CAPTION_JS)
                if js_result:
                    matched_selector = js_result["selector"]
                    for raw in js_result["texts"]:
                        raw = raw.strip()
                        if not raw or _is_garbage(raw):
                            continue
                        speaker, text = _split_speaker_text(raw)
                        if speaker and not _is_garbage(text):
                            if speaker not in current or len(text) > len(current[speaker]):
                                current[speaker] = text
                        else:
                            self._parse_multiline_block(raw, current)
            except Exception as exc:
                logger.debug("JS caption scan failed: %s", exc)

        # ── Tier 3: CSS selector loop (legacy) ────────────────────────────────
        if not current:
            for selector in self.CAPTION_SELECTORS:
                try:
                    els = await self.page.query_selector_all(selector)
                    if not els:
                        continue
                    raw_found = []
                    for el in els:
                        raw = (await el.inner_text()).strip()
                        if not raw:
                            continue
                        raw_found.append(raw)
                        if _is_garbage(raw):
                            continue
                        speaker, text = _split_speaker_text(raw)
                        if speaker and not _is_garbage(text):
                            if speaker not in current or len(text) > len(current[speaker]):
                                current[speaker] = text
                        else:
                            self._parse_multiline_block(raw, current)
                    if raw_found:
                        matched_selector = selector
                    if current:
                        break
                except Exception as exc:
                    logger.debug("Caption selector %r error: %s", selector, exc)

        if current:
            logger.info(
                "Captions [tier=%s] — %d speakers: %s",
                matched_selector, len(current),
                {s: t[:40] for s, t in current.items()},
            )

        if not current:
            return False

        # Track active speaker for non-diarized STT fallback
        self._last_active_speaker = max(current, key=lambda s: len(current[s]))

        # ── Caption timeline with start/end times ─────────────────────────────
        if self.meeting_start_time is not None:
            now = time.time() - self.meeting_start_time

            # Normalize and filter the current tick's speakers
            _bot_name_lc = os.getenv("BOT_DISPLAY_NAME", "OneClarity AI").lower()
            valid_current: Dict[str, str] = {}
            for spk, txt in current.items():
                spk = " ".join(spk.strip().split())  # normalize spaces
                if not spk or spk in ("Unknown", "You", "Me") or _is_garbage(txt):
                    continue
                # Never add the bot as a speaker — it uses a fake audio device
                if spk.lower() == _bot_name_lc:
                    continue
                txt = txt.strip()
                if not txt:
                    continue
                # Roster artifact: Meet briefly shows "Name: Name" at join.
                # text == speaker means it's a name label, not actual speech.
                if txt.lower() == spk.lower():
                    continue
                # Also reject if speaker looks like a UI element (not a real name)
                if not self._is_real_name(spk):
                    continue
                valid_current[spk] = txt

            # Update or create active caption entries.
            for spk, txt in valid_current.items():
                # Skip a frozen bubble we've already finalized — Meet leaves the
                # last caption on screen indefinitely; recreating it here would
                # re-finalize duplicates and keep a departed speaker "live".
                if self._last_finalized_caption.get(spk) == txt:
                    continue
                if spk not in self._active_captions:
                    self._active_captions[spk] = {
                        "start_time": now,
                        "end_time": now,
                        "text": txt,
                        "text_changed_at": now,   # for stale-utterance finalization
                    }
                else:
                    _prev = self._active_captions[spk]
                    _prev["end_time"] = now
                    if txt != _prev.get("text"):
                        _prev["text"] = txt
                        _prev["text_changed_at"] = now

            def _finalize(spk: str, entry: dict) -> None:
                entry["end_time"] = now
                self._caption_timeline.append({
                    "start_time": entry["start_time"],
                    "end_time": entry["end_time"],
                    "speaker": spk,
                    "text": entry["text"],
                })
                self._last_finalized_caption[spk] = entry["text"]

            # Finalize speakers who have disappeared from captions this tick.
            for spk in [s for s in self._active_captions if s not in valid_current]:
                _finalize(spk, self._active_captions.pop(spk))

            # Finalize STALE utterances: in continuous crosstalk, two speakers'
            # caption bubbles can stay on screen for minutes and never "disappear",
            # so the block above never fires and _caption_timeline stays empty.
            # When a speaker's caption text has been unchanged for a few seconds the
            # utterance is finished — flush it once and remove the active window.
            # _last_finalized_caption stops the frozen bubble from re-finalizing.
            for spk in list(self._active_captions):
                entry = self._active_captions[spk]
                if (now - entry.get("text_changed_at", now)) >= _CAPTION_STALE_FINALIZE_SEC:
                    _finalize(spk, entry)
                    self._active_captions.pop(spk, None)

            if len(self._caption_timeline) > _TIMELINE_MAX:
                self._caption_timeline = self._caption_timeline[-_TIMELINE_MAX:]

        # Feed into finaliser — saves when speaker stops / sentence ends
        finalised = self.caption_buf.update(current)
        for speaker, text in finalised:
            try:
                abs_time = (
                    time.time() - self.meeting_start_time
                    if self.meeting_start_time else None
                )
                abs_end = abs_time
                abs_start = max(0.0, abs_time - 3.0) if abs_time is not None else None
                # 1d: normalise speaker name via alias map
                norm_speaker = normalize_speaker_name(
                    speaker if speaker != "Unknown" else None,
                    self._alias_map,
                )
                # 1e: buffer caption for potential STT merge; stale ones flush to DB.
                # Guard: skip if same speaker+fingerprint is already waiting (dual-tier
                # or rapid-pause duplicates that slipped past CaptionFinaliser).
                _cap_fp = " ".join(text.lower().split())[:80]
                if not any(
                    " ".join(c["text"].lower().split())[:80] == _cap_fp
                    and c["speaker"] == norm_speaker
                    for c in self._cap_pending
                ):
                    self._cap_pending.append({
                        "speaker": norm_speaker,
                        "text": text,
                        "abs_start": abs_start,
                        "abs_end": abs_end,
                        "queued_at": time.time(),
                    })
                    logger.info(
                        "Caption buffered [selector=%s] [speaker=%s] [%.1fs]: %s",
                        matched_selector, norm_speaker,
                        abs_end if abs_end is not None else 0,
                        text[:120],
                    )
                else:
                    logger.debug(
                        "Caption dedup-skipped [speaker=%s]: %s",
                        norm_speaker, text[:80],
                    )
                # Flush captions that have waited > TTL with no STT match
                await self._flush_stale_captions()
            except Exception as exc:
                logger.error("Failed to buffer caption [%s]: %s — text=%r", speaker, exc, text[:60])

        return True

    @staticmethod
    def _parse_multiline_block(raw: str, current: Dict[str, str]) -> None:
        """Parse a multi-line caption block into {speaker: text} entries."""
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        i = 0
        while i < len(lines):
            line = lines[i]
            if _is_garbage(line):
                i += 1
                continue
            words = line.split()
            is_name = (
                1 <= len(words) <= 4
                and all(w[0].isupper() for w in words if w)
                and not any(c in line for c in ".,!?:;")
            )
            if is_name and i + 1 < len(lines):
                speaker = line
                text = lines[i + 1]
                if not _is_garbage(text):
                    if speaker not in current or len(text) > len(current[speaker]):
                        current[speaker] = text
                i += 2
            else:
                if not _is_garbage(line):
                    current.setdefault("Unknown", "")
                    if len(line) > len(current["Unknown"]):
                        current["Unknown"] = line
                i += 1

    # ── Participant scraping ───────────────────────────────────────────────────

    # Names that look like Meet internal UI strings, not real people
    _PARTICIPANT_BLOCKLIST = re.compile(
        r"^(frame_\w+|You|you|Meeting Bot|OneClarity AI|Bot|Unknown|presenter|"
        r"Connect|Connected|Disconnect|Disconnected|Connecting|Reconnecting|"
        r"tile_\w+|participant_\w+|[a-z][a-z0-9_]{4,})$"
    )

    def _is_real_name(self, name: str) -> bool:
        """Return True if name looks like an actual person/org name."""
        if not name or len(name) < 2 or len(name) > 80:
            return False
        if _is_garbage(name):
            return False
        if self._PARTICIPANT_BLOCKLIST.match(name):
            return False
        # Reject if name matches the meeting title (e.g. "testing", "Shubham / Vaibhav")
        if self._title and name.lower().strip() == self._title.lower().strip():
            return False
        # Reject aria-label action strings scraped verbatim, e.g.
        # "Rishabh is sharing their screen" / "Priya is presenting"
        _nl = name.lower()
        if " is " in _nl or " are " in _nl or " has " in _nl:
            return False
        # Must have at least one letter and start with uppercase or be ≥2 words
        words = name.split()
        has_upper_start = any(w and w[0].isupper() for w in words)
        return has_upper_start or len(words) >= 2

    async def _scrape_participants(self) -> None:
        # ── Strategy A: JS evaluate — broad net across current Meet DOM ──────
        # Google Meet frequently renames CSS classes; querying via JS with
        # multiple heuristics is more stable than CSS selector lists.
        names_from_js: list[str] = []
        try:
            names_from_js = await self.page.evaluate("""
                () => {
                    const names = new Set();

                    // 1. data-participant-id containers (tile layout, people panel)
                    document.querySelectorAll('[data-participant-id]').forEach(el => {
                        // Name chip overlaid on video tile
                        const chip = el.querySelector(
                            '[data-self-name],[jsname="EzdCnc"],[jsname="tgaKEf"],' +
                            '.zWfAFd,.KF4T6b,.cS7aqe,.dwAOEb,.NjCFcc'
                        );
                        if (chip) {
                            const t = (chip.innerText || chip.textContent || '').trim();
                            if (t) names.add(t);
                        }
                        // Fallback: first short text child of the container
                        else {
                            const t = (el.innerText || '').split('\\n')[0].trim();
                            if (t && t.length < 60) names.add(t);
                        }
                    });

                    // 2. People-panel list items (visible when panel is open)
                    document.querySelectorAll(
                        '[data-participant-id] [data-self-name],' +
                        '[role="listitem"] [jscontroller],' +
                        '.rua5Nb .zWfAFd, .rua5Nb .KF4T6b'
                    ).forEach(el => {
                        const t = (el.innerText || el.textContent || '').trim();
                        if (t) names.add(t);
                    });

                    // 3. aria-label on participant tiles (most stable — Google rarely removes these)
                    document.querySelectorAll('[data-participant-id][aria-label]').forEach(el => {
                        const label = el.getAttribute('aria-label') || '';
                        // Strip suffixes: "Name's video", "Name (presenter)", "Name is sharing…"
                        const name = label
                            .replace(/['']s\\s+(?:video|screen|audio|camera).*$/gi, '')
                            .replace(/\\s+(?:is|are|has)\\s+.*/gi, '')
                            .replace(/\\s*\\(.*\\).*$/g, '')
                            .trim();
                        if (name) names.add(name);
                    });

                    return Array.from(names);
                }
            """) or []
        except Exception as _je:
            logger.debug("JS participant scrape failed: %s", _je)

        # ── Strategy B: harvest from caption timeline ─────────────────────
        # Caption entries always have the real speaker name — use as ground truth.
        names_from_captions = [
            e["speaker"] for e in self._caption_timeline
            if e.get("speaker") and e["speaker"] != "Unknown"
        ]

        # A real name from the LIVE DOM/people-panel (Strategy A) is authoritative
        # proof someone is in the meeting RIGHT NOW. Caption-harvested names
        # (Strategy B) are not — a speaker may have already left. So drive the
        # "seen participants" flag + presence grace timer only from the live DOM,
        # which keeps a muted/silent attendee from tripping the "nobody joined"
        # early-leave timer in _poll_loop.
        _js_real = [n.strip() for n in names_from_js if self._is_real_name(n.strip())]
        if _js_real:
            if not self._has_seen_participants:
                self._has_seen_participants = True
                logger.info(
                    "Participant panel shows %s — marking meeting as joined "
                    "(presence confirmed without needing speech)", _js_real,
                )
            self._last_confirmed_presence_time = time.time()

        all_names = names_from_js + names_from_captions
        for name in all_names:
            name = name.strip()
            if not self._is_real_name(name):
                continue
            if name in self._known_participants:
                continue
            self._known_participants.append(name)
            # Rebuild alias map whenever a new participant is added (1d)
            self._alias_map = build_alias_map(self._known_participants)
            # Clear low-confidence speaker mappings so they re-evaluate
            stale = [lbl for lbl in self._speaker_map
                     if lbl not in self._speaker_map_confident]
            for lbl in stale:
                del self._speaker_map[lbl]
            if stale:
                logger.info("New participant %r — cleared %d low-conf mapping(s)",
                            name, len(stale))
            try:
                await self.client.add_participant(
                    meeting_id=self.meeting_id, name=name
                )
                logger.info("New participant saved: %s  (total known=%d)",
                            name, len(self._known_participants))
            except Exception as exc:
                logger.error("add_participant failed for %r: %s", name, exc)

    # ── Active speaker detection ───────────────────────────────────────────────

    async def _scrape_active_speaker(self) -> Optional[str]:
        """
        Detect who is currently speaking by checking the highlighted/active
        tile in Google Meet's DOM.

        Google Meet signals the active speaker via:
          1. [data-active-speaker="true"] on the tile container
          2. A border/glow CSS class on the tile (varies, detected via JS heuristic)
          3. aria-label containing "is speaking" on the tile or button
          4. The 'active-speaker-border' or similar highlight on the video element

        Returns the speaker's display name, or None if undetectable.
        """
        if not self.meeting_start_time:
            return None
        try:
            name = await self.page.evaluate("""
                () => {
                    const botName = (window.__botDisplayName || '').toLowerCase();
                    const meetingTitle = (window.__meetingTitle || '').toLowerCase();

                    function cleanName(n) {
                        return (n || '').trim().replace(/\\s+/g, ' ');
                    }

                    function isValidName(n) {
                        if (!n || n.length < 2 || n.length > 60) return false;
                        if (n.toLowerCase() === 'you') return false;
                        if (n.toLowerCase() === 'unknown') return false;
                        if (botName && n.toLowerCase().includes(botName)) return false;
                        if (meetingTitle && n.toLowerCase() === meetingTitle) return false;
                        if (n.includes('/')) return false;
                        // Must start with uppercase letter
                        if (!/^[A-Z]/.test(n)) return false;
                        return true;
                    }

                    function extractNameFromTile(tile) {
                        // Try data-display-name attribute
                        let n = tile.getAttribute('data-display-name');
                        if (n) return cleanName(n);

                        // Try aria-label (e.g. "Rishabh Tripathi's video")
                        const label = tile.getAttribute('aria-label') || '';
                        if (label) {
                            const m = label.match(/^([^']+?)(?:'s video|'s screen|\\s*\\(|$)/i);
                            if (m) return cleanName(m[1]);
                        }

                        // Try name chip / overlay inside the tile
                        const chip = tile.querySelector(
                            '[data-self-name],[jsname="EzdCnc"],[jsname="tgaKEf"],' +
                            '.zWfAFd,.KF4T6b,.cS7aqe,.dwAOEb,.NjCFcc'
                        );
                        if (chip) return cleanName(chip.innerText || chip.textContent);

                        // Try first short text node
                        const txt = (tile.innerText || '').split('\\n')[0].trim();
                        if (txt && txt.length < 50) return cleanName(txt);

                        return null;
                    }

                    // ── Strategy 1: data-active-speaker attribute ─────────────
                    const activeTiles = document.querySelectorAll(
                        '[data-active-speaker="true"],[data-is-speaking="true"]'
                    );
                    for (const tile of activeTiles) {
                        const n = extractNameFromTile(tile);
                        if (isValidName(n)) return n;
                        // Check parent/ancestor
                        const parent = tile.closest('[data-participant-id]');
                        if (parent) {
                            const pn = extractNameFromTile(parent);
                            if (isValidName(pn)) return pn;
                        }
                    }

                    // ── Strategy 2: aria-label "is speaking" ──────────────────
                    const speakingEls = document.querySelectorAll('[aria-label*="speaking" i]');
                    for (const el of speakingEls) {
                        const label = el.getAttribute('aria-label') || '';
                        const m = label.match(/^([^']+?)(?:'s| is speaking)/i);
                        if (m) {
                            const n = cleanName(m[1]);
                            if (isValidName(n)) return n;
                        }
                        // Also try the containing tile
                        const tile = el.closest('[data-participant-id]');
                        if (tile) {
                            const n = extractNameFromTile(tile);
                            if (isValidName(n)) return n;
                        }
                    }

                    // ── Strategy 3: DOM border/animation heuristic ────────────
                    // Active speaker tile usually has an animated border element
                    // Look for tiles with a visible audio-level indicator
                    const allTiles = document.querySelectorAll('[data-participant-id]');
                    for (const tile of allTiles) {
                        // Check for audio level bar / speaking indicator inside tile
                        const indicator = tile.querySelector(
                            '[class*="speaking"],[class*="active"],[class*="border"],[class*="audio"]'
                        );
                        if (indicator && getComputedStyle(indicator).opacity > 0.5) {
                            const n = extractNameFromTile(tile);
                            if (isValidName(n)) return n;
                        }
                    }

                    return null;
                }
            """)
            return name or None
        except Exception as exc:
            logger.debug("Active speaker scrape failed: %s", exc)
            return None

    async def _participant_poller(self) -> None:
        """Poll participant panel every 30s throughout the meeting.
        Ensures all attendees — including those who never unmute — are saved
        to the DB so they appear in the summary participant list."""
        logger.info("Participant poller started (30s interval)")
        while self._running:
            try:
                if self.meeting_id is not None:
                    await self._scrape_participants()
            except Exception as exc:
                logger.debug("Participant poller error: %s", exc)
            await asyncio.sleep(30)

    async def _active_speaker_loop(self) -> None:
        """Poll DOM every 500ms for the highlighted active speaker tile.
        Stores results in _active_speaker_timeline for STT attribution."""
        logger.info("Active speaker loop started")
        # Inject bot name + meeting title into page for JS validation
        try:
            await self.page.evaluate(f"""
                window.__botDisplayName = {repr(os.getenv('BOT_DISPLAY_NAME', 'OneClarity AI'))};
                window.__meetingTitle = {repr(self._title or '')};
            """)
        except Exception:
            pass

        while self._running:
            try:
                name = await self._scrape_active_speaker()
                if name and self.meeting_start_time:
                    abs_time = time.time() - self.meeting_start_time
                    # Only append if speaker changed or >2s since last entry
                    if (not self._active_speaker_timeline
                            or self._active_speaker_timeline[-1]["speaker"] != name
                            or abs_time - self._active_speaker_timeline[-1]["abs_time"] > 2.0):
                        self._active_speaker_timeline.append(
                            {"abs_time": abs_time, "speaker": name}
                        )
                        logger.debug("Active speaker: %s @ %.1fs", name, abs_time)
                    # Keep rolling window (last 5 min = 600 entries at 500ms)
                    if len(self._active_speaker_timeline) > 600:
                        self._active_speaker_timeline = self._active_speaker_timeline[-600:]
            except Exception as exc:
                logger.debug("Active speaker loop error: %s", exc)
            await asyncio.sleep(0.5)

    # ── Teardown ──────────────────────────────────────────────────────────────

    async def _teardown(self) -> None:
        logger.info("=" * 60)
        logger.info("TEARDOWN STARTED — meeting_id=%s code=%s", self.meeting_id, self.meet_code)
        self._running = False
        self.audio.stop()
        logger.info("Teardown: audio stopped")

        if self.meeting_id is not None:
            # 1e: flush any captions still waiting for a STT match
            if self._cap_pending:
                logger.info(
                    "Teardown: flushing %d unmatched pending caption(s) …",
                    len(self._cap_pending),
                )
                await self._flush_stale_captions(flush_all=True)

            pending = self.caption_buf.flush()
            logger.info("Teardown: flushing %d pending caption(s) …", len(pending))
            for speaker, text in pending:
                if _is_garbage(text):
                    logger.debug("Teardown: skipping garbage caption [%s]: %s", speaker, text[:60])
                    continue
                try:
                    abs_time = (
                        time.time() - self.meeting_start_time
                        if self.meeting_start_time else None
                    )
                    await self.client.add_transcript(
                        meeting_id=self.meeting_id, text=text,
                        speaker_name=speaker if speaker != "Unknown" else None,
                        start_time=round(abs_time, 2) if abs_time is not None else None,
                        end_time=round(abs_time + 2.0, 2) if abs_time is not None else None,
                    )
                    logger.info("Teardown: flushed caption [%s]: %s", speaker, text[:80])
                except Exception as exc:
                    logger.error("Teardown: failed to flush caption: %s", exc)

        if self.meeting_id is not None:
            logger.info("Teardown: ending meeting in backend …")
            try:
                await self.client.end_meeting(self.meeting_id)
                logger.info("Teardown: meeting ended in backend")
            except Exception as exc:
                logger.error("Teardown: end_meeting FAILED: %s", exc, exc_info=True)
            _organizer = self._organizer_email or "(not set)"
            logger.info(
                "Teardown: requesting AI summary + email → organizer=%s meeting_id=%d",
                _organizer, self.meeting_id,
            )
            if not self._organizer_email:
                logger.warning(
                    "Teardown: organizer_email is EMPTY for meeting_id=%d — "
                    "summary will be generated but NO email will be sent. "
                    "Pass organizer_email in the /bot/join request to enable email.",
                    self.meeting_id,
                )

            _summary_ok = False
            logger.info("Teardown: waiting 90s for in-flight STT saves to complete before summarizing …")
            await asyncio.sleep(90)
            for _attempt in range(1, 4):
                try:
                    await self.client.summarize_meeting(self.meeting_id, attendee_emails=self._attendee_emails)
                    logger.info(
                        "Teardown: ✅ summary generated (attempt %d) — "
                        "email dispatched to %s",
                        _attempt, _organizer,
                    )
                    _summary_ok = True
                    break
                except ValueError as exc:
                    # 400 = no transcript — not retryable
                    if "no_transcript" in str(exc):
                        logger.warning(
                            "Teardown: meeting_id=%d had no transcript (bot was alone or nobody spoke) — "
                            "skipping summary and email",
                            self.meeting_id,
                        )
                        _summary_ok = True  # not an error — just nothing to summarize
                        break
                    logger.error("Teardown: summarize attempt %d/3 FAILED: %s", _attempt, exc)
                    if _attempt < 3:
                        await asyncio.sleep(5)
                except Exception as exc:
                    logger.error("Teardown: summarize attempt %d/3 FAILED: %s", _attempt, exc)
                    if _attempt < 3:
                        await asyncio.sleep(5)
            if not _summary_ok:
                logger.error(
                    "Teardown: ❌ summary FAILED after 3 attempts for meeting_id=%d. "
                    "Transcript is saved; regenerate via POST /meetings/%d/summarize",
                    self.meeting_id, self.meeting_id,
                )

            # Resolve title before push — try page title, then fallback to meet_code
            if not self._title:
                try:
                    _page_title = await self._safe_title()
                    # Reject generic browser/Meet titles
                    if (_page_title
                            and "meet.google" not in _page_title.lower()
                            and "google meet" not in _page_title.lower()
                            and len(_page_title.strip()) > 3):
                        self._title = _page_title.strip()
                    else:
                        self._title = f"Meeting {self.meet_code}"
                except Exception:
                    self._title = f"Meeting {self.meet_code}"
                try:
                    await self.client.update_meeting_title(self.meeting_id, self._title)
                    logger.info("Teardown: title resolved → %r for meeting_id=%d", self._title, self.meeting_id)
                except Exception as _te:
                    logger.warning("Teardown: could not save resolved title: %s", _te)

            # Close out any hand still raised when the meeting ended (never lowered).
            if self._hand_raise_state:
                _end_offset = (
                    round(time.time() - self.meeting_start_time, 2)
                    if self.meeting_start_time else None
                )
                for _name, _raised_at in self._hand_raise_state.items():
                    self._hand_raise_events.append(
                        {"speaker": _name, "raised_at": _raised_at, "lowered_at": _end_offset}
                    )
                self._hand_raise_state.clear()

            # Upsert 1-row summary into ai_meeting_master (runs after summary is ready)
            try:
                await self.client.push_meeting_summary(
                    self.meeting_id,
                    hand_raises=self._hand_raise_events or None,
                    chat_messages=self._chat_messages or None,
                )
                logger.info(
                    "Teardown: ✅ ai_meeting_master upserted for meeting_id=%d "
                    "(%d hand-raises, %d chat messages)",
                    self.meeting_id, len(self._hand_raise_events), len(self._chat_messages),
                )
            except Exception as _exc:
                logger.error("Teardown: push_meeting_summary FAILED for meeting_id=%d: %s", self.meeting_id, _exc)
        else:
            logger.warning("Teardown: meeting_id is None — skipping end_meeting and summary")

        await self.client.close()
        if self.context:
            try:
                await self.context.close()
                logger.info("Teardown: browser context closed")
            except Exception as exc:
                logger.error("Teardown: failed to close context: %s", exc)

        if self._browser:
            try:
                await self._browser.close()
                logger.info("Teardown: browser closed")
            except Exception as exc:
                logger.error("Teardown: failed to close browser: %s", exc)
            self._browser = None

        logger.info("TEARDOWN COMPLETE — bot for %s has stopped", self.meet_code)
        logger.info("=" * 60)

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _try_click(self, selectors: list[str], timeout: int = 3000) -> bool:
        for selector in selectors:
            try:
                el = await self.page.wait_for_selector(selector, timeout=timeout)
                if el:
                    await el.click()
                    return True
            except PWTimeout:
                continue
            except Exception:
                continue
        return False

    async def _is_page_alive(self) -> bool:
        """Return True if the browser page is still open and responsive."""
        try:
            await self.page.evaluate("1")
            return True
        except Exception:
            return False

    async def _safe_title(self) -> str:
        """Return page title without raising if page is closed."""
        try:
            return await self.page.title()
        except Exception:
            return "<page closed>"

    async def _take_debug_screenshot(self, label: str) -> None:
        """Save a screenshot to uploads/ for debugging."""
        try:
            ss_dir = Path(__file__).parent.parent.parent / "uploads"
            ss_dir.mkdir(exist_ok=True)
            ss_path = ss_dir / f"bot_{label}_{self.meet_code}.png"
            await self.page.screenshot(path=str(ss_path), full_page=False)
            logger.info("Screenshot saved → %s", ss_path)
        except Exception as exc:
            logger.warning("Screenshot failed: %s", exc)

    async def _detect_meet_page_state(self) -> str:
        """
        Detect what Google Meet is actually showing after navigation.
        Uses visible element checks (NOT page-source search) to avoid false positives
        from text that appears inside JS bundles or hidden DOM nodes.

        Returns: 'lobby' | 'in_meeting' | 'meeting_not_found' | 'meeting_ended' |
                 'security_check' | 'page_closed' | 'off_meet' | 'unknown'
        """
        if not await self._is_page_alive():
            return "page_closed"

        url = self.page.url
        if "meet.google.com" not in url:
            return "off_meet"

        try:
            # ── Already inside the meeting ────────────────────────────────────
            for sel in ['button[aria-label*="Leave call" i]', '[jsname="CQylAd"]']:
                try:
                    el = await self.page.query_selector(sel)
                    if el and await el.is_visible():
                        return "in_meeting"
                except Exception:
                    continue

            # ── Login required — Meet shows "Sign in" overlay without redirecting ─
            for sel in [
                'button:has-text("Sign in")',
                'a:has-text("Sign in")',
                '[aria-label*="Sign in" i]',
            ]:
                try:
                    el = await self.page.query_selector(sel)
                    if el and await el.is_visible():
                        return "login_required"
                except Exception:
                    continue

            # ── Error/end states — check BEFORE lobby to avoid jsname false-positives.
            # Google reuses internal jsname attributes (e.g. V67aGc) across pages,
            # so the error page's "Return to home screen" button can wrongly match
            # lobby selectors if we check lobby first.
            try:
                body_text = await self.page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )
                body_lower = body_text.lower()
                if any(t in body_lower for t in [
                    "meeting has ended", "you've left the meeting", "meeting is over",
                    "return to home screen", "returning to home screen",
                ]):
                    return "meeting_ended"
                if any(t in body_lower for t in [
                    "no matching meeting", "check your meeting code", "can't find your meeting",
                    "you can't join this video call", "can't join this video call",
                    "no one can join a meeting unless invited",
                ]):
                    return "meeting_not_found"
                if any(t in body_lower for t in ["verify it's you", "unusual activity", "confirm it's you"]):
                    return "security_check"
            except Exception:
                pass

            # ── Lobby — has a join button OR a name-entry field ───────────────
            lobby_selectors = [
                'button:has-text("Join now")',
                'button:has-text("Ask to join")',
                '[jsname="Qx7uuf"]',
                '[jsname="V67aGc"]',
                'input[aria-label*="name" i]',
                'input[placeholder*="name" i]',
            ]
            for sel in lobby_selectors:
                try:
                    el = await self.page.query_selector(sel)
                    if el and await el.is_visible():
                        return "lobby"
                except Exception:
                    continue

            # Default: if we're on meet.google.com, assume lobby is still loading
            return "lobby"

        except Exception:
            return "unknown"

    @staticmethod
    def _extract_code(url: str) -> str:
        return url.rstrip("/").split("/")[-1].split("?")[0]


# ── Speaker/text splitter ─────────────────────────────────────────────────────

_SPEAKER_LINE_RE = re.compile(
    r"^([A-Z][A-Za-z\s.\-']{1,49}):\s+(.+)$", re.DOTALL
)


def _split_speaker_text(raw: str) -> tuple[Optional[str], str]:
    """Split 'Speaker: text' or return (None, raw)."""
    raw = raw.strip()
    m = _SPEAKER_LINE_RE.match(raw)
    if m and len(m.group(1).split()) <= 5:
        return m.group(1).strip(), m.group(2).strip()
    return None, raw
