# Contributing

Thanks for considering a contribution. This project is a FastAPI backend + Playwright
bot + Streamlit dashboard — see [docs/architecture.md](docs/architecture.md) for how
the pieces fit together before diving into a change.

By participating, you're expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
Found a security issue instead of a bug? Please see [SECURITY.md](SECURITY.md) instead
of opening a public issue.

## Before you start

- Check [docs/troubleshooting.md](docs/troubleshooting.md) — many "bot won't join" or
  "no transcript" reports come down to a missing environment variable, not a bug.
- For anything non-trivial, open an issue first to discuss the approach before writing
  code — it saves rework on both sides.

## Development setup

```bash
git clone <your-fork-url> && cd <repo>
python -m venv venv && source venv/bin/activate   # venv\Scripts\activate on Windows
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# fill in the env vars you need — see docs/deployment.md#environment-variables
```

Install the dev tooling and git hooks:

```bash
pip install ruff black pre-commit
pre-commit install
```

Run the backend locally:

```bash
python -m bot.login_once      # one-time Google login (skip if BOT_GUEST_MODE=true)
bash startup.sh                # starts the backend on http://localhost:6050
```

## Code style

- Formatted with [black](https://github.com/psf/black), linted with
  [ruff](https://github.com/astral-sh/ruff) — both run automatically via
  `pre-commit`, and in CI on every pull request. Run them manually with:

  ```bash
  ruff check .
  black --check .
  ```

- Match the existing style in the file you're editing over any personal preference.
- Keep comments to *why*, not *what* — the code should already say what it does.
- No unrelated refactors bundled into a bug-fix or feature PR — keep diffs focused and
  reviewable.

## Testing your change

There's no automated test suite yet (see [docs/troubleshooting.md](docs/troubleshooting.md)
for manual verification steps for common flows: joining a meeting, transcript capture,
summary generation). At minimum, before opening a PR:

1. Confirm the backend starts cleanly (`bash startup.sh` or `docker compose up --build`).
2. Exercise the code path you changed end-to-end (e.g. actually join a test Meet call if
   you touched `bot/`).
3. Run `ruff check .` and `black --check .` with no errors.

If you're adding a new feature, a short manual test plan in the PR description is
genuinely useful for reviewers — screenshots or terminal output included.

## Commit messages

Keep the first line short and in the imperative mood ("Fix speaker attribution race
condition", not "Fixed" or "Fixes"). Explain *why* in the body when the reason isn't
obvious from the diff.

## Pull requests

- Keep PRs scoped to one logical change.
- Reference the issue it closes, if any (`Closes #123`).
- Update relevant docs (`docs/`, `README.md`) in the same PR if behavior or
  configuration changes.
- A maintainer will review and may ask for changes — that's normal, not a rejection.

## Reporting bugs / requesting features

Use the issue templates — they ask for the environment details and repro steps that
speed up triage (Docker vs local, `HEADLESS` setting, relevant log lines).
