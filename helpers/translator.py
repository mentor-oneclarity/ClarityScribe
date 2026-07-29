"""
Async background translation — runs AFTER transcript segment is saved.
Translates non-English text to English using Azure OpenAI.
Updates translated_text + translated_language_code in DB without touching original.
"""

import asyncio
import logging

logger = logging.getLogger("backend")

# Languages we skip translation for (already English or close enough)
_SKIP_LANGS = {"en", "en-IN", "en-US", "en-GB", "en-AU", "unknown", "", None}


async def translate_segment(segment_id: int, text: str, language_code: str) -> None:
    """
    Background coroutine: translate `text` to English and update the DB row.
    Called via asyncio.create_task() — never awaited by the caller directly.
    """
    if language_code in _SKIP_LANGS:
        return

    if not text or not text.strip():
        return

    try:
        from helpers.summarizer import _get_client, _get_model
        client = _get_client()
        model = _get_model()

        response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a translator. Translate the given text to English. "
                        "Return ONLY the translated text, nothing else. "
                        "If the text is already in English, return it unchanged."
                    ),
                },
                {"role": "user", "content": text},
            ],
            max_completion_tokens=500,
        )

        translated = (response.choices[0].message.content or "").strip()
        if not translated:
            return

        # Update DB directly
        from core.database import async_session, TranscriptSegment
        from sqlalchemy import select

        async with async_session() as session:
            result = await session.execute(
                select(TranscriptSegment).where(TranscriptSegment.id == segment_id)
            )
            seg = result.scalar_one_or_none()
            if seg:
                seg.translated_text = translated
                seg.translated_language_code = "en"
                await session.commit()
                logger.info(
                    "Translation done: segment_id=%d lang=%s → en (%d chars)",
                    segment_id, language_code, len(translated),
                )

    except Exception as exc:
        logger.warning(
            "Translation failed for segment_id=%d lang=%s: %s",
            segment_id, language_code, exc,
        )


def schedule_translation(segment_id: int, text: str, language_code: str) -> None:
    """Fire-and-forget translation task. Safe to call from sync or async context."""
    if language_code in _SKIP_LANGS:
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(
            translate_segment(segment_id, text, language_code),
            name=f"translate-{segment_id}",
        )
    except RuntimeError:
        pass  # no running loop — skip (e.g. during tests)
