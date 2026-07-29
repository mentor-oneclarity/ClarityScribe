# 🚀 AI Meeting Bot

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](.python-version)
[![Docker Compose ready](https://img.shields.io/badge/docker--compose-ready-2496ED.svg?logo=docker&logoColor=white)](docker-compose.yml)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Contributions welcome](https://img.shields.io/badge/contributions-welcome-brightgreen.svg)](CONTRIBUTING.md)

An open-source bot that joins Google Meet calls, transcribes them in real time, and
generates AI-powered meeting summaries — with a FastAPI backend, a Streamlit control
dashboard, and a Playwright-driven browser bot.

## Table of Contents

- [Features](#features)
- [Quick Start](#quick-start)
- [Architecture at a Glance](#architecture-at-a-glance)
- [Project Structure](#project-structure)
- [Documentation](#documentation)
- [Security](#security)
- [Known Limitations](#known-limitations)
- [Contributing](#contributing)
- [Changelog](#changelog)
- [License](#license)

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
git clone <repo-url> && cd <repo-directory>
cp .env.example .env
# edit .env with your API keys — see docs/deployment.md#environment-variables

docker compose up --build
```

The dashboard and API are now at `http://localhost:6050` (interactive API docs at
`/docs`). phpMyAdmin (DB inspection) is at `http://localhost:8080`.

### Option 2 — Run locally

```bash
git clone <repo-url> && cd <repo-directory>
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
.
├── main.py                   # FastAPI app entry point
├── api/                      # REST + SSE routes and request/response models
├── core/                     # Bot lifecycle manager, database layer, logging
├── bot/                       # Playwright bot: join flow, caption/audio capture, CLI runner
│   ├── bots/meet_bot.py      # Core bot logic
│   └── utils/                # STT client, WebRTC audio capture, speaker alignment
├── helpers/                  # AI summarization, email delivery, translation
├── streamlit_app/            # Streamlit control dashboard
├── docs/                     # Architecture, deployment, troubleshooting docs
├── .github/                   # Issue/PR templates, CI workflows, Dependabot config
├── Dockerfile                 # Multi-stage build (Playwright + Chromium baked in)
├── docker-compose.yml          # Backend + MySQL + Redis + phpMyAdmin
├── requirements.txt
├── pyproject.toml              # ruff/black tool config
├── .pre-commit-config.yaml     # Formatting, lint, and secret-scan hooks
├── setup_db.py                 # Creates database tables (idempotent)
├── flush_db.py                 # Clears table data (destructive, requires confirmation)
├── startup.sh                   # Container/production entrypoint
└── .env.example
```

## Documentation

| Doc | Covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | System design, component reference, database schema, API reference, speaker attribution |
| [docs/deployment.md](docs/deployment.md) | Local dev, Docker Compose, environment variables, production deployment, security checklist |
| [docs/google-meet-addon.md](docs/google-meet-addon.md) | Publishing the bot as an in-Meet Google Workspace Add-on |
| [docs/database.md](docs/database.md) | The `ai_meeting_master` denormalized reporting table |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Common errors, join failures, transcript/speaker issues |

## Security

This bot authenticates to a real Google account and can send email on your behalf —
please read [SECURITY.md](SECURITY.md) before deploying it anywhere beyond
`localhost`. In short: **set `BOT_API_KEY`** before exposing the backend to a network
you don't fully trust, and never commit real credentials — everything sensitive
belongs in `.env` (gitignored) or your platform's secret manager.

Found a vulnerability? Please report it privately per [SECURITY.md](SECURITY.md)
rather than opening a public issue.

## Known Limitations

Speaker attribution accuracy drops in larger meetings (roughly 85-90% for 2-5
participants, down to 40-55% for 20+), and the bot's saved Google session needs
periodic re-authentication. See
[docs/architecture.md#known-limitations--roadmap](docs/architecture.md#known-limitations--roadmap)
for details and planned improvements.

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for
dev setup, code style, and the PR process. Please note we have a
[Code of Conduct](CODE_OF_CONDUCT.md). Before opening an issue, check
[docs/troubleshooting.md](docs/troubleshooting.md) — many "bot won't join" or "no
transcript" reports come down to a missing environment variable.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release notes.

## License

Licensed under the [MIT License](LICENSE).
