"""
Speaker-transcript alignment utilities for Step 1 of the OneClarity pipeline.

Functions
---------
get_nearest_speaker     — DOM-timeline lookup with caption lag compensation (1b)
split_chunk_by_speaker  — word-level STT chunk → per-speaker sub-segments (1a)
build_alias_map         — normalise name variants to canonical form (1d)
normalize_speaker_name  — apply alias map to a single name (1d)
"""

import difflib
import logging
import os
from typing import Optional

logger = logging.getLogger("speaker_alignment")

# Seconds to shift audio_time forward when querying the DOM active-speaker
# timeline.  Captions / DOM highlights appear ~1.5 s after audio starts.
CAPTION_LAG_BUFFER_SEC: float = float(os.getenv("CAPTION_LAG_BUFFER_SEC", "1.5"))

# Reject a DOM-timeline match if the nearest entry is farther than this.
_NEAREST_MAX_SEC: float = 5.0


# ── 1b: DOM-timeline lookup with lag buffer ───────────────────────────────────

def get_nearest_speaker(
    audio_time: float,
    speaker_timeline: list,
    lag_buffer: float = CAPTION_LAG_BUFFER_SEC,
) -> Optional[str]:
    """
    Return the DOM-observed speaker closest to (audio_time + lag_buffer).

    The lag buffer compensates for the delay between when audio starts and when
    the DOM highlights the speaker tile. Returns None if the closest entry is
    more than _NEAREST_MAX_SEC away (gap or no data).
    """
    if not speaker_timeline:
        return None
    target = audio_time + lag_buffer
    best_name: Optional[str] = None
    best_diff = float("inf")
    for entry in speaker_timeline:
        diff = abs(entry["abs_time"] - target)
        if diff < best_diff:
            best_diff = diff
            best_name = entry["speaker"]
    return best_name if best_diff <= _NEAREST_MAX_SEC else None


# ── 1a: Word-level chunk split ────────────────────────────────────────────────

def split_chunk_by_speaker(
    stt_words: list,
    speaker_timeline: list,
    chunk_start: float,
    chunk_end: float,
    lag_buffer: float = CAPTION_LAG_BUFFER_SEC,
) -> list:
    """
    Split a Sarvam STT chunk into sub-segments wherever the active speaker changes.

    Parameters
    ----------
    stt_words       Word-level timestamps from Sarvam: [{word, start, end}]
                    where start/end are seconds RELATIVE to the chunk.
    speaker_timeline  [{abs_time, speaker}] in absolute meeting seconds.
    chunk_start     Absolute start of the chunk in meeting seconds.
    chunk_end       Absolute end of the chunk in meeting seconds.
    lag_buffer      Seconds to shift each word's time before DOM lookup.

    Returns
    -------
    List of {speaker, text, start_time, end_time} in absolute meeting seconds.
    Falls back to a single whole-chunk segment when word timestamps are absent.
    """
    if not stt_words:
        return []

    has_timestamps = any(
        isinstance(w, dict) and "start" in w and "end" in w
        for w in stt_words
    )

    if not has_timestamps:
        logger.warning(
            "STT response has no word-level timestamps — "
            "falling back to whole-chunk attribution (chunk=[%.1f, %.1f])",
            chunk_start, chunk_end,
        )
        mid = (chunk_start + chunk_end) / 2.0
        speaker = get_nearest_speaker(mid, speaker_timeline, lag_buffer)
        words = [
            (w.get("word", w) if isinstance(w, dict) else str(w)).strip()
            for w in stt_words
        ]
        return [{
            "speaker": speaker,
            "text": " ".join(w for w in words if w),
            "start_time": chunk_start,
            "end_time": chunk_end,
        }]

    sub_segments: list = []
    current_speaker: Optional[str] = None
    current_words: list = []
    current_start: float = chunk_start

    for word_entry in stt_words:
        word_rel_start = float(word_entry.get("start", 0.0))
        word_abs_time = chunk_start + word_rel_start
        speaker = get_nearest_speaker(word_abs_time, speaker_timeline, lag_buffer)

        if speaker != current_speaker:
            if current_words:
                sub_segments.append({
                    "speaker": current_speaker,
                    "text": " ".join(current_words).strip(),
                    "start_time": round(current_start, 2),
                    "end_time": round(word_abs_time, 2),
                })
            current_speaker = speaker
            current_words = [word_entry.get("word", "").strip()]
            current_start = word_abs_time
        else:
            current_words.append(word_entry.get("word", "").strip())

    if current_words:
        sub_segments.append({
            "speaker": current_speaker,
            "text": " ".join(w for w in current_words if w).strip(),
            "start_time": round(current_start, 2),
            "end_time": round(chunk_end, 2),
        })

    logger.debug(
        "split_chunk: chunk=[%.1f,%.1f] → %d sub-segment(s) (%d words)",
        chunk_start, chunk_end, len(sub_segments), len(stt_words),
    )
    return sub_segments


# ── 1d: Speaker name normalisation ───────────────────────────────────────────

def _names_match(a: str, b: str, threshold: float = 0.85) -> bool:
    """
    True if two name strings likely refer to the same person.
    Handles abbreviated last names: "Rahul S." ≈ "Rahul Sharma".
    """
    a_n = a.lower().strip().rstrip(".")
    b_n = b.lower().strip().rstrip(".")
    if a_n == b_n:
        return True
    if difflib.SequenceMatcher(None, a_n, b_n).ratio() >= threshold:
        return True
    # Abbreviated last name: first tokens match, last token is a single initial
    a_parts = a_n.split()
    b_parts = b_n.split()
    if len(a_parts) >= 2 and len(b_parts) >= 2 and a_parts[0] == b_parts[0]:
        if len(a_parts[-1]) == 1 and b_parts[-1].startswith(a_parts[-1]):
            return True
        if len(b_parts[-1]) == 1 and a_parts[-1].startswith(b_parts[-1]):
            return True
    return False


def build_alias_map(dom_participants: list) -> dict:
    """
    Build a {variant → canonical} mapping from the raw DOM participant list.

    Groups fuzzy-matching names (threshold 0.85 or abbreviated last name)
    and maps each variant to the longest/fullest form in its group.

    Example: {"Rahul S.": "Rahul Sharma", "Rahul S": "Rahul Sharma"}
    """
    seen = list(dict.fromkeys(p.strip() for p in dom_participants if p.strip()))
    # Sort longest-first so the fullest form becomes the group representative
    ordered = sorted(seen, key=len, reverse=True)

    groups: list = []
    for name in ordered:
        placed = False
        for group in groups:
            if _names_match(name, group[0]):
                group.append(name)
                placed = True
                break
        if not placed:
            groups.append([name])

    alias_map: dict = {}
    for group in groups:
        canonical = group[0]
        for variant in group[1:]:
            if variant != canonical:
                alias_map[variant] = canonical
                logger.info("Alias map: %r → %r", variant, canonical)
    return alias_map


def normalize_speaker_name(name: Optional[str], alias_map: dict) -> Optional[str]:
    """Return the canonical name from alias_map, or the original if no entry."""
    if not name:
        return name
    return alias_map.get(name, name)
