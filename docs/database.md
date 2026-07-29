# `ai_meeting_master` — Reporting Table

## Overview

`ai_meeting_master` is a denormalized reporting table with **one row per finished
meeting**. It exists so analytics and reporting don't need to join across the
transactional tables (`ai_gm_meetings`, `ai_transcript_segments`, `ai_participants`) for
every query — the whole meeting (metadata, transcript, hand-raises, chat) is collapsed
into a single row with JSON columns for the parts that vary in shape.

- **Engine:** MySQL 8.0
- **Populated:** After a meeting ends and its summary has been generated
- **Grain:** One row per meeting (upserted — safe to re-push the same meeting)

This table is defined in `core/database.py` as the `MeetingMaster` model.

---

## Why This Table Exists

Meeting data otherwise lives across several transactional tables:

| Table | What it stores |
|---|---|
| `ai_gm_meetings` | Meeting sessions |
| `ai_transcript_segments` | Captions + STT segments |
| `ai_participants` | Roster from the participant panel |
| `audit_logs` | Action audit trail |

Joining all of these (plus a `user` table for identity) for every report is expensive.
`ai_meeting_master` denormalizes the finished meeting into one row, with the transcript,
hand-raise events, and chat messages stored as JSON.

---

## How Data Gets In

```
Bot joins meeting
       ↓
Real-time: ai_transcript_segments (captions + STT), ai_participants
       ↓
Meeting ends → PATCH /meetings/{id}/end (status=completed, ended_at=now)
       ↓
Summary generated → POST /meetings/{id}/summarize
       ↓
POST /meetings/{id}/push-summary
       ↓
Upsert (INSERT ... ON DUPLICATE KEY UPDATE) into ai_meeting_master ✅
```

The endpoint is idempotent — calling it again for the same `meeting_id` updates the
existing row rather than creating a duplicate (`meeting_id` has a unique constraint).

---

## Schema

```sql
CREATE TABLE ai_meeting_master (
    id                  INT             NOT NULL AUTO_INCREMENT PRIMARY KEY,

    -- Meeting info
    meeting_id          INT             NOT NULL,
    meet_code           VARCHAR(100)    NOT NULL,
    title               VARCHAR(255)    NULL,
    meeting_status      VARCHAR(20)     NULL,
    meeting_started_at  DATETIME        NULL,
    meeting_ended_at    DATETIME        NULL,
    meeting_date        DATE            NULL,
    organizer_email     VARCHAR(255)    NULL,
    attendee_emails     TEXT            NULL,
    summary             TEXT            NULL,

    -- User identity (resolved from organizer_email, best-effort)
    user_id             VARCHAR(36)     NULL,
    tenant_id           VARCHAR(36)     NULL,

    -- Denormalized meeting content
    person_transcript   JSON            NULL,
    hand_raises         JSON            NULL,
    chat_messages       JSON            NULL,
    speaker_source      VARCHAR(50)     NULL,

    created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE KEY uq_meeting_master_meeting_id (meeting_id),
    INDEX idx_meeting_id  (meeting_id),
    INDEX idx_meeting_date (meeting_date),
    INDEX idx_user_id     (user_id),
    INDEX idx_tenant_id   (tenant_id)
);
```

---

## Column Reference

### Meeting info

| Column | Type | Description |
|---|---|---|
| `id` | INT | Auto-increment primary key |
| `meeting_id` | INT | The originating `ai_gm_meetings.id` (unique — one row per meeting) |
| `meet_code` | VARCHAR(100) | Google Meet room code, e.g. `abc-defg-hij` |
| `title` | VARCHAR(255) | Meeting title (falls back to `Meeting {meet_code}` if empty) |
| `meeting_status` | VARCHAR(20) | Resolved status at push time — normally `completed` |
| `meeting_started_at` | DATETIME | When the meeting row was created (bot joined) |
| `meeting_ended_at` | DATETIME | When the meeting ended |
| `meeting_date` | DATE | `meeting_ended_at`'s date (or `meeting_started_at`'s if the meeting never got an end time) — indexed for date-range reporting |
| `organizer_email` | VARCHAR(255) | Host email — summary sent TO this address |
| `attendee_emails` | TEXT | Comma-separated attendee emails (BCC on the summary email) |
| `summary` | TEXT | AI-generated meeting summary |

### User identity

Resolved with a best-effort lookup against a `user` table by `organizer_email`, at
push time:

```sql
SELECT user_id, company_id AS tenant_id FROM `user` WHERE email = :organizer_email LIMIT 1
```

| Column | Type | Description |
|---|---|---|
| `user_id` | VARCHAR(36) | UUID from `user.user_id`, if a match was found |
| `tenant_id` | VARCHAR(36) | UUID from `user.company_id`, if a match was found |

> Both are `NULL` if `organizer_email` doesn't match any row in `user`, or if there is
> no `user` table in your deployment — this lookup fails silently (logs a warning) and
> the rest of the row is still written.

