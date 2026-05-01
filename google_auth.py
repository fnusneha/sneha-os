"""
Google auth helper — Sneha.OS reads from Sheets, Drive, and Calendar.

Scopes (see `constants.SCOPES`):
  - Sheets — read the Travel Master Planner + cycling Library
  - Drive  — export the Habit Tracker Google Doc
  - Calendar — cycle day detection + weekly notes ("Week Agenda" card)

Auth source order (first hit wins):
  0. `GOOGLE_SERVICE_ACCOUNT_JSON` env var  ← preferred; never expires
  1. `GOOGLE_TOKEN_JSON` env var  (Render web service, GitHub Actions cron)
  2. `token.json` on disk         (local Mac dev)
  3. Interactive browser login    (one-time; only works on a machine with
                                  a browser, raises in containers)

Why service-account first
─────────────────────────
User-OAuth refresh tokens for apps using sensitive scopes (which we do —
Calendar/Sheets/Drive) get rotated by Google every 7 days unless the
app is fully verified through their multi-week review. A service
account dodges that entirely: its key is a long-lived asymmetric
credential. The only requirement is that the resources we want to read
(Travel sheet, Habit doc, primary calendar) are SHARED with the
service-account email — same as sharing with a teammate. Once shared,
the cron and the web service never need their tokens refreshed again.

Headless callers (Flask, cron) should set `GOOGLE_NO_INTERACTIVE=1` so
they fail fast with a clean error instead of hanging on a non-existent
browser.
"""

import json
import logging
import os
import sys

from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as SACredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

from constants import SCRIPT_DIR, SCOPES

log = logging.getLogger(__name__)

OAUTH_CREDENTIALS_FILE = SCRIPT_DIR / os.getenv(
    "OAUTH_CREDENTIALS_FILE", "credentials.json"
)
OAUTH_TOKEN_FILE = SCRIPT_DIR / "token.json"


def _load_creds_from_service_account() -> SACredentials | None:
    """Try to construct credentials from the GOOGLE_SERVICE_ACCOUNT_JSON env var.

    Returns service-account credentials (auto-refreshing, never expire),
    or None if the env var is unset or malformed. Service-account creds
    are the preferred path because they aren't subject to the 7-day
    rotation that hits user-OAuth tokens for unverified apps using
    sensitive scopes.

    Required GCP setup:
      1. Create a service account in the GCP project.
      2. Download its JSON key.
      3. Share the Travel sheet, Habit Doc, and primary Calendar with
         the service-account email.
    """
    blob = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not blob:
        return None
    try:
        info = json.loads(blob)
        creds = SACredentials.from_service_account_info(info, scopes=SCOPES)
        log.info("Using service-account credentials (account=%s)",
                 info.get("client_email", "?"))
        return creds
    except Exception as exc:
        log.warning("GOOGLE_SERVICE_ACCOUNT_JSON parse failed: %s", exc)
        return None


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


def get_google_creds():
    """Get credentials for Sheets + Drive + Calendar.

    Returns either a service-account `SACredentials` (preferred) or a
    user-OAuth `Credentials`. Both implement the `google.auth.credentials`
    protocol and work transparently with `googleapiclient.discovery.build`.
    """
    # 0. Service account (preferred — never expires)
    sa_creds = _load_creds_from_service_account()
    if sa_creds:
        return sa_creds

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
