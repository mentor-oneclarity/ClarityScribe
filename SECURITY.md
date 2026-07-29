# Security Policy

## Reporting a Vulnerability

If you find a security vulnerability in this project, please report it privately —
**do not open a public GitHub issue**.

Email **connect@oneclarity.ai** with:

- A description of the vulnerability and its potential impact
- Steps to reproduce (a minimal repro is ideal)
- The affected version/commit

You should receive an acknowledgment within **3 business days**. We'll work with you
to understand and address the issue, and will credit you in the fix's release notes
unless you'd prefer to stay anonymous.

Please give us a reasonable amount of time to fix the issue before any public
disclosure.

## Supported Versions

This project doesn't yet follow a formal release/versioning schedule — security fixes
land on `main`. Always run the latest commit.

## Scope

In scope:

- The FastAPI backend (`api/`, `core/`, `helpers/`)
- The Playwright bot (`bot/`)
- The Streamlit dashboard (`streamlit_app/`)
- The Docker/Compose deployment configuration

Out of scope:

- Vulnerabilities in third-party dependencies — please report those upstream (though
  we'd appreciate a heads-up so we can pin a patched version)
- Social engineering, physical attacks, or denial-of-service reports against
  infrastructure you don't own

## Hardening Checklist for Self-Hosters

This bot authenticates to a real Google account and can send email on your behalf.
Before exposing it beyond `localhost`:

- **Set `BOT_API_KEY`.** Without it, every bot-control endpoint (`/bot/join`,
  `/bot/login/*`, `/debug/db`, etc.) is open to anyone who can reach the server.
- **Never hardcode credentials.** `BOT_GOOGLE_EMAIL` / `BOT_GOOGLE_PASSWORD` /
  `BOT_SESSION_B64` belong in your secret manager or CI/CD secrets — never in source
  control. See [docs/deployment.md](docs/deployment.md#security-checklist).
- **Restrict `ALLOWED_ORIGINS`** to your actual frontend origin(s) in production
  instead of leaving CORS wide open.
- **Put the backend behind TLS** (reverse proxy / load balancer) if it's reachable
  over the public internet — the API key and session cookies deserve an encrypted
  transport.
- **Rotate the bot's Google account password** periodically, and immediately if you
  suspect `BOT_SESSION_B64` or `session_state.json` was ever exposed.
