"""
Sarvam AI — Speech-to-Text client
Primary model: saaras:v3 (transcription + language detection)
Fallback: configurable via STT_MODEL env var
"""

import asyncio
import os
import time as _time
from typing import Optional

import httpx
from dotenv import load_dotenv

from .logger import get_logger

load_dotenv()

logger = get_logger("stt")

_SARVAM_URL = "https://api.sarvam.ai/speech-to-text"

_LANGUAGE_CODE = os.getenv("SARVAM_LANGUAGE", os.getenv("STT_LANGUAGE_CODE", "unknown"))
_MODEL = os.getenv("STT_MODEL", "saaras:v3")
_STT_PROVIDER = os.getenv("STT_PROVIDER", "sarvam")
_STT_MODE = os.getenv("STT_MODE", "transcribe")


class SarvamClient:
    def __init__(self) -> None:
        self.api_key: str = os.getenv("SARVAM_API_KEY", "")
        if not self.api_key:
            logger.warning("SarvamClient: SARVAM_API_KEY not set — STT will be disabled")
        else:
            logger.info(
                "SarvamClient: initialized (provider=%s model=%s lang=%s mode=%s)",
                _STT_PROVIDER, _MODEL, _LANGUAGE_CODE, _STT_MODE,
            )
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)
        )
        # Circuit breaker: stop all calls after HTTP 402 (no credits).
        self._credits_exhausted: bool = False

    _MAX_RETRIES = 3
    _RETRY_BACKOFF = [2.0, 4.0]

    async def transcribe(
        self, wav_bytes: bytes, num_speakers: int = 2
    ) -> Optional[dict]:
        if not self.api_key:
            logger.warning("Sarvam: SARVAM_API_KEY not set — STT skipped")
            return None

        if self._credits_exhausted:
            logger.debug("Sarvam: credits exhausted (HTTP 402 seen) — skipping chunk")
            return None

        if len(wav_bytes) < 4096:
            logger.debug("Sarvam: skipping tiny chunk (%d bytes < 4096)", len(wav_bytes))
            return None

        t0 = _time.time()
        logger.info(
            "Sarvam: sending %.1f KB to STT (provider=%s model=%s lang=%s) …",
            len(wav_bytes) / 1024, _STT_PROVIDER, _MODEL, _LANGUAGE_CODE,
        )

        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                resp = await self._http.post(
                    _SARVAM_URL,
                    headers={"api-subscription-key": self.api_key},
                    files={"file": ("chunk.wav", wav_bytes, "audio/wav")},
                    data={
                        "model": _MODEL,
                        "language_code": _LANGUAGE_CODE,
                        "with_timestamps": "true",
                        "with_disfluences": "false",
                        # Speaker attribution handled by caption-first pipeline in meet_bot.py
                        "with_diarization": "false",
                    },
                )
                elapsed = _time.time() - t0
                logger.info(
                    "Sarvam: response HTTP %d in %.2fs (attempt %d)",
                    resp.status_code, elapsed, attempt,
                )

                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    if resp.status_code == 402:
                        self._credits_exhausted = True
                        logger.error(
                            "Sarvam HTTP 402 — NO CREDITS. Top up at https://dashboard.sarvam.ai "
                            "All future STT calls are paused until credits are added and container restarted."
                        )
                    else:
                        logger.error(
                            "Sarvam HTTP %d (not retrying) — %s",
                            resp.status_code, resp.text[:500],
                        )
                    return None

                resp.raise_for_status()
                data = resp.json()

                transcript = (data.get("transcript") or "").strip()
                lang = data.get("language_code", "unknown")

                # saaras:v3 may return confidence at segment level
                confidence: Optional[float] = None
                segments = data.get("word_timestamps") or data.get("segments") or []
                if segments and isinstance(segments, list):
                    confs = [
                        s.get("confidence") for s in segments
                        if isinstance(s, dict) and s.get("confidence") is not None
                    ]
                    if confs:
                        confidence = round(sum(confs) / len(confs), 3)

                if not transcript:
                    logger.info("Sarvam: empty transcript (silence or noise) in %.2fs", elapsed)
                    return None

                logger.info(
                    "Sarvam [%s] model=%s conf=%s (%.2fs): %s",
                    lang, _MODEL, confidence, elapsed, transcript[:120],
                )
                return {
                    "transcript": transcript,
                    "language_code": lang,
                    "confidence": confidence,
                    "provider": _STT_PROVIDER,
                    "model": _MODEL,
                    "segments": segments,
                }

            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "Sarvam HTTP %s attempt %d/%d",
                    exc.response.status_code, attempt, self._MAX_RETRIES,
                )
            except httpx.TimeoutException:
                logger.warning(
                    "Sarvam TIMEOUT attempt %d/%d (%.2fs elapsed)",
                    attempt, self._MAX_RETRIES, _time.time() - t0,
                )
            except (httpx.ConnectError, httpx.NetworkError) as exc:
                logger.warning(
                    "Sarvam network error attempt %d/%d: %s",
                    attempt, self._MAX_RETRIES, exc,
                )
            except Exception as exc:
                logger.error(
                    "Sarvam UNEXPECTED ERROR attempt %d/%d: %s",
                    attempt, self._MAX_RETRIES, exc, exc_info=True,
                )
                return None

            if attempt < self._MAX_RETRIES:
                delay = self._RETRY_BACKOFF[attempt - 1]
                logger.info("Sarvam: retrying in %.1fs …", delay)
                await asyncio.sleep(delay)

        logger.error("Sarvam: all %d attempts failed — dropping chunk", self._MAX_RETRIES)
        return None

    async def close(self) -> None:
        await self._http.aclose()
