"""App configuration.

Each setting is read from an environment variable (locally loaded from .env), falling back to
Streamlit secrets (.streamlit/secrets.toml locally, the Secrets box on Streamlit Community Cloud).
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _secret(name: str) -> str | None:
    try:
        import streamlit as st

        value = st.secrets.get(name)
    except Exception:  # no secrets file, or not running under Streamlit
        return None
    return None if value is None else str(value)


def _get(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        value = _secret(name)
    return (default if value is None else value).strip()


# --- App password, required on every visit. The app refuses to open while it is empty. ---
APP_PASSWORD = _get("APP_PASSWORD")

# --- AI provider: claude | gemini | openai ---
AI_PROVIDER = _get("AI_PROVIDER", "claude").lower()

ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = _get("CLAUDE_MODEL", "claude-opus-5")

GEMINI_API_KEY = _get("GEMINI_API_KEY")
GEMINI_MODEL = _get("GEMINI_MODEL", "gemini-3.6-flash")

OPENAI_API_KEY = _get("OPENAI_API_KEY")
OPENAI_MODEL = _get("OPENAI_MODEL", "gpt-5")

# --- Google OAuth (Drive API) ---
GOOGLE_CREDENTIALS_FILE = BASE_DIR / _get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_TOKEN_FILE = BASE_DIR / _get("GOOGLE_TOKEN_FILE", "token.json")
# Contents of token.json, for hosts without a persistent disk (Streamlit Community Cloud).
# When set it replaces the token file and the Login screen.
GOOGLE_TOKEN_JSON = _get("GOOGLE_TOKEN_JSON")

# --- Time zone for visit clock-ins (the server itself may run in UTC) ---
APP_TIMEZONE = _get("APP_TIMEZONE", "Asia/Kolkata")

PROMPTS_DIR = BASE_DIR / "prompts"


def ai_model() -> str:
    """Model name for the configured provider."""
    return {"claude": CLAUDE_MODEL, "gemini": GEMINI_MODEL, "openai": OPENAI_MODEL}.get(AI_PROVIDER, "")
