"""Upload a scraped image to Drive and append caption/link to Google Sheets."""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import google_auth
import google_config
from json_store import load_json, save_json
from output_cleanup import cleanup_failed_post_media, cleanup_submitted_post

SUBMITTED_FLAG = "submitted.flag"
SUBMISSION_STATE = "submission_state.json"
MAX_SUBMISSION_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 20
TIME_HEADERS = ("time", "ประทับเวลา", "timestamp")
_folder_id_cache: str | None = None
_folder_resolved = False


def _ensure_drive_folder(drive_service) -> str | None:
    global _folder_id_cache, _folder_resolved
    if _folder_resolved:
        return _folder_id_cache
    if google_config.DRIVE_FOLDER_ID:
        _folder_id_cache = google_config.DRIVE_FOLDER_ID
        _folder_resolved = True
        return _folder_id_cache
    try:
        folder_name = google_config.DRIVE_FOLDER_NAME.replace("'", "\\'")
        response = drive_service.files().list(
            q=(
                "mimeType='application/vnd.google-apps.folder' "
                f"and name='{folder_name}' and trashed=false"
            ),
            spaces="drive",
            fields="files(id,name)",
        ).execute()
        folders = response.get("files", [])
        if folders:
            _folder_id_cache = folders[0]["id"]
        else:
            created = drive_service.files().create(
                body={
                    "name": google_config.DRIVE_FOLDER_NAME,
                    "mimeType": "application/vnd.google-apps.folder",
                },
                fields="id",
            ).execute()
            _folder_id_cache = created["id"]
    except Exception as exc:
        print(f"[!] หา/สร้างโฟลเดอร์ Google Drive ไม่สำเร็จ จะอัปโหลดไว้ที่ root: {exc}")
        _folder_id_cache = None
    _folder_resolved = True
    return _folder_id_cache


def _resolve_sheet_target(sheets_service) -> tuple[str, int, int, int | None]:
    fallback = (google_config.SHEET_TAB, 0, 1, None)
    try:
        metadata = sheets_service.spreadsheets().get(
            spreadsheetId=google_config.SPREADSHEET_ID,
            fields="sheets(properties(sheetId,title))",
        ).execute()
        sheets = metadata.get("sheets", [])
        title = next(
            (
                item.get("properties", {}).get("title", "")
                for item in sheets
                if item.get("properties", {}).get("sheetId") == google_config.SHEET_GID
            ),
            "",
        )
        if not title:
            title = (
                sheets[0].get("properties", {}).get("title", google_config.SHEET_TAB)
                if sheets else google_config.SHEET_TAB
            )
        header_result = sheets_service.spreadsheets().values().get(
            spreadsheetId=google_config.SPREADSHEET_ID,
            range=f"'{title}'!1:1",
        ).execute()
        headers = header_result.get("values", [[]])
        normalized = [str(value).strip().lower() for value in (headers[0] if headers else [])]
        caption_index = normalized.index("original_caption")
        picture_index = normalized.index("original_picture")
        time_index = next(
            (normalized.index(name) for name in TIME_HEADERS if name in normalized),
            None,
        )
        return title, caption_index, picture_index, time_index
    except Exception as exc:
        print(f"[!] อ่าน header ของ Google Sheet ไม่สำเร็จ ใช้คอลัมน์ A/B แทน: {exc}")
        return fallback


def _find_image(post_dir: Path) -> Path | None:
    preferred = post_dir / "image.jpg"
    if preferred.is_file():
        return preferred
    for pattern in ("image.jpeg", "image.png", "image.webp", "image_1.*"):
        matches = [path for path in post_dir.glob(pattern) if path.is_file()]
        if matches:
            return matches[0]
    return None


def _submission_state(post_dir: Path) -> dict:
    state = load_json(post_dir / SUBMISSION_STATE, dict)
    return state if isinstance(state, dict) else {}


