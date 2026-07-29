import time

import streamlit as st

from lib.api_client import APIError
from lib.common import init_client

client = init_client()

st.title("📋 Meetings")

try:
    meetings = client.list_meetings()
except APIError as exc:
    st.error(f"Could not load meetings: {exc}")
    st.stop()

if not meetings:
    st.info("No meetings yet — join one from the Bot Control page.")
    st.stop()

options = {
    f"#{m['id']} — {m['title'] or m['meet_code']} ({m['status']})": m["id"]
    for m in meetings
}
choice = st.selectbox("Select a meeting", list(options.keys()))
meeting_id = options[choice]
meeting = next(m for m in meetings if m["id"] == meeting_id)

# ── Metadata ───────────────────────────────────────────────────────────────────

col1, col2, col3 = st.columns(3)
col1.metric("Status", meeting["status"])
col2.metric("Meet code", meeting["meet_code"])
col3.metric("Started", meeting["created_at"][:16].replace("T", " "))

if meeting.get("organizer_email") or meeting.get("attendee_emails"):
    st.caption(
        f"Organizer: {meeting.get('organizer_email') or '—'}  |  "
        f"Attendees: {meeting.get('attendee_emails') or '—'}"
    )

st.divider()

tab_transcript, tab_participants, tab_stats, tab_summary = st.tabs(
    ["📝 Transcript", "👥 Participants", "📊 Speaker Stats", "🧾 Summary"]
)

# ── Transcript ─────────────────────────────────────────────────────────────────

with tab_transcript:
    auto_refresh = st.checkbox(
        "Auto-refresh every 5s", value=(meeting["status"] == "active"), key="tr_auto_refresh"
    )
    try:
        segments = client.get_transcript(meeting_id)
    except APIError as exc:
        segments = []
        st.error(f"Could not load transcript: {exc}")

    if not segments:
        st.info("No transcript segments yet.")
    else:
        transcript_txt = "\n".join(f"{s['speaker_name'] or 'Unknown'}: {s['text']}" for s in segments)
        st.download_button(
            "⬇️ Download transcript (.txt)", transcript_txt,
            file_name=f"transcript_{meeting['meet_code']}.txt",
        )
        for s in segments:
            with st.chat_message("user" if s["source"] == "caption" else "assistant"):
                lang = f" [{s['language_code']}]" if s.get("language_code") and s["language_code"] != "en" else ""
                st.markdown(f"**{s['speaker_name'] or 'Unknown'}**{lang}: {s['text']}")
                if s.get("translated_text"):
                    st.caption(f"Translated: {s['translated_text']}")

    if auto_refresh and meeting["status"] == "active":
        time.sleep(5)
        st.rerun()

# ── Participants ───────────────────────────────────────────────────────────────

with tab_participants:
    try:
        participants = client.get_participants(meeting_id)
    except APIError as exc:
        participants = []
        st.error(f"Could not load participants: {exc}")

    if not participants:
        st.info("No participants recorded.")
    else:
        st.dataframe(participants, use_container_width=True, hide_index=True)

# ── Speaker stats ──────────────────────────────────────────────────────────────

with tab_stats:
    try:
        stats = client.get_speaker_stats(meeting_id)
    except APIError as exc:
        stats = []
        st.error(f"Could not load speaker stats: {exc}")

    if not stats:
        st.info("No speaker stats yet.")
    else:
        st.dataframe(stats, use_container_width=True, hide_index=True)
        st.bar_chart({s["name"]: s["total_duration"] for s in stats})

# ── Summary ────────────────────────────────────────────────────────────────────

with tab_summary:
    if meeting.get("summary"):
        st.markdown(meeting["summary"])
    else:
        st.info("No summary generated yet.")

    extra_bcc = st.text_input(
        "Extra BCC recipients (optional, comma-separated)", key=f"bcc_{meeting_id}"
    )
    if st.button("✨ Generate / Regenerate Summary", type="primary"):
        with st.spinner("Generating summary via Azure OpenAI — this can take a few minutes for long meetings…"):
            try:
                result = client.summarize(meeting_id, extra_bcc.strip())
                st.success("Summary generated.")
                st.markdown(result["summary"])
            except APIError as exc:
                st.error(f"Summary failed: {exc}")
