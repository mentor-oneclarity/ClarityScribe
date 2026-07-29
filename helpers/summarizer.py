import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv
from openai import AsyncAzureOpenAI, AsyncOpenAI

load_dotenv()

logger = logging.getLogger("backend")


@dataclass
class SummaryResult:
    summary: str


_client = None

_MAX_TRANSCRIPT_CHARS = 60_000

# ── Quality gate thresholds (configurable via env) ────────────────────────────
_MIN_MEANINGFUL_UTTERANCES = int(os.getenv("MIN_MEANINGFUL_UTTERANCES", "5"))
_MIN_MEANINGFUL_WORDS = int(os.getenv("MIN_MEANINGFUL_WORDS", "50"))
_MIN_SUBSTANTIVE_TOPICS = int(os.getenv("MIN_SUBSTANTIVE_TOPICS", "1"))

_FILLER_WORDS = frozenset({
    # Single-word acknowledgements and greetings — no real content
    "yes", "yeah", "yep", "yup", "no", "nope", "ok", "okay", "sure", "alright",
    "right", "great", "fine", "good", "nice", "cool", "wow", "oh", "ah", "uh",
    "um", "hmm", "hm", "hi", "hello", "bye", "goodbye", "thanks", "thank",
    "welcome", "perfect", "noted", "absolutely", "definitely", "certainly",
    "indeed", "exactly", "correct", "understood", "roger", "copy",
    # Hindi/Hinglish single-word fillers
    "haan", "nahi", "theek", "achha", "bas", "acha", "thik",
})

_JOIN_LEAVE_RE = re.compile(
    r'\b(has joined|has left|left the meeting|joined the meeting|'
    r'has entered|has exited|entered the meeting|exited the meeting)\b',
    re.IGNORECASE,
)
_SPEAKER_PREFIX_RE = re.compile(r'^([^:\n]{1,60}):\s*')

# ── Layer 2: transcript preprocessor ─────────────────────────────────────────

def _preprocess_transcript(transcript: str) -> str:
    """
    Clean a raw live-caption transcript before sending to the LLM.
    Reduces noise without losing meaning:
      - Strips join/leave event lines
      - Removes lines that are purely filler words (no real content)
      - Collapses consecutive word repetitions (transcription stutter)
      - Removes excessive blank lines
    Does NOT normalise vocabulary — the LLM handles meaning correction.
    """
    lines = transcript.split("\n")
    cleaned: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Drop join/leave events — they are not discussion content
        if _JOIN_LEAVE_RE.search(stripped):
            continue

        # Split speaker prefix from content
        speaker_m = _SPEAKER_PREFIX_RE.match(stripped)
        if speaker_m:
            speaker  = speaker_m.group(1).strip()
            content  = stripped[speaker_m.end():].strip()
        else:
            speaker  = None
            content  = stripped

        if not content:
            continue

        # Drop lines that are entirely filler (e.g. "ok", "yes yes", "hmm")
        words      = re.findall(r"\b\w+\b", content.lower())
        non_filler = [w for w in words if w not in _FILLER_WORDS]
        if not non_filler:
            continue

        # Collapse consecutive repeated words — captions stutter often
        # "yes yes yes" → "yes", "the the team" → "the team"
        content = re.sub(r'\b(\w+)(\s+\1){2,}\b', r'\1', content, flags=re.IGNORECASE)
        content = re.sub(r'\b(\w+)\s+\1\b',        r'\1', content, flags=re.IGNORECASE)

        rebuilt = f"{speaker}: {content}" if speaker else content
        cleaned.append(rebuilt)

    result = "\n".join(cleaned)

    # Collapse 3+ consecutive blank lines to one
    result = re.sub(r'\n{3,}', '\n\n', result)

    return result.strip()

# Names that are clearly NOT real participants — filter from fallback summary.
# Catches: URLs ("https"), system words ("You", "Unknown"), all-lowercase short tokens.
_GARBAGE_PARTICIPANT_RE = re.compile(
    r'^(You|Unknown|Bot|Me|presenter|participant|everyone|all|group|https?|www)$'
    r'|https?://'           # URL fragments (https:// in name)
    r'|@[^\s]+'             # email fragments
    r'|^[a-z]{1,5}$'        # short all-lowercase tokens like "https", "you", "me"
    r'|\d{5,}',             # long digit runs (phone numbers, IDs)
    re.IGNORECASE,
)


