import streamlit as st

from lib.api_client import APIError
from lib.common import init_client

client = init_client()

st.title("🎙️ AI Meeting Bot — Control Center")
st.caption("Joins Google Meet calls, captures transcripts, and generates AI-powered summaries.")

col1, col2, col3, col4 = st.columns(4)

with col1:
    try:
        client.health()
        st.metric("Backend", "Online 🟢")
    except APIError:
        st.metric("Backend", "Offline 🔴")

with col2:
    try:
        auth = client.auth_status()
        ok = auth["authenticated"] or auth["auth_detail"] in ("session", "guest")
        st.metric("Google Auth", "Ready 🟢" if ok else "Not logged in 🔴")
    except APIError:
        st.metric("Google Auth", "Unknown ⚪")

with col3:
    try:
        cal = client.calendar_status()
        st.metric("Calendar", "Connected 🟢" if cal["configured"] else "Not set up ⚪")
    except APIError:
        st.metric("Calendar", "Unknown ⚪")

with col4:
    try:
        bots = client.bot_status()
        running = sum(1 for s in bots.values() if s == "running")
        st.metric("Active Bots", running)
    except APIError:
        st.metric("Active Bots", "—")

st.divider()

st.markdown(
    """
    ### Getting started

    1. **🔐 Google Login** — authenticate the bot's Google account (one-time setup).
    2. **🤖 Bot Control** — paste a Google Meet link to send the bot into a meeting.
    3. **📋 Meetings** — watch live transcripts and generate AI summaries.

    Use the sidebar to navigate between pages. If the backend is unreachable, check
    the **Backend URL** in the sidebar — it must point at your running FastAPI server
    (default `http://localhost:6050`).
    """
)
