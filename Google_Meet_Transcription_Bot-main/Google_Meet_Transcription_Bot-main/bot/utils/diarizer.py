"""
Speaker diarization via pyannote.audio 3.x.

Language-agnostic: works for any language/code-switching (Hindi+English etc.)
Pairs with Sarvam STT — pyannote splits audio into per-speaker segments,
Sarvam transcribes each segment independently.

Setup:
  1. pip install pyannote.audio torch torchaudio
  2. Accept model conditions at huggingface.co/pyannote/speaker-diarization-3.1
  3. Set HUGGINGFACE_TOKEN=hf_xxx in .env

CPU: ~0.3-0.5x realtime (8s chunk → ~3s processing) — acceptable
GPU: ~5-10x realtime
"""

import logging
import os
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger("bot")

_SAMPLERATE = 16_000
_MIN_SEGMENT_SEC = 0.4  # drop segments shorter than this (too short for STT)
_MODEL_ID = "pyannote/speaker-diarization-3.1"


class SpeakerDiarizer:
    """
    Lazy-loads pyannote pipeline on first call to .load().
    Thread-safe: diarize() blocks (torch inference) — always call via run_in_executor.
    """

    def __init__(self) -> None:
        self._pipeline = None
        self._attempted = False
        self._device_str = "cpu"

    @property
    def available(self) -> bool:
        return self._pipeline is not None

    def load(self) -> bool:
        if self._attempted:
            return self.available
        self._attempted = True

        token = os.getenv("HUGGINGFACE_TOKEN", "").strip()
        if not token:
            logger.warning(
                "Diarizer: HUGGINGFACE_TOKEN not set — pyannote disabled. "
                "Set it in .env to enable per-speaker diarization."
            )
            return False

        try:
            from pyannote.audio import Pipeline  # type: ignore
            import torch  # type: ignore

            logger.info("Diarizer: loading %s …", _MODEL_ID)
            self._pipeline = Pipeline.from_pretrained(_MODEL_ID, use_auth_token=token)
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            self._pipeline.to(device)
            self._device_str = str(device)
            logger.info("Diarizer: pyannote ready on %s", self._device_str)
            return True

        except ImportError:
            logger.warning(
                "Diarizer: pyannote.audio not installed. "
                "Run: pip install pyannote.audio torch torchaudio"
            )
        except Exception as exc:
            logger.error("Diarizer: failed to load pipeline: %s", exc, exc_info=True)

        return False

    def diarize(self, pcm: np.ndarray, sample_rate: int = _SAMPLERATE) -> List[Dict]:
        """
        Synchronous (blocking) diarization. Call via run_in_executor.

        Args:
            pcm: int16 numpy array (mono, from sounddevice)
            sample_rate: samples per second

        Returns:
            List of {"speaker": "SPEAKER_00", "start": 0.5, "end": 2.3}
            Sorted by start time. Segments shorter than _MIN_SEGMENT_SEC are dropped.
        """
        if not self.available:
            return []

        try:
            import torch  # type: ignore

            # int16 → float32 normalised to [-1, 1], shape (1, samples)
            audio_f32 = pcm.flatten().astype(np.float32) / 32768.0
            waveform = torch.from_numpy(audio_f32).unsqueeze(0)

            diarization = self._pipeline(
                {"waveform": waveform, "sample_rate": sample_rate}
            )

            segments: List[Dict] = []
            for turn, _, speaker in diarization.itertracks(yield_label=True):
                duration = turn.end - turn.start
                if duration < _MIN_SEGMENT_SEC:
                    continue
                segments.append(
                    {
                        "speaker": speaker,   # e.g. "SPEAKER_00"
                        "start": round(turn.start, 3),
                        "end": round(turn.end, 3),
                    }
                )

            logger.debug(
                "Diarizer: %d segment(s) from %.1fs audio  speakers=%s",
                len(segments),
                len(pcm) / sample_rate,
                list({s["speaker"] for s in segments}),
            )
            return segments

        except Exception as exc:
            logger.error("Diarizer: diarization error: %s", exc, exc_info=True)
            return []

    def extract_pcm(
        self, pcm: np.ndarray, start: float, end: float, sample_rate: int = _SAMPLERATE
    ) -> np.ndarray:
        """Slice a PCM array to the [start, end] time window (seconds)."""
        s = int(start * sample_rate)
        e = int(end * sample_rate)
        return pcm[s:e]


# Module-level singleton
diarizer = SpeakerDiarizer()
