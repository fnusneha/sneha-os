"""
Google OAuth helper — the only thing Sneha.OS still uses from the old
Google Workspace stack.

Scopes (see `constants.SCOPES`):
  - Sheets — read the Travel Master Planner + cycling Library
  - Drive  — export the Habit Tracker Google Doc
  - Calendar — cycle day detection + weekly notes ("Week Agenda" card)

Token source order:
  1. `GOOGLE_TOKEN_JSON` env var  (Render web service, GitHub Actions cron)
  2. `token.json` on disk         (local Mac dev)
  3. Interactive browser login   (one-time; only works on a machine with
                                  a browser, raises in containers)

All three return a refresh-capable `Credentials` object. Headless
callers (Flask, cron) should set `GOOGLE_NO_INTERACTIVE=1` so they fail
fast with a clean error instead of hanging on a non-existent browser.
"""

import json
import logging
import os
import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

from constants import SCRIPT_DIR, SCOPES

log = logging.getLogger(__name__)

OAUTH_CREDENTIALS_FILE = SCRIPT_DIR / os.getenv(
    "OAUTH_CREDENTIALS_FILE", "credentials.json"
)
OAUTH_TOKEN_FILE = SCRIPT_DIR / "token.json"


def _load_creds_from_env() -> Credentials | None:
    """Try to construct credentials from the GOOGLE_TOKEN_JSON env var.

    Returns valid (refreshed if needed) `Credentials`, or None if the
    env var is unset / malformed / no longer refreshable.
    """
    token_blob = os.getenv("GOOGLE_TOKEN_JSON")
    if not token_blob:
        return None

    try:
        data = json.loads(token_blob)
        creds = Credentials.from_authorized_user_info(data, SCOPES)
    except Exception as exc:
        log.warning("GOOGLE_TOKEN_JSON parse failed: %s", exc)
        return None

    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            log.info("Refreshing OAuth2 token from env...")
            creds.refresh(Request())
            return creds
        except Exception as exc:
            log.warning("Token refresh from env failed: %s", exc)
    return None


def get_google_creds() -> Credentials:
    """Get OAuth2 credentials for Sheets + Drive + Calendar."""
    # 1. env var (cloud)
    env_creds = _load_creds_from_env()
    if env_creds:
        return env_creds

    # 2. token.json (local)
    creds = None
    if OAUTH_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(OAUTH_TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                log.info("Refreshing OAuth2 token...")
                creds.refresh(Request())
            except Exception as exc:
                log.warning("Token refresh failed (%s) — re-authenticating...", exc)
                creds = None

        # 3. Interactive login (LOCAL ONLY — browser + free port required)
        if not creds or not creds.valid:
            if os.getenv("GOOGLE_NO_INTERACTIVE") == "1":
                raise RuntimeError(
                    "Google OAuth token missing/expired and interactive login "
                    "is disabled. Set GOOGLE_TOKEN_JSON env var."
                )
            if not OAUTH_CREDENTIALS_FILE.exists():
                log.error("OAuth credentials file not found: %s", OAUTH_CREDENTIALS_FILE)
                log.error("Download it from GCP Console → APIs → Credentials → OAuth 2.0 Client IDs")
                sys.exit(1)
            log.info("Opening browser for Google login...")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(OAUTH_CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)

        # Persist locally. In containers OAUTH_TOKEN_FILE is writable
        # only under /tmp, so wrap in try/except.
        try:
            OAUTH_TOKEN_FILE.write_text(creds.to_json())
            log.info("OAuth2 token saved to %s", OAUTH_TOKEN_FILE)
        except OSError as exc:
            log.warning("Could not persist token.json (%s) — carrying on", exc)

    return creds
