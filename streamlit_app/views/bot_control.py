import base64

import streamlit as st

from lib.api_client import APIError
from lib.common import init_client

client = init_client()

st.title("🤖 Bot Control")

# ── Join a meeting ────────────────────────────────────────────────────────────

st.subheader("Join a meeting")
with st.form("join_form", clear_on_submit=False):
    meet_url = st.text_input("Google Meet URL", placeholder="https://meet.google.com/abc-defg-hij")
    col1, col2 = st.columns(2)
    with col1:
        title = st.text_input("Meeting title (optional)")
        organizer_email = st.text_input("Organizer email (optional)", help="Summary will be sent TO this address.")
    with col2:
        attendee_emails = st.text_input(
            "Attendee emails (optional)",
            help="Comma-separated. Summary will be BCC'd to these addresses.",
        )
    submitted = st.form_submit_button("🚀 Join meeting", type="primary")

if submitted:
    if not meet_url.strip():
        st.error("Enter a Google Meet URL first.")
    else:
        try:
            result = client.join_bot(meet_url.strip(), title.strip(), organizer_email.strip(), attendee_emails.strip())
            st.success(f"Bot launching for meeting **{result['meet_code']}** — status: {result['status']}")
        except APIError as exc:
            st.error(f"Could not start bot: {exc}")

st.divider()

# ── Active bots ────────────────────────────────────────────────────────────────

st.subheader("Active bots")
if st.button("🔄 Refresh"):
    st.rerun()

try:
    bots = client.bot_status()
except APIError as exc:
    bots = {}
    st.error(f"Could not fetch bot status: {exc}")

if not bots:
    st.info("No bots have run yet in this backend session.")
else:
    for meet_code, status in bots.items():
        row = st.columns([3, 2, 2])
        row[0].markdown(f"**{meet_code}**")
        badge = {"running": "🟢 running", "done": "⚪ done", "cancelled": "🟡 cancelled", "error": "🔴 error"}
        row[1].markdown(badge.get(status, status))
        if status == "running":
            if row[2].button("Stop", key=f"stop_{meet_code}"):
                try:
                    client.stop_bot(meet_code)
                    st.success(f"Stop requested for {meet_code}")
                    st.rerun()
                except APIError as exc:
                    st.error(f"Could not stop bot: {exc}")
        else:
            row[2].caption("—")

st.divider()

# ── Failure screenshot viewer ─────────────────────────────────────────────────

st.subheader("Debug: bot join-failure screenshot")
fail_code = st.text_input("Meeting code", key="fail_code", placeholder="abc-defg-hij")
if st.button("Load screenshot"):
    if not fail_code.strip():
        st.error("Enter a meeting code first.")
    else:
        try:
            resp = client.fail_screenshot(fail_code.strip())
            st.image(base64.b64decode(resp["screenshot"]), caption=resp["path"])
        except APIError as exc:
            st.error(f"No screenshot found: {exc}")