def _attempt_count(state: dict) -> int:
    try:
        return max(0, int(state.get("attempt_count", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _is_terminal_failure(post_dir: Path) -> bool:
    state = _submission_state(post_dir)
    return (
        state.get("status") == "failed"
        and _attempt_count(state) >= MAX_SUBMISSION_ATTEMPTS
    )


def _record_submission_failure(post_dir: Path) -> dict:
    state = _submission_state(post_dir)
    now = datetime.now().astimezone()
    attempt_count = _attempt_count(state) + 1
    state.update({
        "attempt_count": attempt_count,
        "first_failed_at": state.get("first_failed_at") or now.isoformat(timespec="seconds"),
        "last_failed_at": now.isoformat(timespec="seconds"),
    })
    if attempt_count >= MAX_SUBMISSION_ATTEMPTS:
        state["status"] = "failed"
    else:
        state["status"] = "retry_pending"
    state.pop("delete_after", None)
    save_json(post_dir / SUBMISSION_STATE, state)
    return state


def append_post(post_directory: str | Path, *, credentials=None) -> bool:
    """Never raise: return True only when Drive upload and Sheets append succeeded."""
    post_dir = Path(post_directory).resolve()
    flag_path = post_dir / SUBMITTED_FLAG
    if flag_path.is_file():
        return True
    caption_path = post_dir / "caption.txt"
    image_path = _find_image(post_dir)
    if not caption_path.is_file() or image_path is None:
        print(f"[!] ส่งไม่ได้ {post_dir.name}: ต้องมี caption.txt และ image.jpg")
        return False
    caption = caption_path.read_text(encoding="utf-8-sig").strip()
    if not caption:
        print(f"[!] ส่งไม่ได้ {post_dir.name}: caption.txt ว่าง")
        return False
    if not google_config.SPREADSHEET_ID:
        print("[!] ยังไม่ได้ตั้ง SCRAPE_PAGE_SPREADSHEET_ID")
        return False

    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

        creds = credentials or google_auth.get_credentials()
        drive = build("drive", "v3", credentials=creds)
        folder_id = _ensure_drive_folder(drive)
        body = {"name": f"{post_dir.name}_{image_path.name}"}
        if folder_id:
            body["parents"] = [folder_id]
        uploaded = drive.files().create(
            body=body,
            media_body=MediaFileUpload(str(image_path)),
            fields="id",
        ).execute()
        file_id = uploaded["id"]
        try:
            drive.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": "reader"},
            ).execute()
        except Exception as exc:
            print(f"[!] ตั้งรูปเป็น public ไม่สำเร็จ แต่จะลองส่งลิงก์ต่อ: {exc}")
        link_result = drive.files().get(fileId=file_id, fields="webViewLink").execute()
        image_link = link_result.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"

        sheets = build("sheets", "v4", credentials=creds)
        title, caption_index, picture_index, time_index = _resolve_sheet_target(sheets)
        indexes = [caption_index, picture_index]
        if time_index is not None:
            indexes.append(time_index)
        row = [""] * (max(indexes) + 1)
        row[caption_index] = caption
        row[picture_index] = image_link
        if time_index is not None:
            row[time_index] = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        sheets.spreadsheets().values().append(
            spreadsheetId=google_config.SPREADSHEET_ID,
            range=f"'{title}'!A:Z",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        flag_path.write_text(
            f"submitted_at={datetime.now().astimezone().isoformat(timespec='seconds')}\n",
            encoding="utf-8",
        )
        (post_dir / SUBMISSION_STATE).unlink(missing_ok=True)
        return True
    except Exception as exc:
        print(f"[!] ส่ง Google Drive/Sheets ไม่สำเร็จสำหรับ {post_dir.name}: {type(exc).__name__}: {exc}")
        return False


def process_pending_submissions(
    output_root: str | Path,
    *,
    retry_delay_seconds: int = RETRY_DELAY_SECONDS,
) -> dict:
    posts_root = Path(output_root).resolve() / "posts"
    pending = []
    if posts_root.is_dir():
        pending = sorted(
            (
                directory for directory in posts_root.iterdir()
                if directory.is_dir()
                and (directory / "post.json").is_file()
                and load_json(directory / "post.json", dict).get("status") == "success"
                and (directory / "caption.txt").is_file()
                and _find_image(directory) is not None
                and not (directory / SUBMITTED_FLAG).is_file()
                and not _is_terminal_failure(directory)
            ),
            key=lambda path: (path.stat().st_mtime, path.name),
        )
    submitted: list[str] = []
    failed: list[str] = []
    retry_pending: list[str] = []
    terminal_failed: list[str] = []
    for index, directory in enumerate(pending, start=1):
        print(f"[*] ส่ง Google Sheets {index}/{len(pending)} post_id={directory.name}")
        while _attempt_count(_submission_state(directory)) < MAX_SUBMISSION_ATTEMPTS:
            attempt_number = _attempt_count(_submission_state(directory)) + 1
            if append_post(directory):
                submitted.append(directory.name)
                cleanup = cleanup_submitted_post(directory)
                print(
                    f"[+] ส่งสำเร็จ post_id={directory.name} | "
                    f"ลบไฟล์ในเครื่อง {cleanup['removed_files']} ไฟล์"
                )
                break
            state = _record_submission_failure(directory)
            if state["status"] == "failed":
                failed.append(directory.name)
                terminal_failed.append(directory.name)
                cleanup = cleanup_failed_post_media(directory)
                print(
                    f"[x] ส่งไม่สำเร็จครบ {MAX_SUBMISSION_ATTEMPTS} ครั้ง "
                    f"post_id={directory.name} | "
                    f"ลบรูปและ caption {cleanup['removed_files']} ไฟล์แล้ว"
                )
                break
            else:
                print(
                    f"[!] ส่งไม่สำเร็จครั้งที่ {attempt_number}/"
                    f"{MAX_SUBMISSION_ATTEMPTS} post_id={directory.name} | "
                    f"รอ {retry_delay_seconds} วินาทีแล้วลองใหม่"
                )
                if retry_delay_seconds > 0:
                    time.sleep(retry_delay_seconds)
    return {
        "total": len(pending),
        "submitted": len(submitted),
        "failed": len(failed),
        "submitted_post_ids": submitted,
        "failed_post_ids": failed,
        "retry_pending_post_ids": retry_pending,
        "terminal_failed_post_ids": terminal_failed,
    }
