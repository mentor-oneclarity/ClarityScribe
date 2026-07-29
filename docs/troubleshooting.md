# Troubleshooting

Common problems encountered running the bot, with likely causes and fixes. See also
[docs/deployment.md](deployment.md) for environment variable reference and
[docs/architecture.md](architecture.md) for how the pieces fit together.

## Common Errors & Fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| `POST /bot/join` returns 403 | Bot isn't authenticated and guest mode is off | Set `BOT_GUEST_MODE=true`, or provide a saved session (`BOT_SESSION_B64`) / credentials |
| `POST /bot/join` returns 409 | A bot is already running for that meeting code | Wait for it to finish, or stop it with `DELETE /bot/{meet_code}/stop` |
| Bot process exits with no clear error | Chromium crashed | If `HEADLESS=false`, confirm Xvfb is running; check container logs for PulseAudio/X11 setup messages from `startup.sh` |
| No transcript appears at all | SSE stream not reaching the client, or no segments being created | See [Transcript Not Showing](#transcript-not-showing) below |
| Transcript appears but every line says the same speaker (or "Unknown") | Captions aren't being scraped, so speaker resolution falls through to a weaker strategy | See [Speaker Detection Issues](#speaker-detection-issues) below |
| Speech-to-text never produces segments | `SARVAM_API_KEY` missing, or `ffmpeg` unavailable | See [Audio Capture / STT Issues](#audio-capture--stt-issues) below |
| `POST /bot/join` accepted but the bot never appears in the meeting | Meeting not started yet, or the bot is stuck retrying | The bot retries joining for several minutes — check logs for `[4/6]` join-attempt messages; confirm the meeting is actually live |
| Database errors on startup | MySQL unreachable or credentials wrong | See [Database Connection Errors](#database-connection-errors) below |

---

## Bot Not Joining a Meeting

When "Send Bot" doesn't result in the bot appearing in the meeting, check these in
order:

1. **Authentication.** Exactly one of `BOT_GUEST_MODE=true`, `BOT_SESSION_B64`, or
   `BOT_GOOGLE_EMAIL`/`BOT_GOOGLE_PASSWORD` should be configured. If none are set, the
   bot has no way to join.
2. **Headless + no display.** In Docker, `HEADLESS=true` runs Chromium with a fake
   media device and no display server requirement — this is the simplest production
   configuration. Running `HEADLESS=false` in a container requires Xvfb (already
   installed in the provided `Dockerfile`/`startup.sh`) — if Xvfb fails to start,
   `startup.sh` logs a warning and falls back to headless mode automatically.
3. **Google security checks.** A new session logging in from an unfamiliar location
   or IP can trigger Google's automated-login protections, which can't be bypassed
   without manual interaction. Guest mode sidesteps this entirely; a saved session
   (`BOT_SESSION_B64`) is the next most reliable option.
4. **Meeting not active yet.** The bot retries joining for several minutes if the
   meeting hasn't started. Check the logs for join-attempt messages
   (`[4/6] FAILED to join meeting after N attempts`) to see whether it's actively
   retrying or has given up.

**Creating a saved session for production:**

```bash
# 1. Run the bot locally and log in successfully once
python -m bot.login_once

# 2. Encode the resulting session file
base64 -i bot/session_state.json > session_b64.txt

# 3. Put the contents in your production .env
BOT_SESSION_B64=<paste contents of session_b64.txt>
```

---

## Transcript Not Showing

The live transcript is streamed over SSE at `GET /meetings/{id}/transcript/live`
(implemented in `api/routes.py`). If nothing shows up on the dashboard:

1. **Confirm the SSE connection opens at all.** From a terminal:
   ```bash
   curl -N http://localhost:6050/meetings/1/transcript/live
   ```
   You should see a stream of `event: transcript` / `data: {...}` lines, or at least
   periodic `ping` keep-alives.
2. **Confirm segments are actually being created.** Check `GET /meetings/{id}/transcript`
   (the plain REST version) — if it's empty, the problem is upstream of the SSE layer
   (the bot isn't producing segments), not the streaming itself.
3. **Manually post a test segment** to isolate frontend vs. backend issues:
   ```bash
   curl -X POST http://localhost:6050/meetings/1/transcript \
     -H "Content-Type: application/json" \
     -d '{"speaker_name": "Alice", "text": "Test segment", "source": "manual", "start_time": 0, "end_time": 3}'
   ```
   If this appears in the SSE stream and dashboard, the pipeline works — the issue is
   upstream in the bot's capture loops.
4. **Check CORS.** If the dashboard is served from a different origin than the
   backend, confirm that origin is included in `ALLOWED_ORIGINS`.

---

## Speaker Detection Issues

Speaker attribution uses a layered strategy (see
[docs/architecture.md](architecture.md#speaker-attribution-pipeline)): WebRTC track
metadata, active-speaker DOM polling, caption timeline matching, and a track-assignment
cache as a last resort. If speaker names are consistently wrong or missing:

1. **Confirm captions are enabled in the meeting.** The bot relies on Google's live
   captions as one of its strongest signals. In the meeting, the "CC" button must be
   on. Without it, the caption-based strategies can't run at all.
2. **Confirm the caption observer is actually attached.** Look for a log line like
   `Caption MutationObserver re-injected into live page`. If it's missing, the
   observer never attached to the DOM.
3. **Check the caption CSS selectors still match Google's markup.** Google
   occasionally changes Meet's DOM structure, which can silently break caption
   scraping. The selectors are defined in `bot/bots/meet_bot.py`
   (`CAPTION_SELECTORS`, currently including `.a4cQT`, `.iTTPOb`, and a few others). If
   captions have stopped working entirely after a Meet UI change, this is the first
   place to check.
4. **Large meetings degrade attribution accuracy.** This is an inherent limitation of
   the current heuristics, not a bug — see the accuracy table in
   [docs/architecture.md](architecture.md#speaker-attribution-pipeline).

---

## Audio Capture / STT Issues

```bash
# ffmpeg must be installed (already included in the provided Dockerfile)
ffmpeg -version

# Confirm the Sarvam API key is set
echo "$SARVAM_API_KEY" | wc -c   # should not be empty

# Check bot logs for STT-related lines
grep -i "sarvam\|audio\|ffmpeg" logs/bot.log
```

If `SARVAM_API_KEY` isn't set, speech-to-text is silently disabled and the bot falls
back to Google's live captions only — this is by design (see
`bot/utils/sarvam_client.py`), not a crash. Set the key if you want STT-driven
transcription in addition to captions.

---

## Database Connection Errors

```bash
# Quick connectivity check from inside the container / host
python - <<'EOF'
import asyncio, os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

async def check():
    engine = create_async_engine(os.environ["DATABASE_URL"] if os.getenv("DATABASE_URL") else "mysql+aiomysql://user:pass@host:3306/db")
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
        print("Connected")

asyncio.run(check())
EOF
```

- Confirm `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` are correct.
- In Docker Compose, `DB_HOST` is hardcoded to the service name `mysql` for the
  `backend` container — a value from `.env` is ignored there.
- If `REQUIRE_MYSQL=true` and MySQL is unreachable, startup fails loudly (by design)
  instead of silently falling back to a local SQLite file that would lose data on
  every container restart. Check firewall rules / network reachability first.
- Managed MySQL instances with self-signed certificates need
  `AZURE_MYSQL_SSL_NO_VERIFY=true` (or your provider's equivalent) — only do this if
  you understand the security tradeoff.

---

## General Diagnostic Checklist

- [ ] `GET /health` returns `200 {"status": "ok"}`
- [ ] `GET /bot/auth-status` shows the bot is authenticated (or guest mode is enabled)
- [ ] `GET /bot/status` shows the bot as running for the expected meeting code
- [ ] `GET /meetings/{id}/transcript` is non-empty a minute or two into the meeting
- [ ] Logs show `BOT ACTIVE — meeting_id=...` after a successful join
- [ ] `ALLOWED_ORIGINS` includes whatever origin your dashboard is served from
- [ ] Recent logs (`logs/*.log` locally, or `docker logs <container>`) don't show
      repeated errors around STT, database, or the join flow

If you're stuck, gather the Python version, OS, the exact error/stack trace, and
recent logs before filing an issue — that's almost always what's needed to diagnose it
quickly.
