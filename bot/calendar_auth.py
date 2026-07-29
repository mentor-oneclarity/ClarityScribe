"""
One-time Google Calendar OAuth2 setup.

    python -m bot.calendar_auth

Pre-requisite — download OAuth credentials from Google Cloud Console:
  1. https://console.cloud.google.com
  2. Enable the Google Calendar API for your project
  3. APIs & Services → Credentials → Create OAuth 2.0 Client ID
  4. Application type: Desktop app
  5. Download JSON and save as: bot/calendar_credentials.json
  6. Run this script
"""

import sys
from pathlib import Path

CREDS_FILE = Path(__file__).parent / "calendar_credentials.json"
TOKEN_FILE = Path(__file__).parent / "calendar_token.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def main() -> None:
    if not CREDS_FILE.exists():
        print(f"""
ERROR: {CREDS_FILE} not found.

Steps:
  1. Open https://console.cloud.google.com
  2. Enable the Google Calendar API for your project
  3. APIs & Services → Credentials → Create OAuth 2.0 Client ID (Desktop app)
  4. Download the JSON → save as:
       {CREDS_FILE}
  5. Re-run this script
""")
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Missing library. Run:  pip install google-api-python-client google-auth-oauthlib")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())
    print(f"\nCalendar authenticated! Token saved to: {TOKEN_FILE}")
    print("The bot will now auto-join Google Meet events from your calendar.")


if __name__ == "__main__":
    main()
