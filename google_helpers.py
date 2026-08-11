"""Google Calendar + Gmail integration for Cadence (calendar) and Piper (gmail).

Auth model: a one-time local OAuth consent (run google_auth.py) mints token.json,
which stores a long-lived refresh token. Every call here loads token.json and
refreshes silently, so the headless deployed worker never needs a browser.
token.json and credentials.json are gitignored and must be provided as secrets /
persistent-volume files on deploy.

The Google client libraries are imported lazily inside functions so that importing
this module never fails just because the libraries (or token.json) aren't installed
yet - each function instead returns a friendly error string, matching the tool-result
convention used by get_weather/create_task in main.py.
"""
import base64
import os
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

BASE_DIR = Path(__file__).parent
# token.json is refreshed at runtime, so it lives in DATA_DIR (a mounted volume on a
# cloud deploy) to survive redeploys. credentials.json is only needed for the one-time
# local google_auth.py run, so it stays in the project dir.
DATA_DIR = Path(os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or BASE_DIR)
TOKEN_FILE = DATA_DIR / "token.json"
CREDENTIALS_FILE = BASE_DIR / "credentials.json"

# Scopes: read/write calendar events, read email, create drafts, and send.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
]

TIMEZONE = os.environ.get("TIMEZONE", "America/New_York")


def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TIMEZONE)
    except Exception:
        return None


def _ensure_token_file():
    """On a fresh cloud deploy the gitignored token.json isn't baked into the image.
    If it's not on disk but its contents are supplied via the GOOGLE_TOKEN_JSON env
    var (a platform secret), materialize the file so credentials load normally. A
    mounted volume file also just works - this only fills the gap when there's no
    volume. The refresh token inside stays valid across restarts, so re-materializing
    from the env var on each boot is fine."""
    if TOKEN_FILE.exists():
        return

    token_json = os.environ.get("GOOGLE_TOKEN_JSON")
    if token_json:
        TOKEN_FILE.write_text(token_json, encoding="utf-8")


def _get_credentials():
    """Load stored credentials and refresh if needed. Raises RuntimeError with a
    user-facing message if token.json is missing or unusable - callers turn that
    into a friendly tool result."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    _ensure_token_file()

    if not TOKEN_FILE.exists():
        raise RuntimeError(
            "Google isn't connected yet - run 'python google_auth.py' locally to sign "
            "in, then set the token.json contents as the GOOGLE_TOKEN_JSON secret on "
            "your deploy."
        )

    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise RuntimeError(
                "Google credentials are invalid - re-run 'python google_auth.py' to reconnect."
            )

    return creds


def _calendar_service():
    from googleapiclient.discovery import build
    return build("calendar", "v3", credentials=_get_credentials(), cache_discovery=False)


def _gmail_service():
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=_get_credentials(), cache_discovery=False)


# --------------------------------------------------------------------------- #
# Calendar
# --------------------------------------------------------------------------- #

def list_events(time_min_iso, time_max_iso):
    """Return a human-readable list of primary-calendar events in a time window."""
    try:
        service = _calendar_service()
        result = service.events().list(
            calendarId="primary",
            timeMin=time_min_iso,
            timeMax=time_max_iso,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = result.get("items", [])
        if not events:
            return "No events in that window."

        lines = []
        for event in events:
            start = event["start"].get("dateTime", event["start"].get("date", "?"))
            lines.append(f"- {start}: {event.get('summary', '(no title)')}")
        return "\n".join(lines)

    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"Sorry, couldn't read the calendar ({e})."


def _today_window():
    tz = _tz()
    now = datetime.now(tz) if tz else datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end


def list_today_events():
    start, end = _today_window()
    return list_events(start.isoformat(), end.isoformat())


def get_today_events_raw():
    """Structured today's *timed* events for the scheduler: a list of
    {'summary': str, 'start': aware datetime}. All-day events are skipped (no alert).
    Returns [] on any error so the scheduler can degrade gracefully."""
    from dateutil import parser as date_parser

    start, end = _today_window()
    try:
        service = _calendar_service()
        result = service.events().list(
            calendarId="primary",
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()
    except Exception:
        return []

    events = []
    for event in result.get("items", []):
        start_str = event["start"].get("dateTime")
        if not start_str:
            continue  # all-day event
        try:
            start_dt = date_parser.parse(start_str)
        except Exception:
            continue
        events.append({"summary": event.get("summary", "(no title)"), "start": start_dt})
    return events


def create_event(summary, start_iso, end_iso, description=""):
    try:
        service = _calendar_service()
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start_iso, "timeZone": TIMEZONE},
            "end": {"dateTime": end_iso, "timeZone": TIMEZONE},
        }
        event = service.events().insert(calendarId="primary", body=body).execute()
        return f"Created event '{summary}' ({start_iso} to {end_iso}). Link: {event.get('htmlLink', 'n/a')}"
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"Sorry, couldn't create the event ({e})."


# --------------------------------------------------------------------------- #
# Gmail
# --------------------------------------------------------------------------- #

def list_recent_emails(query="", max_results=10):
    try:
        service = _gmail_service()
        result = service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()

        messages = result.get("messages", [])
        if not messages:
            return "No matching emails."

        lines = []
        for meta in messages:
            msg = service.users().messages().get(
                userId="me", id=meta["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
            lines.append(
                f"- [{meta['id']}] {headers.get('Date', '?')} | "
                f"{headers.get('From', '?')} | {headers.get('Subject', '(no subject)')}"
            )
        return "\n".join(lines)

    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"Sorry, couldn't read email ({e})."


def _extract_body(payload):
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        nested = _extract_body(part)
        if nested and nested != "(no plain-text body found)":
            return nested
    return "(no plain-text body found)"


def get_email(message_id):
    try:
        service = _gmail_service()
        msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        body = _extract_body(msg["payload"])
        return (
            f"From: {headers.get('From', '?')}\n"
            f"Subject: {headers.get('Subject', '(no subject)')}\n"
            f"Date: {headers.get('Date', '?')}\n\n"
            f"{body[:4000]}"
        )
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"Sorry, couldn't read that email ({e})."


def _build_message(to, subject, body):
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return {"raw": raw}


def create_draft(to, subject, body):
    try:
        service = _gmail_service()
        draft = service.users().drafts().create(
            userId="me", body={"message": _build_message(to, subject, body)}
        ).execute()
        return f"Draft created (id {draft['id']}) to {to}, subject '{subject}'. It's waiting in your Gmail Drafts."
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"Sorry, couldn't create the draft ({e})."


def send_email(to, subject, body, *, service=None):
    try:
        service = service or _gmail_service()
        sent = service.users().messages().send(
            userId="me", body=_build_message(to, subject, body)
        ).execute()
        return f"Email sent to {to}, subject '{subject}' (id {sent['id']})."
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"Sorry, couldn't send the email ({e})."
