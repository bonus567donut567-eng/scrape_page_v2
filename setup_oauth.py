"""Create token.json for Google Drive/Sheets submission."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import google_config
from google_auth import SCOPES


def main() -> int:
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_path = (
        google_config.GOOGLE_OAUTH_CLIENT
        if len(sys.argv) < 2 else Path(sys.argv[1]).expanduser().resolve()
    )
    if not client_path.is_file():
        print(f"ไม่พบ client_secret.json ที่ {client_path}")
        return 1
    token_path = google_config.ROOT / "token.json"
    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), SCOPES)
    credentials = flow.run_local_server(port=0)
    temporary = token_path.with_suffix(".json.tmp")
    try:
        temporary.write_text(credentials.to_json(), encoding="utf-8")
        os.replace(temporary, token_path)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"บันทึก token แล้วที่ {token_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
