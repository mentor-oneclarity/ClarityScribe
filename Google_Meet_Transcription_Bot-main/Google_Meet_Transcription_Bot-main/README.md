# 🚀 AI Meeting Bot

An open-source bot that joins Google Meet calls, transcribes them in real time, and
generates AI-powered meeting summaries — with a FastAPI backend, a Streamlit control
dashboard, and a Playwright-driven browser bot.

## Features

- **Joins Google Meet automatically** via Playwright + Chromium — no manual clicking
- **Live transcription** from Google's captions, optionally combined with Sarvam AI
  speech-to-text for better multilingual accuracy (Hindi, Hinglish, English, and more)
- **Speaker attribution** using a layered strategy (WebRTC metadata, active-speaker
  detection, caption timing, and a fallback cache)
- **AI-generated summaries** via Azure OpenAI, length-adjusted to the meeting duration
- **Optional email delivery** of the summary to the organizer and attendees via AWS SES
- **Live dashboard** — a Streamlit app to send/stop bots, watch the transcript stream
  in, and browse meeting history
- **Google Calendar auto-join** — optionally polls a calendar and joins upcoming Meet
  events without any manual trigger
- **Google Workspace Add-on** — an optional in-Meet sidebar for one-click start/stop

## Quick Start

### Option 1 — Docker Compose (recommended)

```bash
git clone <repo-url> && cd AI_Corp_Meeting_Transcriber_6050
cp .env.example .env
# edit .env with your API keys — see docs/deployment.md#environment-variables

docker-compose up --build
```

The dashboard and API are now at `http://localhost:6050` (interactive API docs at
`/docs`). phpMyAdmin (DB inspection) is at `http://localhost:8080`.

### Option 2 — Run locally

```bash
git clone <repo-url> && cd AI_Corp_Meeting_Transcriber_6050
python -m venv venv && source venv/bin/activate   # venv\Scripts\activate on Windows
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# edit .env — leave DB_HOST empty to use local SQLite instead of MySQL

python -m bot.login_once      # one-time Google login (skip if BOT_GUEST_MODE=true)
bash startup.sh                # starts the backend on http://localhost:6050
```

In another terminal, send a bot to a meeting:

```bash
python -m bot.bot_runner https://meet.google.com/abc-defg-hij
```

Or run the Streamlit dashboard instead of the CLI:

```bash
pip install -r streamlit_app/requirements.txt
streamlit run streamlit_app/app.py   # http://localhost:8501
```

See [docs/deployment.md](docs/deployment.md) for full setup details, environment
variables, and production deployment options.

## Architecture at a Glance

A FastAPI backend orchestrates one or more Playwright-driven Chromium bots, each
joining a Google Meet call, scraping live captions and audio, and posting transcript
segments back to the backend over HTTP. The backend stores everything in MySQL (or
SQLite for local dev), streams the live transcript to clients over Server-Sent Events,
and generates AI summaries via Azure OpenAI once a meeting ends.

For the full breakdown — component map, database schema, API reference, and the
speaker-attribution pipeline — see **[docs/architecture.md](docs/architecture.md)**.

## Project Structure

```
AI_Corp_Meeting_Transcriber_6050/
├── main.py                  # FastAPI app entry point
├── API/                     # REST + SSE routes and request/response models
├── core/                    # Bot lifecycle manager, database layer, logging
├── bot/                     # Playwright bot: join flow, caption/audio capture, CLI runner
│   ├── bots/meet_bot.py     # Core bot logic
│   └── utils/               # STT client, WebRTC audio capture, speaker alignment
├── helpers/                 # AI summarization, email delivery, translation
├── streamlit_app/           # Streamlit control dashboard
├── docs/                    # Documentation (this file links out to all of it)
├── Dockerfile                # Multi-stage build (Playwright + Chromium baked in)
├── docker-compose.yml         # Backend + MySQL + Redis + phpMyAdmin
├── requirements.txt
├── setup_db.py               # Creates database tables (idempotent)
├── flush_db.py               # Clears table data (destructive, requires confirmation)
├── startup.sh                 # Container/production entrypoint
└── .env.example
```

## Documentation

| Doc | Covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | System design, component reference, database schema, API reference, speaker attribution |
| [docs/deployment.md](docs/deployment.md) | Local dev, Docker Compose, environment variables, production deployment |
| [docs/google-meet-addon.md](docs/google-meet-addon.md) | Publishing the bot as an in-Meet Google Workspace Add-on |
| [docs/database.md](docs/database.md) | The `ai_meeting_master` denormalized reporting table |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Common errors, join failures, transcript/speaker issues |

## Known Limitations

Speaker attribution accuracy drops in larger meetings (roughly 85-90% for 2-5
participants, down to 40-55% for 20+), and the bot's saved Google session needs
periodic re-authentication. See
[docs/architecture.md#known-limitations--roadmap](docs/architecture.md#known-limitations--roadmap)
for details and planned improvements.

## Contributing

Issues and pull requests are welcome. Before opening one, please check
[docs/troubleshooting.md](docs/troubleshooting.md) — many "bot won't join" or "no
transcript" reports come down to a missing environment variable.

## License

No license file is currently included in this repository — add one (e.g. MIT,
Apache-2.0) appropriate for your intended use before publishing or accepting external
contributions.
