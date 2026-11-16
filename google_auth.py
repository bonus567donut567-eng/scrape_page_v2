"""Load and refresh OAuth credentials for Google Sheets and Drive."""
from __future__ import annotations

import os

import google_config

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


def get_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = google_config.GOOGLE_TOKEN_PATH
    if not token_path.is_file():
        raise FileNotFoundError(
            f"ไม่พบ token.json ที่ {token_path} กรุณารัน python setup_oauth.py ก่อน"
        )
    credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        temporary = token_path.with_suffix(token_path.suffix + ".tmp")
        try:
            temporary.write_text(credentials.to_json(), encoding="utf-8")
            os.replace(temporary, token_path)
        finally:
            temporary.unlink(missing_ok=True)
    if not credentials.valid:
        raise RuntimeError("Google OAuth token ใช้งานไม่ได้ กรุณารัน python setup_oauth.py ใหม่")
    return credentials