def _get_client():
    global _client
    if _client is not None:
        return _client

    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    azure_key = os.getenv("AZURE_OPENAI_API_KEY", "")

    if azure_endpoint and azure_key:
        _client = AsyncAzureOpenAI(
            api_key=azure_key,
            azure_endpoint=azure_endpoint,
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview"),
            timeout=300.0,      # 5 min — covers 1h+ meeting summaries
            max_retries=2,
        )
        logger.info("Using Azure OpenAI at %s", azure_endpoint)
    else:
        openai_key = os.getenv("OPENAI_API_KEY", "")
        if not openai_key:
            raise RuntimeError(
                "No OpenAI credentials found. Set AZURE_OPENAI_ENDPOINT + "
                "AZURE_OPENAI_API_KEY, or OPENAI_API_KEY in .env"
            )
        _client = AsyncOpenAI(api_key=openai_key, timeout=300.0, max_retries=2)
        logger.info("Using standard OpenAI")

    return _client


def _get_model() -> str:
    return os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME") or os.getenv(
        "OPENAI_MODEL", "gpt-4o-mini"
    )


# ── Quality gate ──────────────────────────────────────────────────────────────

def _assess_transcript_quality(transcript: str) -> dict:
    """
    Analyse transcript content and return quality metrics.
    Does NOT call the LLM — pure local heuristics.
    """
    lines = [ln.strip() for ln in transcript.strip().split("\n") if ln.strip()]

    events: list[str] = []
    participants: set[str] = set()
    meaningful_utterances = 0
    meaningful_words = 0
    substantive_topics = 0

    for line in lines:
        # Detect join/leave events
        if _JOIN_LEAVE_RE.search(line):
            events.append(line)
            name_m = re.match(
                r'^(.+?)\s+(?:has\s+(?:joined|left|entered|exited)|'
                r'joined|left|entered|exited)',
                line, re.IGNORECASE,
            )
            if name_m:
                name = name_m.group(1).strip()
                if not _GARBAGE_PARTICIPANT_RE.search(name):
                    participants.add(name)
            continue

        # Strip speaker prefix and record speaker name
        speaker_m = _SPEAKER_PREFIX_RE.match(line)
        if speaker_m:
            name = speaker_m.group(1).strip()
            if not _GARBAGE_PARTICIPANT_RE.search(name):
                participants.add(name)
            content = line[speaker_m.end():]
        else:
            content = line

        if not content.strip():
            continue

        words = re.findall(r"\b[a-zA-Zऀ-ॿ]+\b", content.lower())
        non_filler = [w for w in words if w not in _FILLER_WORDS and len(w) > 1]

        if len(non_filler) >= 3:
            meaningful_utterances += 1
            meaningful_words += len(non_filler)
            if len(non_filler) >= 6:
                substantive_topics += 1

    is_substantive = (
        meaningful_utterances >= _MIN_MEANINGFUL_UTTERANCES
        and meaningful_words >= _MIN_MEANINGFUL_WORDS
        and substantive_topics >= _MIN_SUBSTANTIVE_TOPICS
    )

    return {
        "is_substantive": is_substantive,
        "meaningful_utterance_count": meaningful_utterances,
        "meaningful_word_count": meaningful_words,
        "substantive_topic_count": substantive_topics,
        "participants": sorted(participants),
        "events": events,
    }


def _build_fallback_summary(quality: dict, known_participants: list[str] | None = None) -> str:
    """Return a short, honest fallback when the transcript lacks substance."""
    participant_names = known_participants or quality["participants"]
    participants_str = (
        ", ".join(participant_names) if participant_names else "Not identified"
    )
    return (
        "## Meeting Overview\n\n"
        f"Participants: {participants_str}\n\n"
        "The captured transcript does not contain enough substantive discussion "
        "to generate a detailed summary. The content appears to consist of brief "
        "acknowledgements, closing remarks, or attendance updates only."
    )


# ── LLM prompts ───────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a professional meeting assistant who writes polished, executive-quality meeting minutes.

TRANSCRIPT INPUT QUALITY — read this first:
The transcript you receive is captured live from automated speech recognition during a meeting.
It will contain transcription errors. This is expected and normal. Specifically expect:
  - Phonetic misspellings (e.g. "matb" = "matlab", "deploay" = "deploy", "tomarrow" = "tomorrow")
  - Hinglish and mixed-language phrases (e.g. "karna hai" = "we need to", "theek hai" = "agreed",
    "abhi" = "now", "woh" = "that", "matlab" = "meaning/I mean", "bas" = "just/that's it",
    "nahi" = "no/not", "kar lete" = "let's do it", "dekh lete" = "let's look into it")
  - Incomplete sentences and false starts
  - Missing punctuation and incorrect capitalisation
  - Repeated words from caption stutter (e.g. "we we will deploy")
  - Filler sounds transcribed as words (e.g. "uh", "um", "hmm")

