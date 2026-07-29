"""Shared sidebar (backend connection config) used by every view."""

import os

import streamlit as st
from dotenv import load_dotenv

from lib.api_client import APIError, BackendClient

load_dotenv()

_DEFAULT_BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:6050")
_DEFAULT_API_KEY = os.getenv("BOT_API_KEY", "")


def init_client() -> BackendClient:
    """Renders the sidebar connection panel and returns a configured client."""
    st.session_state.setdefault("backend_url", _DEFAULT_BACKEND_URL)
    st.session_state.setdefault("api_key", _DEFAULT_API_KEY)

    with st.sidebar:
        st.markdown("### ⚙️ Backend connection")
        st.text_input("Backend URL", key="backend_url")
        st.text_input(
            "API key (optional)", key="api_key", type="password",
            help="Only needed if BOT_API_KEY is set on the backend.",
        )

        client = BackendClient(st.session_state.backend_url, st.session_state.api_key)
        try:
            client.health()
            st.success("Backend reachable", icon="🟢")
        except APIError as exc:
            st.error(f"Backend unreachable\n\n{exc}", icon="🔴")

        st.divider()

    return client
