# Deployment Guide

This guide covers running the bot locally for development, running it with Docker
Compose, and deploying it to a production host.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Local Development](#local-development)
- [Streamlit Dashboard](#streamlit-dashboard)
- [Docker Compose](#docker-compose)
- [Environment Variables](#environment-variables)
- [Database Setup](#database-setup)
- [Production Deployment](#production-deployment)
- [Google Calendar Auto-Join (Optional)](#google-calendar-auto-join-optional)
- [Security Checklist](#security-checklist)
- [Monitoring & Logs](#monitoring--logs)

---

## Prerequisites

**System requirements**

- Python 3.11 (see `.python-version`)
- OS: Linux, macOS, or Windows (Docker recommended for production; Chromium/Playwright
  need a few native libraries — the `Dockerfile` installs all of them)
- ~2 GB RAM minimum, more if you plan to run several bots concurrently
- Outbound internet access to Google Meet, your STT provider, and your LLM provider

**Credentials you'll need**

| Purpose | Where to get it |
|---|---|
| Bot's Google account (to join meetings) | Any Google account; authenticate once with `python -m bot.login_once`, or skip entirely with guest mode |
| Speech-to-text | [Sarvam AI](https://dashboard.sarvam.ai) API key (or swap in your own STT client) |
| Meeting summaries | Azure OpenAI resource + deployment (or adapt `helpers/summarizer.py` for plain OpenAI) |
| Email delivery (optional) | AWS SES credentials |
| Calendar auto-join (optional) | A Google Cloud OAuth client (Desktop app type) |

---

## Local Development

```bash
# 1. Clone and enter the repo
git clone <repo-url> && cd AI_Corp_Meeting_Transcriber_6050

# 2. Create a virtual environment
python -m venv venv
source venv/bin/activate       # macOS/Linux
# venv\Scripts\activate        # Windows

# 3. Install dependencies (installs Playwright's Python bindings too)
pip install -r requirements.txt
playwright install chromium

# 4. Configure environment
cp .env.example .env
# edit .env — see "Environment Variables" below

# 5. One-time: authenticate the bot's Google account
python -m bot.login_once
# Opens a real Chrome window — sign in, then close it. Session is saved to
# bot/session_state.json. Skip this step entirely if you set BOT_GUEST_MODE=true.

# 6. Start the backend
bash startup.sh
# Runs setup_db.py (creates tables) then starts uvicorn on http://localhost:6050

# 7. In another terminal, send a bot to a meeting
python -m bot.bot_runner https://meet.google.com/abc-defg-hij
```

For local dev, leave `DB_HOST` empty in `.env` — `core/database.py` will fall back to a
local SQLite file at `uploads/local_meetings.db` automatically. Set `HEADLESS=false` if
you want to watch the bot's browser window while debugging.

The API is interactive at `http://localhost:6050/docs` (FastAPI's built-in Swagger UI).

---

## Streamlit Dashboard

A Python-only control-center UI lives in `streamlit_app/` — no Node/npm required. It
talks to the FastAPI backend purely over HTTP from the Streamlit server process (never
from the browser), so there are no CORS concerns and it can run on a completely
different machine from the backend.

```bash
pip install -r streamlit_app/requirements.txt
streamlit run streamlit_app/app.py
# Opens at http://localhost:8501
```

Point it at a remote backend by setting `BACKEND_URL` (and `BOT_API_KEY` if the backend
enforces one — see [Environment Variables](#environment-variables)) before launching, or
by editing the "Backend connection" fields in the sidebar at runtime:

```bash
BACKEND_URL=https://your-backend.example.com BOT_API_KEY=your-key streamlit run streamlit_app/app.py
```

| Page | Purpose |
|---|---|
| Home | At-a-glance backend / auth / calendar / bot health |
| Bot Control | Join a meeting by URL, view/stop active bots, view join-failure screenshots |
| Meetings | Browse past & active meetings — live-ish transcript (auto-refresh), participants, speaker stats, and one-click AI summary generation |
| Google Login | Authenticate the bot's Google account — either a visible Chrome window (local dev) or a remote screenshot-driven flow (headless/Docker deployments with no display) |

```
streamlit_app/
├── app.py                    # Entry point — st.navigation() router
├── requirements.txt
├── .streamlit/config.toml    # Dark theme
├── lib/
│   ├── api_client.py         # BackendClient — thin requests wrapper over the REST API
│   └── common.py             # Shared sidebar (backend URL / API key config)
└── views/
    ├── home.py
    ├── bot_control.py
    ├── meetings.py
    └── google_login.py
```

---

## Docker Compose

The included `docker-compose.yml` brings up the backend, MySQL, Redis (reserved for
future use), and phpMyAdmin (DB inspection UI).

```bash
# 1. Configure environment
cp .env.example .env
# fill in your values — see "Environment Variables" below

# 2. Build and run
docker-compose up --build

# 3. Verify
curl http://localhost:6050/health
```

- Backend: `http://localhost:6050` (dashboard + REST API + `/docs`)
- phpMyAdmin: `http://localhost:8080`
- MySQL: `localhost:3306` (also reachable from the backend container as host `mysql`)

Notes on the compose file:

- `DB_HOST` is hardcoded to the compose service name `mysql` inside `backend` — the
  `.env` value for `DB_HOST` is ignored when running via Compose.
- `HEADLESS` controls the audio-capture strategy, not just window visibility: `true`
  uses a headless Chromium with a fake media device and a WebRTC JS interceptor that
  only captures the currently active speaker; `false` runs headful Chromium under Xvfb
  with PulseAudio routing so `ffmpeg` captures **all** speakers, not just the active
  one. `startup.sh` sets up the `MeetingSink` (audio output → STT) and `SilentMic`
  (bot's own mic, kept silent) PulseAudio devices either way.
- `REQUIRE_MYSQL=true` is set by default — the backend will refuse to silently fall
  back to SQLite if MySQL is unreachable (a stale, container-local SQLite DB would
  silently lose data on every restart).

Useful commands:

```bash
docker-compose logs -f backend      # tail backend logs
docker-compose restart backend      # restart just the backend
docker-compose down -v              # stop and remove volumes (deletes DB data!)
docker-compose exec backend python -c "..."   # run one-off code in the container
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in your own values — never commit the real
`.env` file. The table below documents what each variable does; `.env.example` lists
the placeholders.

| Variable | Required | Notes |
|---|---|---|
| `APP_URL` | No | Base URL used in summary emails ("View Transcript" link) |
| `LOG_LEVEL` | No | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOCAL_MODE` | No | Local-dev convenience flag |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | Yes (prod) | MySQL connection. Leave `DB_HOST` empty to use local SQLite instead |
| `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT_NAME` / `AZURE_OPENAI_API_VERSION` | Yes | Powers `helpers/summarizer.py` (meeting summaries) |
| `BOT_DISPLAY_NAME` | No | Name the bot shows in the meeting (default: `Meeting Bot`) |
| `BOT_GOOGLE_EMAIL` / `BOT_GOOGLE_PASSWORD` | No | One of three auth options — see below |
| `BOT_GUEST_MODE` | No | `true` = bot joins as an anonymous guest, no Google account needed (host must admit it) |
| `BOT_SESSION_B64` | No | Base64 of `bot/session_state.json`, used to restore a saved login in Docker/CI where there's no browser to log in with |
| `BOT_MAX_CONCURRENT` | No | Max simultaneous bots (default: `4`, RAM-bound) |
| `HEADLESS` | No | `true` for servers, `false` to watch the browser locally — see Docker Compose notes above |
| `AUDIO_DEVICE_INDEX` | No | Local microphone index (dev only) |
| `PULSE_SINK` / `PULSE_SOURCE` | No | PulseAudio device names used by `ffmpeg` in headful/Xvfb mode |
| `CALENDAR_POLL_SEC` / `CALENDAR_LOOKAHEAD_MINUTES` | No | Google Calendar auto-join polling — see [below](#google-calendar-auto-join-optional) |
| `DEFAULT_ORGANIZER_EMAIL` | No | Fallback "summary to" address when a meeting has none set |
| `AWS_SES_ACCESS_KEY_ID` / `AWS_SES_SECRET_ACCESS_KEY` / `AWS_SES_REGION` / `SES_FROM` | No | Enables emailing the AI summary via AWS SES |

**Additional variables read directly from the environment** (not yet listed in
`.env.example`, but real and used by the code — add them to your own `.env` if you need
them):

| Variable | Used by | Notes |
|---|---|---|
| `SARVAM_API_KEY` | `bot/utils/sarvam_client.py` | Required for speech-to-text. Without it, STT is silently disabled and only Google's live captions are used |
| `STT_MODEL` | `bot/utils/sarvam_client.py` | Sarvam model name, default `saaras:v3` |
| `SARVAM_LANGUAGE` | `bot/utils/sarvam_client.py` | Default `unknown` (auto-detect) |
| `AUDIO_CHUNK_SEC` | Docker Compose default `5` | Audio chunk size sent to STT |
| `ALLOWED_ORIGINS` | `main.py` | Comma-separated CORS allow-list. Leave unset only for local dev — it defaults to `*` |
| `BOT_API_KEY` | `api/routes.py`, Streamlit `lib/common.py` | If set, all bot-control endpoints require an `X-API-Key` header matching this value |
| `REQUIRE_MYSQL` | `core/database.py` | `true` fails startup loudly instead of silently falling back to SQLite when MySQL is unreachable — recommended in production |
| `AZURE_MYSQL_SSL_NO_VERIFY` | `core/database.py` | Only for self-signed MySQL certs; leave `false` in production |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `core/database.py` | SQLAlchemy connection pool tuning (defaults `5` / `10`) |
| `CALENDAR_TOKEN_B64` | `startup.sh` | Base64 of `bot/calendar_token.json`, restores calendar auth in Docker/CI |

**Bot authentication — pick one:**

1. **Guest mode** (simplest): `BOT_GUEST_MODE=true`. The bot joins anonymously; the
   meeting host has to admit it from the waiting room.
2. **Saved session** (recommended for servers): run `python -m bot.login_once` locally,
   then `base64 -i bot/session_state.json` and put the result in `BOT_SESSION_B64`.
3. **Email + password**: `BOT_GOOGLE_EMAIL` / `BOT_GOOGLE_PASSWORD`. Use an
   app-specific password, never the account's real password. Least recommended —
   Google may flag automated logins from new locations.

---

## Database Setup

**SQLite (local dev, default)** — used automatically whenever `DB_HOST` is empty.
Nothing to configure; the file lives at `uploads/local_meetings.db`.

**MySQL (production)** — set `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`.
Any of the following work:

- The `mysql` service already defined in `docker-compose.yml`
- A managed MySQL instance (e.g. Azure Database for MySQL) — `core/database.py`
  automatically enables TLS when the hostname contains `azure.com`
- A self-managed MySQL 8.0 server

Tables are created automatically by `setup_db.py`, which both `startup.sh` and the
Docker image run on every boot. It's idempotent (`CREATE TABLE IF NOT EXISTS`), so it's
safe to run repeatedly:

```bash
python setup_db.py
```

`mysql-init.sql` (run automatically by the MySQL container on first start) grants the
`appuser` account full privileges on the application database.

To wipe all data while keeping the schema intact:

```bash
python flush_db.py --confirm
```

See [docs/database.md](database.md) for the full schema reference, including the
`ai_meeting_master` reporting table.

---

## Production Deployment

The project ships with a single `Dockerfile` (multi-stage: build stage installs
dependencies and bakes in the Chromium browser; runtime stage runs as a non-root user)
and a health check at `/health`. Any container host that can run a Docker image and
expose port 6050 will work. Two common paths:

### Option 1 — Docker Compose on a VM

```bash
git clone <repo-url> && cd AI_Corp_Meeting_Transcriber_6050
cp .env.example .env   # fill in production values
docker-compose up -d
docker-compose ps
curl http://localhost:6050/health
```

Put a reverse proxy (nginx, Caddy, or your cloud provider's load balancer) in front for
TLS termination — Google Workspace Add-ons and browser clients calling the API over
CORS both require HTTPS in production.

### Option 2 — Any container registry + host

```bash
# Build and push
docker build -t your-registry/meeting-bot:latest .
docker push your-registry/meeting-bot:latest

# Run (example — adapt to your platform's env-var mechanism)
docker run -d -p 6050:6050 \
  --env-file .env.prod \
  -v meeting_bot_logs:/app/logs \
  your-registry/meeting-bot:latest
```

Whatever platform you use, make sure the container has:

- `HEADLESS=true` unless you've deliberately set up Xvfb (the Dockerfile already
  includes Xvfb and PulseAudio, so headful mode works out of the box in this image)
- A reachable MySQL instance and `REQUIRE_MYSQL=true`
- HTTPS in front of it if you're publishing the [Google Meet Add-on](google-meet-addon.md)
  or calling the API from a browser

### Backups

```bash
# Backup
docker exec <mysql-container> mysqldump -u appuser -p ai_meeting_db > backup_$(date +%Y%m%d).sql

# Restore
docker exec -i <mysql-container> mysql -u appuser -p ai_meeting_db < backup_20260101.sql
```

---

## Google Calendar Auto-Join (Optional)

`main.py` runs a background loop that polls Google Calendar and automatically sends the
bot to any upcoming Google Meet event a few minutes before it starts — no manual
"send bot" click needed.

1. Create an OAuth client in the [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
   → **Create Credentials → OAuth 2.0 Client ID** → Application type **Desktop app**.
2. Download the JSON and save it as `bot/calendar_credentials.json`.
3. Authenticate once locally:
   ```bash
   python -m bot.calendar_auth
   # Opens a browser → sign in → approve Calendar access
   # Creates bot/calendar_token.json
   ```
4. For Docker/CI deployments (no local browser), base64-encode the token and pass it as
   `CALENDAR_TOKEN_B64`; `startup.sh` decodes it back to `bot/calendar_token.json` on boot.

`calendar_credentials.json` and `calendar_token.json` are already in `.gitignore` —
never commit them. Tune polling with `CALENDAR_POLL_SEC` (default 60s) and
`CALENDAR_LOOKAHEAD_MINUTES` (how far ahead to look for upcoming meetings).

---

## Security Checklist

- [ ] `.env` is not committed (check `.gitignore`)
- [ ] Database password is strong and different from any dev/test password
- [ ] `ALLOWED_ORIGINS` is set to specific domains, not left as `*`, in production
- [ ] `REQUIRE_MYSQL=true` in production, so a DB outage fails loudly instead of
      silently switching to a throwaway local SQLite file
- [ ] `BOT_API_KEY` is set if the API is reachable from outside your own network
- [ ] HTTPS is terminated in front of the backend (required for the Meet Add-on)
- [ ] Secrets are stored in your platform's secret manager, not baked into the image
- [ ] Backups are automated and periodically test-restored

---

## Monitoring & Logs

```bash
# Local file logs (rotated)
tail -f logs/*.log

# Docker
docker-compose logs -f backend
docker logs <container-name>

# Health check
curl http://localhost:6050/health
# {"status":"ok","timestamp":"..."}
```

For issues with bots not joining, empty transcripts, or misattributed speakers, see
[docs/troubleshooting.md](troubleshooting.md).
