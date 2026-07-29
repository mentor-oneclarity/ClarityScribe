"""
Audio capture via sounddevice.

Captures audio from a device (mic or loopback) in fixed-length chunks
and yields them as raw numpy arrays to the async consumer.
WAV conversion is handled by the caller so it can prepend context audio.

Device selection (set AUDIO_DEVICE_INDEX in .env):
  - Leave blank → default input (usually built-in mic)
  - Set to BlackHole index → captures ALL meeting audio (all participants)

Find your device index:
  python -m bot.utils.audio_capture
"""

import asyncio
import io
import os
import queue as thread_queue
import wave
from typing import AsyncIterator, Optional, Union

import logging

import numpy as np

logger = logging.getLogger("bot")

SAMPLERATE: int = 16_000            # 16 kHz — optimal for Sarvam STT
CHANNELS: int = 1                   # mono
CHUNK_SECONDS: int = int(os.getenv("AUDIO_CHUNK_SEC", "8"))
_DEVICE_ENV: str = os.getenv("AUDIO_DEVICE_INDEX", "").strip()
DEVICE_INDEX: Optional[Union[int, str]] = (
    int(_DEVICE_ENV) if _DEVICE_ENV.lstrip("-").isdigit() else (_DEVICE_ENV or None)
)


def to_wav(data: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)          # int16 = 2 bytes per sample
        wf.setframerate(SAMPLERATE)
        wf.writeframes(data.flatten().tobytes())
    return buf.getvalue()


def _rms(data: np.ndarray) -> float:
    return float(np.sqrt(np.mean(data.astype(np.float32) ** 2)))


class AudioCapturer:
    """
    Thread-safe audio capture that bridges sounddevice callbacks → asyncio.

    Usage:
        capturer = AudioCapturer()
        async for wav_bytes in capturer.chunks():
            # process chunk
    """

    def __init__(self, device: Optional[Union[int, str]] = DEVICE_INDEX) -> None:
        self.device = device
        self._q: thread_queue.Queue[np.ndarray] = thread_queue.Queue(maxsize=20)
        self._frames: list[np.ndarray] = []
        self._running = False

    # ── sounddevice callback (runs in a thread) ───────────────────────────────

    def _sd_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            pass  # ignore overflow/underflow silently
        self._frames.append(indata.copy())
        total = sum(f.shape[0] for f in self._frames)
        if total >= SAMPLERATE * CHUNK_SECONDS:
            audio = np.concatenate(self._frames, axis=0)
            self._frames = []

            # Skip near-silent chunks (RMS < 50 on int16 range 0-32768 ≈ 0.15%)
            if _rms(audio) < 50:
                return

            try:
                self._q.put_nowait(audio)
            except thread_queue.Full:
                pass  # drop if consumer is too slow

    # ── Async generator ───────────────────────────────────────────────────────

    async def chunks(self) -> AsyncIterator[np.ndarray]:
        try:
            import sounddevice as sd
        except ImportError:
            logger.warning("sounddevice not installed — audio STT disabled (pip install sounddevice)")
            return

        logger.info("Audio: requested device=%s samplerate=%d channels=%d chunk_sec=%d",
                    self.device, SAMPLERATE, CHANNELS, CHUNK_SECONDS)

        try:
            available = sd.query_devices()
            logger.info("Audio: %d device(s) available on this system", len(available))
            for i, d in enumerate(available):
                if d.get("max_input_channels", 0) > 0:
                    logger.info("  [%d] %s (inputs=%d)", i, d["name"], d["max_input_channels"])
        except Exception as exc:
            logger.warning("Audio: could not list devices: %s", exc)

        self._running = True
        try:
            stream = sd.InputStream(
                samplerate=SAMPLERATE,
                channels=CHANNELS,
                dtype="int16",
                device=self.device,
                callback=self._sd_callback,
                blocksize=SAMPLERATE // 10,
            )
            stream.start()
            logger.info("Audio: stream started on device=%s", self.device)
        except Exception as exc:
            logger.error("Audio: FAILED to open device=%s — %s", self.device, exc, exc_info=True)
            logger.error("Audio: Sarvam STT will be DISABLED for this session")
            self._running = False
            return

        chunk_count = 0
        try:
            while self._running:
                pcm = await asyncio.get_running_loop().run_in_executor(
                    None, self._blocking_get
                )
                if pcm is not None:
                    chunk_count += 1
                    logger.info("Audio: chunk #%d — samples=%d duration=%.1fs",
                                chunk_count, len(pcm), len(pcm) / SAMPLERATE)
                    yield pcm
        finally:
            stream.stop()
            stream.close()
            logger.info("Audio: stream stopped after %d chunk(s)", chunk_count)

    def _blocking_get(self) -> Optional[np.ndarray]:
        try:
            return self._q.get(timeout=1.0)
        except thread_queue.Empty:
            return None

    def stop(self) -> None:
        self._running = False


# ── CLI: list devices ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sounddevice as sd

    print("\nAvailable INPUT audio devices:")
    print("-" * 60)
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            marker = "← default" if i == sd.default.device[0] else ""
            print(f"  [{i:2d}]  {d['name']:<40} {d['max_input_channels']}ch  {marker}")

    print("\nSet AUDIO_DEVICE_INDEX=<number> in .env")
    print("For all-participant capture: install BlackHole-2ch, set it as output,")
    print("then use its device index here.\n")
