# Architecture

## Table of Contents

1. [System Overview](#system-overview)
2. [High-Level Architecture](#high-level-architecture)
3. [End-to-End Flow](#end-to-end-flow)
4. [Component Reference](#component-reference)
5. [Database Schema](#database-schema)
6. [API Reference](#api-reference)
7. [Speaker Attribution Pipeline](#speaker-attribution-pipeline)
8. [Environment Variables](#environment-variables)
9. [Known Limitations & Roadmap](#known-limitations--roadmap)

---

## System Overview

This bot:

- Joins a Google Meet call automatically (Playwright + Chromium)
- Transcribes speech in real time (Google's live captions, plus optional Sarvam AI
  speech-to-text for better multilingual accuracy)
- Attributes speech to the correct speaker name
- Generates an AI meeting summary via Azure OpenAI
- Optionally emails the summary to the organizer and attendees (AWS SES)
- Streams the live transcript to a web dashboard over Server-Sent Events (SSE)

**Tech stack**

| Layer | Technology |
|---|---|
| Bot browser | Playwright + Chromium |
| Speech-to-text | Sarvam AI (`saaras:v3` by default, configurable) |
| AI summary | Azure OpenAI |
| Backend | FastAPI (async Python) |
| Database | MySQL (SQLAlchemy async ORM), SQLite fallback for local dev |
| Email | AWS SES |
| Dashboard | Streamlit (`streamlit_app/`) + a static HTML frontend served by the backend |
| Deployment | Docker (multi-stage build), Docker Compose for local/self-hosted stacks |

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                    CLIENT (browser / Streamlit)                     │
│  Paste Meet link → Send Bot   │   Live transcript (SSE)   │  History │
└─────────┼──────────────────────────────┼─────────────────────────────┘
          │ POST /bot/join               │ GET /meetings/{id}/transcript/live (SSE)
          ▼                              │
┌─────────────────────────────────────────────────────────────────────┐
│                     FASTAPI BACKEND  (main.py, port 6050)            │
│                                                                       │
│  core/bot_manager.py   API/routes.py           helpers/summarizer.py │
│  - launches bots       - /meetings, /bot/*      - Azure OpenAI       │
│  - max N concurrent    - /transcript, /calendar - chunk + summarize  │
│  - crash watchdog                               - triggers email    │
└─────────┼─────────────────────────────────────────────────────────────┘
          │ asyncio.create_task(MeetBot.run())
          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                bot/bots/meet_bot.py  (Playwright, headless Chromium) │
│                                                                       │
│  Caption scraper (DOM MutationObserver)   WebRTC audio interceptor   │
│         │                                          │                │
│         ▼                                          ▼                │
│  CaptionFinaliser                          Sarvam STT (saaras:v3)    │
│  (buffers incremental caption text)        (5s audio chunks → text) │
│         │                                          │                │
│         └──────────────┬───────────────────────────┘                │
│                         ▼                                           │
│              Speaker Resolution (4-strategy — see below)            │
└─────────────────────────┼─────────────────────────────────────────────┘
                           │ POST /meetings/{id}/transcript
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                            MySQL DATABASE                            │
│   ai_gm_meetings │ ai_transcript_segments │ ai_participants │        │
│   audit_logs │ ai_meeting_master (denormalized reporting table)      │
└─────────────────────────┼─────────────────────────────────────────────┘
                           │ on meeting end
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     SUMMARY + EMAIL PIPELINE                         │
│  Fetch segments → dedupe → speaker stats → Azure OpenAI summary →    │
│  save to meeting row → AWS SES email (organizer TO, attendees BCC)   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## End-to-End Flow

### 1 — A bot is requested

Either a user pastes a Meet URL and clicks "Send Bot" in the dashboard, an admin calls
`POST /bot/join` directly (optionally through the Google Meet [Add-on](google-meet-addon.md)),
or the calendar auto-join loop in `main.py` detects an upcoming meeting on the bot
account's calendar.

```
POST /bot/join  { meet_url, title, organizer_email, attendee_emails }
→ 202 Accepted  { meet_code, status: "launching" }
```

### 2 — `bot_manager` launches the bot

`core/bot_manager.py` checks the bot is authenticated (or guest mode is enabled),
checks current concurrency against `BOT_MAX_CONCURRENT`, and schedules
`MeetBot.run()` as an asyncio task. A watchdog restarts a bot that crashes after
running for more than 30 seconds (up to 2 restarts).

### 3 — The bot joins the meeting

`bot/bots/meet_bot.py` launches Chromium, injects stealth/caption/audio-capture
JavaScript, navigates to the Meet URL, resolves the Google auth flow (saved session,
guest join, or email/password login), dismisses consent dialogs, mutes mic/camera, and
clicks "Ask to join" / "Join now". Once in the meeting it calls `POST /meetings/start`
to create the DB row and begins its capture loops.

### 4 — Real-time transcription loops

Running concurrently inside the meeting:

- **Caption scraper** (~every second): a DOM `MutationObserver` captures Google's live
  captions into a JS buffer; Python drains it, buffers per-speaker text via
  `CaptionFinaliser`, and finalizes a segment once a speaker stops talking. Posted with
  `source: "caption"`.
- **WebRTC audio capture** (~every 5s, configurable via `AUDIO_CHUNK_SEC`): a
  `MediaRecorder` hooked into `RTCPeerConnection` records audio chunks, which are
  decoded, converted to WAV, and sent to Sarvam STT. Posted with `source: "stt"` (or the
  provider-specific value used by your STT client).
- **Active-speaker DOM poll** (~every 500ms): tracks who the DOM currently marks as
  speaking, feeding the speaker resolution logic below.
- **Participant monitor** (~every second): scrapes the participant list and upserts it
  via `POST /meetings/{id}/participants`.

### 5 — Live transcript streaming

The frontend opens `GET /meetings/{id}/transcript/live` (SSE). The backend polls the DB
for new segments roughly once a second, streams a `transcript` event per new segment, a
periodic `ping` keep-alive, and a `meeting_ended` event once the meeting's status flips
to `completed`.

### 6 — Meeting end detection

The bot detects the meeting ending via the "Meeting ended" banner, everyone having left
for several consecutive checks, or repeated network failures. It flushes any pending
captions, then calls `PATCH /meetings/{id}/end`.

### 7 — Summary generation

Triggered manually (`POST /meetings/{id}/summarize`) or by the bot on teardown. The
backend fetches all segments, deduplicates caption/STT overlap, computes rough
speaker talk-time stats, builds a length-appropriate prompt (shorter meetings get a
shorter summary), calls Azure OpenAI, saves the result to the meeting row, and — if AWS
SES is configured — emails it to the organizer (TO) and attendees (BCC).

Optionally, `POST /meetings/{id}/push-summary` denormalizes the finished meeting
(transcript, hand-raise events, chat messages) into the `ai_meeting_master` reporting
table — see [docs/database.md](database.md).

---

## Component Reference

| Path | Role |
|---|---|
| `main.py` | FastAPI app entry point. Lifespan hook restores the bot's saved session from `BOT_SESSION_B64`, cleans up stale meeting rows left over from a crash, starts the startup-login task and the calendar auto-join loop, and mounts the static frontend |
| `API/routes.py` | All REST + SSE routes. Optional `X-API-Key` enforcement via `BOT_API_KEY`, in-memory rate limiting, meeting title sanitization (strips emails from calendar-sourced titles) |
| `API/models.py` | Pydantic request/response models |
| `core/bot_manager.py` | Bot lifecycle: concurrency limits, crash watchdog, `join()` / `stop()` |
| `core/database.py` | SQLAlchemy async engine + ORM models. MySQL primary, SQLite fallback; `REQUIRE_MYSQL=true` disables the silent fallback in production |
| `core/login_session.py` | Backs the Streamlit "Google Login" remote screenshot-driven flow for headless/Docker environments with no display |
| `core/startup_login.py` | Runs the bot's Google login flow once at startup if needed |
| `core/logger.py` | Centralized logging configuration |
| `bot/bot_runner.py` | CLI entry point: `python -m bot.bot_runner <meet-url>` |
| `bot/bots/meet_bot.py` | The core Playwright bot — join flow, caption scraping, speaker resolution, meeting-end detection |
| `bot/utils/webrtc_audio.py` | In-browser audio capture via an injected `RTCPeerConnection` interceptor |
| `bot/utils/sarvam_client.py` | Sarvam AI speech-to-text client, with retry/backoff |
| `bot/utils/speaker_alignment.py`, `bot/utils/diarizer.py` | Supporting speaker-attribution logic |
| `bot/login_once.py` | One-time interactive Google login, saves `bot/session_state.json` |
| `bot/calendar_auth.py`, `bot/calendar_poller.py` | Google Calendar OAuth + polling for auto-join |
| `helpers/summarizer.py` | Azure OpenAI summarization, with chunking for very long transcripts |
| `helpers/email_sender.py` | Markdown-to-HTML summary emails via AWS SES |
| `helpers/caption_parser.py` | Caption text cleanup/parsing helpers |
| `helpers/translator.py` | Optional segment translation |
| `streamlit_app/` | Streamlit dashboard — bot control, meeting browser, live transcript view, Google login UI |
| `setup_db.py` | Creates the database (if needed) and all tables; safe to re-run |
| `flush_db.py` | Truncates all tables (keeps schema) — destructive, requires `--confirm` or an interactive "yes" |
| `startup.sh` | Container/production entrypoint: restores session/calendar secrets from base64 env vars, sets up PulseAudio virtual devices, optionally starts Xvfb, runs `setup_db.py`, starts `uvicorn` |

---

## Database Schema

```sql
ai_gm_meetings
  id               INT PK
  title            VARCHAR(255)   -- sanitized (no emails)
  meet_code        VARCHAR(100)   -- "abc-defg-hij"
  status           VARCHAR(20)    -- pending | active | completed
  created_at       DATETIME
  ended_at         DATETIME
  summary          TEXT           -- AI-generated summary
  organizer_email  VARCHAR(255)   -- summary TO: address
  attendee_emails  TEXT           -- comma-separated BCC addresses

ai_transcript_segments
  id                       INT PK
  meeting_id               INT       -- FK → ai_gm_meetings.id
  speaker_name             VARCHAR(200)
  text                     TEXT
  source                   VARCHAR(50)    -- e.g. "caption" | "stt"
  start_time / end_time    FLOAT          -- seconds from meeting start
  created_at               DATETIME
  speaker_source           VARCHAR(50)    -- caption | mixed_caption | active_dom | webrtc | track_cache
  speaker_confidence       NUMERIC(5,4)
  alignment_overlap_ms     INT
  is_overlapping_speech    BOOLEAN
  secondary_speakers_json  JSON
  confidence               VARCHAR(10)    -- "high" | "low" (crosstalk flag)
  language_code            VARCHAR(20)    -- e.g. en-IN, hi-IN
  translated_text          TEXT
  translated_language_code VARCHAR(10)

ai_participants
  id           INT PK
  meeting_id   INT       -- FK → ai_gm_meetings.id
  name         VARCHAR(200)
  is_active    BOOLEAN

audit_logs
  id           INT PK
  entity_type  VARCHAR(50)
  entity_id    INT
  action       VARCHAR(20)
  changes      JSON
  created_at   DATETIME
```

See [docs/database.md](database.md) for the `ai_meeting_master` reporting table, which
denormalizes a finished meeting (summary, transcript, hand-raises, chat) into one row
per meeting for analytics.

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Server health check |
| GET | `/debug/db` | DB connection diagnostics |
| POST | `/bot/join` | Send the bot to a meeting |
| GET | `/bot/status` | Status of all active bots |
| GET | `/bot/auth-status` | Bot's Google auth status |
| DELETE | `/bot/{meet_code}/stop` | Stop a running bot |
| GET | `/bot/{meet_code}/fail-screenshot` | Screenshot captured on a join failure, for debugging |
| POST | `/bot/login/*` | Remote, screenshot-driven Google login flow (open/status/start/screenshot/click/type/key/navigate/done/abort) — used by the Streamlit "Google Login" page for headless/Docker hosts with no display |
| GET | `/meetings` | List recent meetings |
| GET | `/meetings/{id}` | Single meeting details |
| GET | `/meetings/by-code/{meet_code}` | Look up a meeting by its Meet code |
| POST | `/meetings/start` | Create a meeting row (called by the bot on join) |
| PATCH | `/meetings/{id}/end` | Mark a meeting completed |
| PATCH | `/meetings/{id}/title` | Update a meeting's title |
| PATCH | `/meetings/{id}/emails` | Update organizer/attendee emails for the summary |
| GET | `/meetings/{id}/transcript` | Full transcript (REST) |
| GET | `/meetings/{id}/transcript/live` | Live transcript (SSE) |
| POST | `/meetings/{id}/transcript` | Add a transcript segment |
| GET | `/meetings/{id}/participants` | List participants |
| POST | `/meetings/{id}/participants` | Upsert a participant |
| POST | `/meetings/{id}/summarize` | Generate (and optionally email) the AI summary |
| GET | `/meetings/{id}/speaker-stats` | Talk-time stats per speaker |
| POST | `/meetings/{id}/push-summary` | Upsert the finished meeting into `ai_meeting_master` |
| GET | `/meetings/master/audit-empty` | Reporting-table column-completeness audit |
| GET | `/calendar/status` | Calendar integration status |

All endpoints are interactive at `/docs` (Swagger UI) once the backend is running. If
`BOT_API_KEY` is set, bot-control endpoints require an `X-API-Key` header.

---

## Speaker Attribution Pipeline

```
Audio chunk (WAV)
        │
        ▼
Sarvam STT → raw text
        │
        ▼
Speaker resolution
        │
        ├── S1: WebRTC track metadata (participant name attached to the audio track)
        │       Works best: 1-on-1 meetings, authenticated bot
        │       Weak point: SFU track-slot reuse in large meetings
        │
        ├── S2: Active-speaker DOM majority vote (500ms polls → who spoke most in
        │       the segment's time window)
        │       Works best: a clear "is speaking" indicator in the DOM
        │       Weak point: rapid speaker changes, large meetings
        │
        ├── S3: Caption timeline closest time match (±8s window)
        │       Works best: any meeting size, when live captions are enabled
        │       Weak point: captions disabled, or lag beyond the match window
        │
        └── S4: Track-assignment cache (last resort — remember which track last
                belonged to whom)
                Weak point: SFU track-slot reuse changes the owner

Rough observed accuracy:
  2-5 participants    ~85-90%
  5-10 participants   ~70-80%
  10-20 participants  ~50-65%
  20+ participants    ~40-55%
```

---

## Environment Variables

See [docs/deployment.md](deployment.md#environment-variables) for the full, current
list — this section intentionally isn't duplicated here to avoid the two docs drifting
out of sync.

---

## Known Limitations & Roadmap

| Issue | Impact |
|---|---|
| Speaker attribution degrades above ~10 participants | Wrong speaker names in the transcript |
| SFU track-slot reuse | One speaker's audio can get labeled with another's name |
| Live captions are lower quality in some languages | Mitigated by running audio through Sarvam STT as well |
| Bot's saved Google session expires periodically | Bot can't join meetings until `bot/login_once.py` is re-run and `BOT_SESSION_B64` updated |
| Concurrency is capped by RAM, not just config | Increase `BOT_MAX_CONCURRENT` only if the host has headroom — each bot runs its own Chromium instance |

Ideas for improving speaker attribution further, roughly in order of effort:

1. **Caption-first attribution** — treat the caption speaker name as the primary
   source of truth (Google's own attribution is usually correct) and use Sarvam's text
   for better multilingual quality, time-aligning the two.
2. **Per-track WebRTC recording** — record each participant's audio track separately
   instead of resolving speaker identity after the fact.
3. **Official per-participant media APIs**, where available, remove the need for
   speaker resolution heuristics entirely.
