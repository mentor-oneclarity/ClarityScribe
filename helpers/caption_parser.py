import re
from typing import Optional, Tuple

# Matches "Speaker Name: caption text"
_CAPTION_RE = re.compile(r"^([A-Za-z][A-Za-z\s.\-']{0,49}):\s+(.+)$", re.DOTALL)


def parse_caption(raw: str) -> Tuple[Optional[str], str]:
    """
    Split a raw caption string into (speaker_name, text).

    Returns (None, raw) when no speaker prefix is detected.
    """
    raw = raw.strip()
    if not raw:
        return None, ""

    match = _CAPTION_RE.match(raw)
    if match:
        speaker = match.group(1).strip()
        text = match.group(2).strip()
        if len(speaker.split()) <= 5:
            return speaker, text

    return None, raw
