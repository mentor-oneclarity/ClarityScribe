"""Thin HTTP client for the FastAPI backend — all calls happen server-side in the
Streamlit process, so the browser never talks to the backend directly (no CORS)."""

import requests


class APIError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class BackendClient:
    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._session = requests.Session()

    def _headers(self) -> dict:
        return {"X-API-Key": self.api_key} if self.api_key else {}

    def _request(self, method: str, path: str, timeout: float = 15, **kwargs):
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.request(method, url, headers=self._headers(), timeout=timeout, **kwargs)
        except requests.exceptions.RequestException as exc:
            raise APIError(f"Could not reach backend at {self.base_url} — {exc}") from exc

        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise APIError(str(detail), status_code=resp.status_code)

        return resp.json() if resp.content else None

    # ── Health / status ────────────────────────────────────────────────────────

    def health(self) -> dict:
        return self._request("GET", "/health")

    def auth_status(self) -> dict:
        return self._request("GET", "/bot/auth-status")

    def calendar_status(self) -> dict:
        return self._request("GET", "/calendar/status", timeout=15)

    # ── Bot control ────────────────────────────────────────────────────────────

    def bot_status(self) -> dict:
        return self._request("GET", "/bot/status")

    def join_bot(self, meet_url: str, title: str = "", organizer_email: str = "", attendee_emails: str = "") -> dict:
        payload = {"meet_url": meet_url}
        if title:
            payload["title"] = title
        if organizer_email:
            payload["organizer_email"] = organizer_email
        if attendee_emails:
            payload["attendee_emails"] = attendee_emails
        return self._request("POST", "/bot/join", json=payload)

    def stop_bot(self, meet_code: str) -> dict:
        return self._request("DELETE", f"/bot/{meet_code}/stop")

    def fail_screenshot(self, meet_code: str) -> dict:
        return self._request("GET", f"/bot/{meet_code}/fail-screenshot")

    # ── Meetings ───────────────────────────────────────────────────────────────

    def list_meetings(self) -> list:
        return self._request("GET", "/meetings")

    def get_meeting(self, meeting_id: int) -> dict:
        return self._request("GET", f"/meetings/{meeting_id}")

    def get_transcript(self, meeting_id: int) -> list:
        return self._request("GET", f"/meetings/{meeting_id}/transcript")

    def get_participants(self, meeting_id: int) -> list:
        return self._request("GET", f"/meetings/{meeting_id}/participants")

    def get_speaker_stats(self, meeting_id: int) -> list:
        return self._request("GET", f"/meetings/{meeting_id}/speaker-stats")

    def summarize(self, meeting_id: int, attendee_emails: str = "") -> dict:
        payload = {"attendee_emails": attendee_emails} if attendee_emails else {}
        return self._request("POST", f"/meetings/{meeting_id}/summarize", json=payload, timeout=300)

    # ── Google login ──────────────────────────────────────────────────────────

    def login_open(self) -> dict:
        return self._request("POST", "/bot/login/open")

    def login_open_status(self) -> dict:
        return self._request("GET", "/bot/login/status")

    def login_start(self) -> dict:
        return self._request("POST", "/bot/login/start", timeout=30)

    def login_screenshot(self) -> dict:
        return self._request("GET", "/bot/login/screenshot", timeout=30)

    def login_click(self, x: int, y: int) -> dict:
        return self._request("POST", "/bot/login/click", json={"x": x, "y": y}, timeout=30)

    def login_type(self, text: str) -> dict:
        return self._request("POST", "/bot/login/type", json={"text": text}, timeout=30)

    def login_key(self, key: str) -> dict:
        return self._request("POST", "/bot/login/key", json={"key": key}, timeout=30)

    def login_navigate(self, url: str) -> dict:
        return self._request("POST", "/bot/login/navigate", json={"url": url}, timeout=30)

    def login_done(self) -> dict:
        return self._request("POST", "/bot/login/done", timeout=30)

    def login_abort(self) -> dict:
        return self._request("POST", "/bot/login/abort", timeout=30)
