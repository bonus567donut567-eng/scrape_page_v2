"""Configuration for Google Drive/Sheets submission."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LEGACY_GOOGLE_DIR = ROOT.parent / "scrapIg"


def _env(name: str, default: str) -> str:
    return str(os.getenv(name, default)).strip()


def _credential_path(env_name: str, filename: str) -> Path:
    override = os.getenv(env_name, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local = ROOT / filename
    if local.is_file():
        return local
    # ใช้ OAuth เดิมของ scrapIg ได้ทันทีโดยไม่คัดลอก secret/token เพิ่ม
    legacy = LEGACY_GOOGLE_DIR / filename
    return legacy if legacy.is_file() else local


GOOGLE_OAUTH_CLIENT = _credential_path("SCRAPE_PAGE_GOOGLE_OAUTH_CLIENT", "client_secret.json")
GOOGLE_TOKEN_PATH = _credential_path("SCRAPE_PAGE_GOOGLE_TOKEN", "token.json")
SPREADSHEET_ID = _env(
    "SCRAPE_PAGE_SPREADSHEET_ID",
    "1Vtft-SCLVbmveV8xCHds7NuVIS0Opy8lbcbl8xhQfbU",
)
SHEET_GID = int(_env("SCRAPE_PAGE_SHEET_GID", "1674818093") or "0")
SHEET_TAB = _env("SCRAPE_PAGE_SHEET_TAB", "Sheet1")
DRIVE_FOLDER_ID = _env("SCRAPE_PAGE_DRIVE_FOLDER_ID", "")
DRIVE_FOLDER_NAME = _env("SCRAPE_PAGE_DRIVE_FOLDER_NAME", "scrapIG_images")