YOUR ROLE is NOT to clean the transcript. \
YOUR ROLE is to understand what was actually discussed and produce professional meeting minutes \
that read as if written by an experienced human assistant — \
grammatically perfect, error-free, and in clear business English.

CORRECTION RULES:
- Silently correct obvious transcription errors using context. \
  Never copy a misspelled or garbled word into the summary.
- Convert all Hinglish, Hindi, or mixed-language content into natural professional English. \
  Never leave untranslated or transliterated words in the output.
- Complete truncated sentences using surrounding context to infer the intended meaning.
- Remove filler words, repetitions, and false starts — they are noise, not content.
- Preserve technical terms, product names, API names, libraries, and proper nouns — \
  use the correct standard spelling even if the transcript has a slight error \
  (e.g. "react" → "React", "git hub" → "GitHub", "post gres" → "PostgreSQL").
- Do NOT reproduce any transcription artifact in the output. \
  The summary must be completely free of spelling errors, grammar errors, and garbled words.

GROUNDING RULES — strictly enforced:
- Every Decision and Action Item must name a real speaker from the transcript.
  Use the format: "as decided by [speaker_name]" or "as proposed by [speaker_name]".
- Transcript lines marked "(low)" indicate crosstalk or uncertain attribution. \
  Do NOT cite these as firm decisions or action items unless confirmed by a "(high)" line.
- Use exact speaker names as they appear in the transcript. \
  Never generalise to "the team", "someone", or "a participant".
- Do NOT introduce topics, decisions, or facts that have no basis in the transcript. \
  You MAY correct errors and complete truncated sentences — you may NOT invent new content.
- Attendance events (joined/left the meeting) are not discussion content — ignore them.

OUTPUT RULES:
- No emojis anywhere in the output.
- Meeting Overview, Key Topics Discussed, Decisions Made, and Key Insight \
are MANDATORY — always include all four even if content is brief.
- DECISIONS MADE is ONE plain bullet list — no table, no columns, and NEVER label a bullet \
  with the word "Decision" or "Action". It holds both outcomes settled in the meeting AND \
  future tasks assigned to someone, written as natural sentences:
  - Settled outcome: "[what was decided] — as decided by [speaker_name]."
  - Future task: "[speaker_name] will [task]" — weave in the deadline or urgency naturally \
    only if the transcript actually states one (e.g. "by Friday", "urgently"); \
    never invent a deadline or priority that wasn't said.
  - If there is genuinely nothing to report: "- No decisions or action items recorded."
- COMPLETENESS RULE: list EVERY decision and EVERY future task as its own bullet. \
  Do NOT stop at 3 or any fixed number. Never truncate or say "and more…". \
  Never list the same point twice.
- Open Questions / Blockers is optional — include only if explicitly in the transcript. \
  Do not write "None" or "N/A" — simply omit the heading.
- No corporate filler: "moving forward", "going forward", "leveraging synergies", \
  "action-oriented", "taking ownership", "drill down", "circle back", "touch base".
- SPEAKER STATS are background context only — never reproduce them in any output section.
- Key Insight must be 1–2 sentences on the most important strategic or business takeaway \
  from the discussion. Never mention participation percentages or speaking time.\
"""

_CHUNK_SYSTEM_PROMPT = """\
You are a professional meeting assistant summarizing one segment of a live meeting transcript.
The transcript is raw, noisy, and may contain transcription errors, Hinglish phrases, \
incomplete sentences, and filler words — this is expected.

Your job is to extract the key points, decisions, action items, and blockers from this segment \
and express them in clean, professional English — NOT to reproduce the raw text.

Rules:
- Silently correct transcription errors. Never copy garbled or misspelled words.
- Translate any Hinglish or mixed-language content into professional English.
- Name the speaker for every decision or action item.
- Lines marked "(low)" indicate crosstalk — exclude from firm decisions.
- Be concise — this output will be merged with other segments later.
- Output in English only. Omit any section with no real content.\
"""

_MERGE_SYSTEM_PROMPT = """\
You are a professional meeting assistant merging multiple segment summaries \
of one meeting into a single, polished set of meeting minutes.