### Denormalized content

| Column | Type | Description |
|---|---|---|
| `person_transcript` | JSON | `[{"speaker": "...", "text": "..."}, ...]`, ordered by time. Bot-attributed system speakers (e.g. the bot's own display name) are filtered out |
| `hand_raises` | JSON | `[{"speaker": "...", "raised_at": ..., "lowered_at": ...}, ...]`. No transactional table of its own — the bot accumulates these in memory during the meeting and passes them in the `push-summary` request body |
| `chat_messages` | JSON | `[{"sender": "...", "text": "...", "sent_at": ...}, ...]`, same pattern as `hand_raises` |
| `speaker_source` | VARCHAR(50) | The most common `speaker_source` value across the meeting's transcript segments (see [docs/architecture.md](architecture.md#speaker-attribution-pipeline)) |

### Timestamps

| Column | Type | Description |
|---|---|---|
| `created_at` | DATETIME | Row creation time (first push); updates via `ON DUPLICATE KEY UPDATE` don't change it |

---

## Source Tables

| Source | Data pulled |
|---|---|
| `ai_gm_meetings` | `meet_code`, `title`, `status`, `created_at`, `ended_at`, `summary`, `organizer_email`, `attendee_emails` |
| `ai_transcript_segments` | Flattened into `person_transcript`; `speaker_source` values aggregated into a single dominant value |
| `user` (if present) | `user_id`, `company_id` → `tenant_id` — matched by `organizer_email = user.email` |
| Request body of `POST /meetings/{id}/push-summary` | `hand_raises`, `chat_messages` |

---

## API

```
POST /meetings/{meeting_id}/push-summary
```

Upserts the finished meeting into `ai_meeting_master`. Safe to call repeatedly for the
same meeting. Request body (optional):

```json
{
  "hand_raises": [{"speaker": "Alice", "raised_at": 120.5, "lowered_at": 128.0}],
  "chat_messages": [{"sender": "Bob", "text": "sounds good", "sent_at": 300.2}]
}
```

Response:

```json
{
  "meeting_id": 42,
  "meet_code": "abc-defg-hij",
  "transcript_count": 187,
  "hand_raise_count": 3,
  "chat_message_count": 12,
  "status": "ok"
}
```

Returns `{"status": "skipped", "reason": "no_segments"}` if the meeting has no
transcript segments yet.

```
POST /meetings/{meeting_id}/push-master
```

Deprecated alias for `push-summary`, kept for backward compatibility.

```
GET /meetings/master/audit-empty
```

Scans `ai_meeting_master` and reports, per column, how many rows are `NULL`/empty and
which `meeting_id`s are affected — useful for spotting gaps in the reporting pipeline
(e.g. meetings whose `user_id`/`tenant_id` never resolved).

---

## Sample Queries

**All transcript turns for a meeting:**

```sql
SELECT
    meeting_id,
    JSON_UNQUOTE(JSON_EXTRACT(person_transcript, CONCAT('$[', idx.n, '].speaker'))) AS speaker,
    JSON_UNQUOTE(JSON_EXTRACT(person_transcript, CONCAT('$[', idx.n, '].text')))    AS text
FROM ai_meeting_master
JOIN JSON_TABLE(
    person_transcript, '$[*]' COLUMNS (n FOR ORDINALITY)
) AS idx
WHERE meet_code = 'abc-defg-hij';
```

**Meetings for a tenant, most recent first:**

```sql
SELECT meeting_id, meet_code, title, meeting_ended_at, summary
FROM ai_meeting_master
WHERE tenant_id = '<tenant-uuid>'
ORDER BY meeting_ended_at DESC;
```

**How many chat messages and hand-raises per meeting:**

```sql
SELECT
    meeting_id,
    JSON_LENGTH(chat_messages) AS chat_message_count,
    JSON_LENGTH(hand_raises)   AS hand_raise_count
FROM ai_meeting_master
ORDER BY meeting_ended_at DESC
LIMIT 20;
```

**Column completeness (equivalent to what `/meetings/master/audit-empty` returns):**

```sql
SELECT
    COUNT(*)                                   AS total_rows,
    SUM(user_id IS NULL)                       AS missing_user_id,
    SUM(tenant_id IS NULL)                     AS missing_tenant_id,
    SUM(summary IS NULL OR summary = '')       AS missing_summary
FROM ai_meeting_master;
```

---

## Notes

- This table is **upserted per meeting**, not appended per transcript segment — each
  `meeting_id` has exactly one row, kept up to date by re-running `push-summary`.
- `user_id` / `tenant_id` are `NULL` whenever `organizer_email` doesn't match a row in
  a `user` table (e.g. external guests, or deployments that don't have a `user` table
  at all — the lookup fails gracefully).
- There is no `audit_action` / `audit_changes_json` column on this table. Audit
  history lives in the separate `audit_logs` table (see
  [docs/architecture.md](architecture.md#database-schema)).
