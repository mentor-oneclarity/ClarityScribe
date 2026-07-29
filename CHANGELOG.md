# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once a first tagged release is cut.

## [Unreleased]

### Security
- All bot-control endpoints (including the remote browser-login flow and
  `/debug/db`) now consistently require `BOT_API_KEY` when it's configured.
- API key comparison uses a constant-time check (`hmac.compare_digest`).
- The optional cross-table user lookup in `push-summary` no longer hardcodes a
  specific schema — table/column names are configurable via env vars and the
  lookup is disabled by default.

## [0.1.0] - 2026-07-29

Initial public release.

### Added
- FastAPI backend for meeting lifecycle, transcript storage, and live SSE streaming.
- Playwright-driven bot that joins Google Meet calls, captures live captions and
  audio, and attributes speech to speakers via a layered strategy.
- Optional Sarvam AI speech-to-text integration for multilingual accuracy.
- AI-generated meeting summaries via Azure OpenAI.
- Optional summary email delivery via AWS SES.
- Streamlit control dashboard for sending/stopping bots and browsing transcripts.
- Google Calendar auto-join polling.
- Docker Compose stack (backend, MySQL, Redis, phpMyAdmin) for local/self-hosted use.