Eliminate redundancy. Preserve ALL unique points, decisions, action items, and blockers.
Every Decisions Made bullet must name the real speaker — never label a bullet "Decision" or "Action".
COMPLETENESS RULE: list EVERY decision and EVERY future task as its own bullet. Do NOT cap at any fixed number.

The output must read as if written by an experienced human meeting assistant — \
grammatically perfect, in clear business English, free from transcription artifacts.

Use the standard structure: Meeting Overview, Key Topics Discussed, Decisions Made \
(one plain bullet list — outcomes and future tasks together, no table, no labels), \
Open Questions / Blockers, Key Insight.
No emojis. Omit sections with nothing real to report. Output in English only.\
"""


# ── Tier-aware prompt builder ─────────────────────────────────────────────────

def _build_prompt(
    transcript: str,
    duration_minutes: int,
    speaker_stats: str,
    participants: list,
) -> str:
    """Build the user-turn prompt with tier-appropriate length target."""
    if duration_minutes > 0 and duration_minutes < 15:
        tier, word_target = "SHORT", "at least 150"
    elif duration_minutes <= 45:
        tier, word_target = "MEDIUM", "at least 300"
    else:
        tier, word_target = "LONG", "at least 500"

    if duration_minutes >= 60:
        h, m = divmod(duration_minutes, 60)
        duration_str = f"{h}h {m}m" if m else f"{h}h"
    elif duration_minutes > 0:
        duration_str = f"{duration_minutes} minutes"
    else:
        duration_str = "Unknown"

    participants_str = ", ".join(participants) if participants else "See transcript"

    stats_block = (
        f"Speaker participation (background context only — do NOT reproduce in output):\n{speaker_stats}\n"
        if speaker_stats.strip() else ""
    )

    topics_section = (
        "\n## Key Topics Discussed\n"
        "- [One bullet per distinct topic or per speaker's update — include ALL topics discussed. "
        "For standups, write one bullet per speaker showing what they shared.]\n"
    )

    return f"""\
Read the full transcript below carefully. \
Note every speaker name and EVERYTHING they said — every decision, every action item, \
every discussion point, every topic raised. Do not skip or compress anything.

NOTE: This transcript was captured live from automated speech recognition. \
It may contain transcription errors, Hinglish phrases, and incomplete sentences — \
read through these to understand what was actually said. \
When WRITING the summary, express each point in correct, professional English. \
Correct errors on output. Translate Hinglish to English. \
But first — READ EVERYTHING and miss nothing.

TRANSCRIPT:
{transcript}

---
Meeting duration: {duration_str}
Participants: {participants_str}
{stats_block}
Now write the meeting summary in the EXACT structure below. \
Target {word_target} words ({tier} meeting).
Use ONLY what is in the transcript — capture every point that was discussed. \
Do not invent content. Do not skip content. \
When writing each point, use correct professional English — fix errors, translate Hinglish.

