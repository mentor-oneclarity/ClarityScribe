import streamlit as st

st.set_page_config(page_title="AI Meeting Bot", page_icon="🎙️", layout="wide")

pages = {
    "Overview": [
        st.Page("views/home.py", title="Home", icon="🏠", default=True),
    ],
    "Control": [
        st.Page("views/bot_control.py", title="Bot Control", icon="🤖"),
        st.Page("views/meetings.py", title="Meetings", icon="📋"),
        st.Page("views/google_login.py", title="Google Login", icon="🔐"),
    ],
}

st.navigation(pages).run()
