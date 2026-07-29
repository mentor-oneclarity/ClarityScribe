#!/usr/bin/env bash
set -euo pipefail

# ── Detect python binary ───────────────────────────────────────────────────────
PYTHON=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)
if [ -z "$PYTHON" ]; then
    echo "[startup] ERROR: python3 not found."
    exit 1
fi
echo "[startup] Using Python: $($PYTHON --version)"

# ── Load .env (local dev only — Docker/Azure injects env vars directly) ───────
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
    echo "[startup] .env loaded"
fi

# ── Required directories ───────────────────────────────────────────────────────
mkdir -p uploads bot/bot_browser_profile
echo "[startup] Directories ready"

# ── Restore bot Google session from CI/CD secret ──────────────────────────────
if [ -n "${BOT_SESSION_B64:-}" ]; then
    echo "$BOT_SESSION_B64" | base64 -d > bot/session_state.json
    echo "[startup] Bot session restored from BOT_SESSION_B64"
fi

# ── Restore Google Calendar token from CI/CD secret ───────────────────────────
if [ -n "${CALENDAR_TOKEN_B64:-}" ]; then
    mkdir -p bot
    echo "$CALENDAR_TOKEN_B64" | base64 -d > bot/calendar_token.json
    echo "[startup] Calendar token restored from CALENDAR_TOKEN_B64"
fi

# ── Virtual audio device (PulseAudio) ────────────────────────────────────────
# MeetingSink  = Chrome's audio OUTPUT → all participants' audio lands here.
# SilentMic    = Chrome's audio INPUT  → silent, so bot never sends noise to Meet.
# ffmpeg reads from MeetingSink.monitor to capture all speakers for STT.
if command -v pulseaudio &>/dev/null; then
    pulseaudio --start --exit-idle-time=-1 --log-level=error 2>/dev/null || true
    sleep 1

    # Meeting audio output sink (captured by ffmpeg for STT)
    pactl load-module module-null-sink sink_name=MeetingSink \
        sink_properties=device.description=MeetingSink 2>/dev/null || true
    pactl set-default-sink MeetingSink 2>/dev/null || true

    # Silent mic source — Chrome's mic input in headful mode; keeps bot muted cleanly
    pactl load-module module-null-sink sink_name=SilentMic \
        sink_properties=device.description=SilentMic 2>/dev/null || true
    pactl set-default-source SilentMic.monitor 2>/dev/null || true

    echo "[startup] PulseAudio ready: MeetingSink (output→STT) + SilentMic (bot mic, silent)"

    # ── Diagnostic: verify PulseAudio sinks/sources exist ─────────────────
    echo "[startup:diag] PulseAudio sinks:"
    pactl list sinks short 2>/dev/null || echo "[startup:diag] WARNING: pactl list sinks failed"
    echo "[startup:diag] PulseAudio sources:"
    pactl list sources short 2>/dev/null || echo "[startup:diag] WARNING: pactl list sources failed"
    echo "[startup:diag] Default sink:   $(pactl get-default-sink 2>/dev/null || echo UNKNOWN)"
    echo "[startup:diag] Default source: $(pactl get-default-source 2>/dev/null || echo UNKNOWN)"

    # Quick silence test: 2s ffmpeg capture from MeetingSink.monitor → should produce a WAV file
    # If size < 8000 bytes it means no audio device is working (silence only = expected pre-Chrome)
    timeout 4 ffmpeg -y -f pulse -i MeetingSink.monitor -t 2 -ar 16000 -ac 1 /tmp/pulse_test.wav \
        -loglevel error 2>/dev/null && \
        echo "[startup:diag] ffmpeg pulse capture OK — $(stat -c%s /tmp/pulse_test.wav 2>/dev/null || echo 0) bytes (silence expected until Chrome joins)" || \
        echo "[startup:diag] WARNING: ffmpeg could not capture from MeetingSink.monitor — audio pipeline may be broken"
    rm -f /tmp/pulse_test.wav
else
    echo "[startup:diag] WARNING: pulseaudio not found — audio STT will not work in headful mode"
fi

# ── Virtual display (Xvfb) — required when HEADLESS=false ────────────────────
# Chrome headful + Xvfb routes incoming Meet audio to PulseAudio/MeetingSink,
# so ALL speakers are captured (not just active speaker via WebRTC fake device).
# When HEADLESS=true, Xvfb is skipped and the WebRTC JS interceptor handles audio.
echo "[startup:diag] HEADLESS=${HEADLESS:-not_set}"
if [ "${HEADLESS:-true}" != "true" ] && command -v Xvfb &>/dev/null; then
    # Kill stale Xvfb and remove stale lock (container restart leaves /tmp/.X99-lock)
    pkill -f "Xvfb :99" 2>/dev/null || true
    rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
    sleep 0.3

    Xvfb :99 -screen 0 1280x720x24 -ac +extension GLX +render -noreset &
    XVFB_PID=$!
    sleep 2

    if kill -0 "$XVFB_PID" 2>/dev/null; then
        export DISPLAY=:99
        echo "[startup] Xvfb started on :99 — Chrome headful mode, ALL-SPEAKER audio capture enabled"
        echo "[startup:diag] DISPLAY=$DISPLAY  Xvfb PID=$XVFB_PID"
        # Verify X11 socket was created
        ls -la /tmp/.X11-unix/ 2>/dev/null && echo "[startup:diag] X11 socket OK" || echo "[startup:diag] WARNING: X11 socket missing"
    else
        echo "[startup] WARNING: Xvfb failed to start — falling back to headless mode (only active speaker captured)"
        export HEADLESS=true
        echo "[startup:diag] Xvfb failure — check: apt list --installed 2>/dev/null | grep xvfb"
    fi
elif [ "${HEADLESS:-true}" = "true" ]; then
    echo "[startup] Headless mode — WebRTC JS interceptor handles audio (active speaker only)"
else
    echo "[startup:diag] WARNING: Xvfb binary not found — install xvfb package"
    export HEADLESS=true
fi

echo "[startup:diag] Final HEADLESS=${HEADLESS}"
echo "[startup:diag] Final DISPLAY=${DISPLAY:-not_set}"

# ── Init database ──────────────────────────────────────────────────────────────
echo "[startup] Initialising database ..."
"$PYTHON" setup_db.py

# ── Start FastAPI ──────────────────────────────────────────────────────────────
HOST=${HOST:-0.0.0.0}
PORT=${PORT:-6050}
echo "[startup] Starting FastAPI on ${HOST}:${PORT} ..."
exec "$PYTHON" -m uvicorn main:app \
    --host "${HOST}" \
    --port "${PORT}" \
    --workers 1 \
    --loop asyncio