MANDATORY sections — you MUST include ALL of these even if content is brief:
  • Meeting Overview
  • Key Topics Discussed        (for standups: one bullet per speaker's update)
  • Decisions Made              (ONE plain bullet list — settled outcomes AND future tasks together, no table, never label a bullet "Decision"/"Action"; if none: "- No decisions or action items recorded.")
  • Key Insight

OPTIONAL sections — include ONLY if explicitly present in the transcript, otherwise skip entirely:
  • Open Questions / Blockers

---

## Meeting Overview
Duration: {duration_str}
Participants: {participants_str}
{topics_section}
## Decisions Made
- [decision/outcome] — as decided by [exact speaker name].
- [exact speaker name] will [specific task] [weave in deadline/urgency only if stated in transcript].

## Open Questions / Blockers
- [unresolved issue or blocker from the transcript]

## Key Insight
[1–2 sentences on the single most important takeaway from the discussion. \
Must be based on meeting content — never mention speaker percentages or talk time.]"""


# ── Chunking helpers ──────────────────────────────────────────────────────────

def _chunk_transcript(transcript: str, max_chars: int) -> list[str]:
    if len(transcript) <= max_chars:
        return [transcript]

    lines = transcript.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1
        if current_len + line_len > max_chars and current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len

    if current:
        chunks.append("\n".join(current))

    return chunks


async def _summarize_chunk(chunk: str, chunk_index: int, total_chunks: int) -> str:
    prompt = (
        f"This is segment {chunk_index + 1} of {total_chunks} of the meeting transcript.\n\n"
        f"{chunk}"
    )
    response = await _get_client().chat.completions.create(
        model=_get_model(),
        messages=[
            {"role": "system", "content": _CHUNK_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    return response.choices[0].message.content or ""


async def _merge_chunk_summaries(
    chunk_summaries: list[str],
    meeting_title: str,
    speaker_stats: str,
    participants: list,
    duration_minutes: int = 0,
) -> str:
    combined = "\n\n---\n\n".join(
        f"Segment {i + 1}:\n{s}" for i, s in enumerate(chunk_summaries)
    )
    participants_str = ", ".join(participants) if participants else "See transcript"

    if duration_minutes >= 60:
        h, m = divmod(duration_minutes, 60)
        duration_str = f"{h}h {m}m" if m else f"{h}h"
    elif duration_minutes > 0:
        duration_str = f"{duration_minutes} minutes"
    else:
        duration_str = "Unknown"

    stats_note = (
        f"Speaker participation (background context only — do NOT reproduce in output):\n{speaker_stats}\n"
        if speaker_stats.strip() else ""
    )
    prompt = (
        f"SEGMENT SUMMARIES:\n{combined}\n\n"
        f"---\n"
        f"Meeting duration: {duration_str}\n"
        f"Participants: {participants_str}\n"
        f"{stats_note}\n"
        f"Merge the {len(chunk_summaries)} segment summaries above into one coherent meeting summary.\n"
        f"Omit a section only if it is genuinely absent from all segments. "
        f"For standups, Key Topics should list each participant's update.\n\n"
        f"## Meeting Overview\n"
        f"Duration: {duration_str}\n"
        f"Participants: {participants_str}\n\n"
        f"## Key Topics Discussed\n"
        f"- [one bullet per topic or participant update]\n\n"
        f"## Decisions Made\n"
        f"- [decision/outcome] — as decided by [exact speaker name].\n"
        f"- [exact speaker name] will [task] [weave in deadline/urgency only if stated in transcript].\n\n"
        f"## Open Questions / Blockers\n"
        f"- [unresolved issue]\n\n"
        f"## Key Insight\n"
        f"[1–2 sentences on the most important business takeaway — not speaker statistics]"
    )
    response = await _get_client().chat.completions.create(
        model=_get_model(),
        messages=[
            {"role": "system", "content": _MERGE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    return response.choices[0].message.content or "Summary generation failed."


# ── Public API ────────────────────────────────────────────────────────────────

async def generate_summary(
    transcript: str,
    speaker_stats: str = "",
    duration_minutes: int = 0,
    known_participants: list[str] | None = None,
) -> SummaryResult:
    if not transcript.strip():
        return SummaryResult(summary="No transcript available to summarize.")

    # ── Layer 2: preprocess — remove noise before quality gate + LLM ─────────
    original_len = len(transcript)
    transcript   = _preprocess_transcript(transcript)
    logger.info(
        "Summary: preprocessed transcript %d → %d chars (removed %d chars of noise)",
        original_len, len(transcript), original_len - len(transcript),
    )
    if not transcript.strip():
        return SummaryResult(summary="No transcript available to summarize.")

    # ── Quality gate ──────────────────────────────────────────────────────────
    quality = _assess_transcript_quality(transcript)
    logger.info(
        "Summary quality gate: substantive=%s utterances=%d words=%d topics=%d",
        quality["is_substantive"],
        quality["meaningful_utterance_count"],
        quality["meaningful_word_count"],
        quality["substantive_topic_count"],
    )

    if not quality["is_substantive"]:
        logger.warning(
            "Summary: transcript did not pass quality gate "
            "(utterances=%d/%d words=%d/%d topics=%d/%d) — returning fallback",
            quality["meaningful_utterance_count"], _MIN_MEANINGFUL_UTTERANCES,
            quality["meaningful_word_count"], _MIN_MEANINGFUL_WORDS,
            quality["substantive_topic_count"], _MIN_SUBSTANTIVE_TOPICS,
        )
        return SummaryResult(
            summary=_build_fallback_summary(quality, known_participants=known_participants)
        )

    participants = known_participants or sorted(quality["participants"])

    # ── Long meeting: chunk → summarise each → merge ──────────────────────────
    if len(transcript) > _MAX_TRANSCRIPT_CHARS:
        chunks = _chunk_transcript(transcript, _MAX_TRANSCRIPT_CHARS)
        logger.info(
            "Summary: transcript too long (%d chars) — splitting into %d chunks",
            len(transcript), len(chunks),
        )
        try:
            chunk_summaries = []
            for i, chunk in enumerate(chunks):
                logger.info("Summary: summarizing chunk %d/%d …", i + 1, len(chunks))
                chunk_text = await _summarize_chunk(chunk, i, len(chunks))
                chunk_summaries.append(chunk_text)
            merged_text = await _merge_chunk_summaries(
                chunk_summaries, "", speaker_stats, participants, duration_minutes
            )
            return SummaryResult(summary=merged_text)
        except Exception as exc:
            logger.error("Chunked summary failed: %s — falling back to truncated", exc)
            transcript = transcript[:_MAX_TRANSCRIPT_CHARS]

    prompt = _build_prompt(
        transcript=transcript,
        duration_minutes=duration_minutes,
        speaker_stats=speaker_stats,
        participants=participants,
    )

    try:
        response = await _get_client().chat.completions.create(
            model=_get_model(),
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        if not response.choices:
            logger.warning("Summary: no choices returned from Azure OpenAI — content filter or quota?")
            return SummaryResult(
                summary=_build_fallback_summary(quality, known_participants=known_participants)
            )

        choice = response.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason == "content_filter":
            logger.warning("Summary: Azure OpenAI content_filter triggered — returning fallback summary")
            return SummaryResult(
                summary=_build_fallback_summary(quality, known_participants=known_participants)
            )

        content = (choice.message.content or "").strip()
        if not content:
            logger.warning("Summary: Azure OpenAI returned empty content (finish_reason=%s) — returning fallback", finish_reason)
            return SummaryResult(
                summary=_build_fallback_summary(quality, known_participants=known_participants)
            )

        return SummaryResult(summary=content)
    except Exception as exc:
        logger.error("Summary generation failed: %s", exc, exc_info=True)
        raise


async def generate_and_email_summary(
    transcript: str,
    meeting_title: str,
    meet_code: str = "",
    to_email: str = "",
    duration_minutes: int = 0,
    speaker_stats: str = "",
    bcc_emails: Optional[list] = None,
    cc_emails: Optional[list] = None,
    meeting_id: Optional[int] = None,
    known_participants: Optional[list] = None,
) -> SummaryResult:
    """Generate summary (with quality gate + chunking for long meetings) then email via SES."""
    logger.info(
        "Summary: generating for meet_code=%s title=%r duration=%dmin "
        "transcript_chars=%d to_email=%s bcc_count=%d",
        meet_code, meeting_title, duration_minutes,
        len(transcript), to_email or "(not set)", len(bcc_emails or []),
    )

    result = await generate_summary(
        transcript,
        speaker_stats=speaker_stats,
        duration_minutes=duration_minutes,
        known_participants=known_participants,
    )

    logger.info(
        "Summary: generated %d chars for meet_code=%s",
        len(result.summary), meet_code,
    )

    if not to_email:
        logger.warning(
            "Summary: ⚠️  no to_email set for meet_code=%s — summary saved to DB but NO email sent",
            meet_code,
        )
        return result

    if "No transcript" in result.summary:
        logger.warning(
            "Summary: empty transcript for meet_code=%s — skipping email to %s",
            meet_code, to_email,
        )
        return result

    try:
        logger.info(
            "Summary: 📧 sending email → to=%s bcc_count=%d meet_code=%s title=%r",
            to_email, len(bcc_emails or []), meet_code, meeting_title,
        )
        from helpers.email_sender import send_summary_email
        sent = await send_summary_email(
            to_email=to_email,
            meeting_title=meeting_title,
            summary=result.summary,
            meet_code=meet_code,
            duration_minutes=duration_minutes,
            bcc_emails=bcc_emails,
            cc_emails=cc_emails,
            meeting_id=meeting_id,
        )
        if sent:
            logger.info(
                "Summary: ✅ email delivered → %s  meet_code=%s",
                to_email, meet_code,
            )
        else:
            logger.error(
                "Summary: ❌ email delivery failed (SES returned False) → %s  meet_code=%s",
                to_email, meet_code,
            )
    except Exception as exc:
        logger.error(
            "Summary: ❌ email exception → %s  meet_code=%s  error=%s",
            to_email, meet_code, exc,
        )

    return result
