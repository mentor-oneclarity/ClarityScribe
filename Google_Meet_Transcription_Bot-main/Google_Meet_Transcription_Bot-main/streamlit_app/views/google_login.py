import base64

import streamlit as st
from streamlit_image_coordinates import streamlit_image_coordinates

from lib.api_client import APIError
from lib.common import init_client

client = init_client()

st.title("🔐 Google Login")

try:
    status = client.auth_status()
    st.info(status["message"])
    st.caption(f"Detail: {status['auth_detail']}  |  Bot account: {status.get('email') or '—'}")
except APIError as exc:
    st.error(f"Could not fetch auth status: {exc}")

st.divider()

tab_visible, tab_remote = st.tabs(["🖥️ Visible browser (local machine)", "📡 Remote screenshot login (headless / Docker)"])

# ── Visible browser flow — only works if the backend host has a display ──────

with tab_visible:
    st.markdown(
        "Opens a real Chrome window **on the machine running the backend**. "
        "Log in with the bot's Google account, then close the window — the "
        "session is saved automatically. Only works when the backend is *not* "
        "running with `HEADLESS=true`."
    )
    if st.button("Open visible Chrome window"):
        try:
            result = client.login_open()
            if result.get("status") == "headless":
                st.warning(result["message"])
            else:
                st.success(result["message"])
        except APIError as exc:
            st.error(f"Could not open login browser: {exc}")

    if st.button("Check if login window is still open"):
        try:
            st.write(client.login_open_status())
        except APIError as exc:
            st.error(f"Could not check status: {exc}")

# ── Remote screenshot-driven flow — works anywhere, incl. Docker ─────────────

with tab_remote:
    st.markdown(
        "Drives a hidden Chrome window on the backend via screenshots — click, type, "
        "and press keys just like a normal browser. Use this when the backend runs "
        "headless (e.g. in Docker) and there's no local display to open a real window."
    )

    def _refresh_screenshot():
        shot = client.login_screenshot()
        st.session_state.login_shot = shot["screenshot"]
        st.session_state.login_url = shot["url"]

    if not st.session_state.get("login_active"):
        if st.button("▶️ Start remote login session", type="primary"):
            try:
                result = client.login_start()
                st.session_state.login_active = True
                st.session_state.login_shot = result["screenshot"]
                st.session_state.login_url = result["url"]
                st.session_state.pop("_last_login_click", None)
                st.rerun()
            except APIError as exc:
                st.error(f"Could not start login session: {exc}")
    else:
        st.caption(f"Current URL: {st.session_state.get('login_url', '')}")

        img_bytes = base64.b64decode(st.session_state.login_shot)
        coords = streamlit_image_coordinates(img_bytes, key="login_click_img")
        if coords and coords != st.session_state.get("_last_login_click"):
            st.session_state._last_login_click = coords
            try:
                client.login_click(coords["x"], coords["y"])
                _refresh_screenshot()
                st.rerun()
            except APIError as exc:
                st.error(f"Click failed: {exc}")

        col1, col2 = st.columns(2)

        with col1:
            type_text = st.text_input("Text to type into the focused field")
            if st.button("⌨️ Type"):
                try:
                    client.login_type(type_text)
                    _refresh_screenshot()
                    st.rerun()
                except APIError as exc:
                    st.error(f"Type failed: {exc}")

            key_choice = st.selectbox("Key to press", ["Enter", "Tab", "Backspace", "Escape", "ArrowDown", "ArrowUp"])
            if st.button("🔘 Press key"):
                try:
                    client.login_key(key_choice)
                    _refresh_screenshot()
                    st.rerun()
                except APIError as exc:
                    st.error(f"Key press failed: {exc}")

        with col2:
            nav_url = st.text_input("Navigate to URL", value="https://accounts.google.com/ServiceLogin")
            if st.button("🧭 Navigate"):
                try:
                    client.login_navigate(nav_url)
                    _refresh_screenshot()
                    st.rerun()
                except APIError as exc:
                    st.error(f"Navigation failed: {exc}")

            if st.button("🔄 Refresh screenshot"):
                try:
                    _refresh_screenshot()
                    st.rerun()
                except APIError as exc:
                    st.error(f"Refresh failed: {exc}")

        st.divider()
        col_done, col_abort = st.columns(2)
        with col_done:
            if st.button("✅ Done — save session", type="primary"):
                try:
                    result = client.login_done()
                    st.session_state.login_active = False
                    st.success(f"Session saved. Authenticated: {result['authenticated']}")
                    st.rerun()
                except APIError as exc:
                    st.error(f"Could not save session: {exc}")
        with col_abort:
            if st.button("❌ Abort (discard session)"):
                try:
                    client.login_abort()
                    st.session_state.login_active = False
                    st.info("Login session aborted — nothing saved.")
                    st.rerun()
                except APIError as exc:
                    st.error(f"Could not abort session: {exc}")
