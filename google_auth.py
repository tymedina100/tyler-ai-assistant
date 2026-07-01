"""One-time Google OAuth setup: run this locally once to create token.json.

Prerequisites:
1. In Google Cloud Console, create an OAuth 2.0 Client ID of type "Desktop app"
   and enable the Google Calendar API and the Gmail API on the project.
2. Download that client secret and save it next to this script as credentials.json.

Then run:  python google_auth.py

A browser window opens for consent. On success, token.json is written (gitignored).
Deploy token.json as a secret / persistent-volume file so the headless bot can keep
refreshing it without ever needing a browser again.
"""
from google_auth_oauthlib.flow import InstalledAppFlow

from google_helpers import SCOPES, TOKEN_FILE, CREDENTIALS_FILE


def main():
    if not CREDENTIALS_FILE.exists():
        raise SystemExit(
            f"Missing {CREDENTIALS_FILE.name}. Download an OAuth 'Desktop app' client "
            "ID from Google Cloud Console and save it as credentials.json next to this script."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    print(f"Success - wrote {TOKEN_FILE.name}. Google Calendar + Gmail are now connected.")


if __name__ == "__main__":
    main()
