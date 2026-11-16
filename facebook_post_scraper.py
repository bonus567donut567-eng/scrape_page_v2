import os
import sys
import time
import re
import hashlib
import io
import json
import html
import requests
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit
from PIL import Image, UnidentifiedImageError
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.command import Command

from chrome_profile import create_selenium_driver

MIN_POST_IMAGE_PX = 200
MIN_POST_CAPTION_CHARS = 20
MIN_COMMENT_PHOTO_LONG_EDGE = 240
MIN_COMMENT_PHOTO_AREA = 50_000
COMMENT_MANIFEST_NAME = "comment_media_manifest.json"
CAPTION_DIAGNOSTICS_FILE = Path(__file__).resolve().parent / "logs" / "caption_diagnostics.jsonl"


def _safe_print(*values) -> None:
    """Keep standalone runs alive on legacy Windows console encodings."""
    try:
        print(*values)
    except UnicodeEncodeError:
        message = " ".join(str(value) for value in values) + "\n"
        stream = getattr(sys.stdout, "buffer", None)
        if stream is not None:
            stream.write(message.encode("utf-8", errors="replace"))
            stream.flush()
        else:
            sys.stdout.write(message.encode("ascii", errors="replace").decode("ascii"))

_COMMENT_LABEL_MARKERS = (
    "comment by",
    "comment from",
    "reply by",
    "reply from",
    "ความคิดเห็นโดย",
    "ความคิดเห็นจาก",
    "การตอบกลับโดย",
    "การตอบกลับจาก",
)
_NON_PHOTO_MEDIA_MARKERS = (
    "sticker",
    "สติกเกอร์",
    "emoji",
    "อีโมจิ",
    "animated",
    "ภาพเคลื่อนไหว",
    "tenor.com",
    "giphy.com",
    "/rsrc.php",
)
_NON_ENTITY_PATHS = {
    "photo",
    "photos",
    "posts",
    "story.php",
    "permalink.php",
    "watch",
    "reel",
    "groups",
    "hashtag",
    "help",
    "l.php",
}

# คำลงท้ายหรือข้อความขยะที่มักติดมาตอนกดขยายโพสต์
_TRIM_SUFFIXES = (
    "ดูน้อยลง",
    "See less",
    "See Less",
    "ดูเพิ่มเติม",
    "See more",
    "See More",
    "… ดูน้อยลง",
    "... See less",
)

# Facebook can keep serving public media while replacing the post message with
# a soft login wall. These phrases are page chrome, never an original post
# caption. Keep strong markers separate from the generic "Log in" label so a
# loaded public post is not rejected merely because the header has a login
# button.
_LOGIN_WALL_TEXT_MARKERS = (
    "log in or sign up for facebook",
    "log into facebook to start sharing and connecting",
    "connect with friends, family and people you know",
    "facebook helps you connect and share",
    "เข้าสู่ระบบหรือสมัครใช้งาน facebook",
    "เข้าสู่ระบบ facebook เพื่อเริ่มแชร์และเชื่อมต่อ",
    "เชื่อมต่อกับเพื่อน ครอบครัว และผู้คนที่คุณรู้จัก",
)


def clean_caption_text(text: str) -> str:
    """ล้างเครื่องหมายคำพูดคู่ และตัดคำว่า ดูน้อยลง / See less ที่ติดมาท้ายข้อความ"""
    if not text:
        return ""
    
    # 1. ล้างเครื่องหมายคำพูดคู่ แปลงเป็น single quote
    cleaned = (
        text.replace('"', "'")
        .replace('“', "'")
        .replace('”', "'")
        .replace("\ufeff", "")
        .replace("\u200b", "")
        .replace("\u2060", "")
        .strip()
    )
    
    # 2. ตรวจสอบและตัดคำว่า ดูน้อยลง / See less ท้ายข้อความ
    changed = True
    while changed:
        changed = False
        for suffix in _TRIM_SUFFIXES:
            if cleaned.endswith(suffix):
                cleaned = cleaned[:-len(suffix)].strip()
                changed = True
                
    return cleaned.strip()


def _caption_trace(driver, *, status: str, source: str, text: str = "", reason: str = "") -> None:
    """Keep bounded evidence until the scrape result is written to diagnostics."""
    try:
        trace = list(getattr(driver, "_gf_caption_trace", []) or [])
        normalized = re.sub(r"[\s\u00a0]+", " ", str(text or "")).strip()
        trace.append({
            "status": status,
            "source": source,
            "reason": reason,
            "length": len(normalized),
            "preview": normalized[:500],
            "sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else "",
        })
        setattr(driver, "_gf_caption_trace", trace[-30:])
    except Exception:
        pass


def _caption_result(driver, post_url: str, success: bool, caption: str, error: str):
    """Persist one bounded candidate trail and return the scraper tuple."""
    try:
        CAPTION_DIAGNOSTICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "post_url": str(post_url or ""),
            "success": bool(success),
            "error": str(error or ""),
            "caption_length": len(str(caption or "")),
            "caption_sha256": hashlib.sha256(str(caption or "").encode("utf-8")).hexdigest() if caption else "",
            "trace": list(getattr(driver, "_gf_caption_trace", []) or []),
        }
        # One append/write keeps records intact when multiple workers finish
        # close together.
        payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        descriptor = os.open(
            str(CAPTION_DIAGNOSTICS_FILE),
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
        )
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
    except (OSError, TypeError, ValueError):
        pass
    return success, caption, error


def caption_is_login_wall_text(text: str) -> bool:
    """Return True for Facebook's anonymous-session login-wall copy."""
    normalized = re.sub(r"[\s\u00a0]+", " ", str(text or "")).strip().casefold()
    return bool(normalized) and any(
        marker in normalized for marker in _LOGIN_WALL_TEXT_MARKERS
    )


def page_has_login_wall(driver) -> bool:
    """Detect a login page before generic DOM labels are scored as captions."""
    try:
        current_url = str(driver.current_url or "").casefold()
    except Exception:
        current_url = ""
    if any(marker in current_url for marker in ("/login", "/checkpoint", "/recover")):
        return True
    try:
        body_text = str(driver.execute_script(
            "return (document.body && document.body.innerText) || ''"
        ) or "")
    except Exception:
        return False
    folded = re.sub(r"[\s\u00a0]+", " ", body_text).strip().casefold()
    if caption_is_login_wall_text(folded):
        return True
    has_login_action = "log in" in folded or "เข้าสู่ระบบ" in folded
    has_account_prompt = any(marker in folded for marker in (
        "forgotten account",
        "forgot password",
        "create new account",
        "sign up for facebook",
        "ลืมบัญชีใช่หรือไม่",
        "ลืมรหัสผ่าน",
        "สร้างบัญชีใหม่",
        "สมัครใช้งาน facebook",
    ))
    return has_login_action and has_account_prompt


def strip_fb_cdn_compression(url: str) -> str:
    """ตัดพารามิเตอร์บีบอัด stp=... เพื่อดึงไฟล์ขนาดเต็ม Full HD"""
    if not url:
        return ""
    try:
        parts = urlsplit(html.unescape(url))
        # stp/ctp/cstp are Facebook CDN resize/crop transforms. Removing them
        # preserves the signed media URL while requesting the original asset.
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.casefold() not in {"stp", "ctp", "cstp"}
        ]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    except Exception:
        clean_url = re.sub(r'(?:stp|ctp|cstp)=[^&]+&?', '', html.unescape(url))
        return clean_url.rstrip('?&')


def extract_page_id_from_url(url: str) -> str:
    """Extract a numeric Facebook entity ID without guessing from display text."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        set_value = str(query.get("set", [""])[0])
        match = re.match(r"pb\.(\d+)(?:\.|$)", set_value)
        if match:
            return match.group(1)
        profile_id = str(query.get("id", [""])[0])
        if profile_id.isdigit():
            return profile_id
        path_parts = [part for part in parts.path.split("/") if part]
        if len(path_parts) == 1 and path_parts[0].isdigit():
            return path_parts[0]
    except Exception:
        return ""
    return ""


def normalize_facebook_entity_key(url: str) -> str:
    """Return a comparable Page/profile key, excluding post and media URLs."""
    if not url:
        return ""
    try:
        parts = urlsplit(url if "://" in url else f"https://www.facebook.com/{url.lstrip('/')}")
        host = parts.netloc.lower().split(":", 1)[0]
        if host and not (host == "facebook.com" or host.endswith(".facebook.com")):
            return ""
        path_parts = [part for part in parts.path.split("/") if part]
        if not path_parts:
            return ""
        first = path_parts[0].lower()
        if first in _NON_ENTITY_PATHS:
            return ""
        if first == "profile.php":
            profile_id = str(parse_qs(parts.query).get("id", [""])[0])
            return f"id:{profile_id}" if profile_id.isdigit() else ""
        if first.isdigit():
            return f"id:{first}"
        return f"path:{first}"
    except Exception:
        return ""


def _empty_owner_identity() -> dict:
    return {
        "ids": set(),
        "keys": set(),
        "names": set(),
        "sources": [],
        "conflicts": [],
    }


def _add_identity_url(identity: dict, url: str, source: str) -> None:
    page_id = extract_page_id_from_url(url)
    key = normalize_facebook_entity_key(url)
    if page_id:
        identity["ids"].add(page_id)
        identity["keys"].add(f"id:{page_id}")
    if key:
        identity["keys"].add(key)
    if page_id or key:
        identity["sources"].append(source)


def _profile_like_anchor(href: str) -> bool:
    return bool(normalize_facebook_entity_key(href))


def _merge_identity(target: dict, source: dict) -> None:
    for field in ("ids", "keys", "names"):
        target[field].update(source.get(field, ()))
    target["sources"].extend(source.get("sources", ()))
    target["conflicts"].extend(source.get("conflicts", ()))


def resolve_post_owner_identity(
    driver,
    post_url: str,
    *,
    source_page_id: str = "",
    source_page_url: str = "",
) -> dict:
    """Build a strict identity set for the Page/profile which owns the post."""
    identity = _empty_owner_identity()
    if str(source_page_id).isdigit():
        identity["ids"].add(str(source_page_id))
        identity["keys"].add(f"id:{source_page_id}")
        identity["sources"].append("collector_page_id")
    _add_identity_url(identity, source_page_url, "collector_page_url")
    _add_identity_url(identity, post_url, "post_url")
    if len(identity["ids"]) > 1:
        identity["conflicts"].append(
            "collector/source URL Page IDs disagree: "
            + ",".join(sorted(identity["ids"]))
        )

    # The first post message is much safer than searching every anchor on the
    # page: walk to its own article and read the leading profile/Page link.
    try:
        messages = driver.find_elements(
            By.XPATH,
            '//div[@data-ad-comet-preview="message" or @data-ad-preview="message"]',
        )
        for message in messages[:2]:
            try:
                article = message.find_element(By.XPATH, './ancestor::div[@role="article"][1]')
                # Only the heading area represents the post author. Searching
                # every article anchor can pick up mentions or shared links.
                anchors = article.find_elements(
                    By.XPATH,
                    './/h2//a[@href] | .//h3//a[@href] | '
                    './/*[@role="heading"]//a[@href]',
                )
                for anchor in anchors:
                    href = anchor.get_attribute("href") or ""
                    if not _profile_like_anchor(href):
                        continue
                    candidate = _empty_owner_identity()
                    _add_identity_url(candidate, href, "post_author_link")
                    name = (anchor.text or anchor.get_attribute("aria-label") or "").strip()
                    if name:
                        candidate["names"].add(name.casefold())

                    # Collector metadata and pb.<PAGE_ID> in the photo URL are
                    # authoritative. DOM discovery may confirm them, but a
                    # stray/comment anchor must never broaden the allow-list.
                    if owner_identity_is_proven(identity):
                        if identities_match(identity, candidate):
                            _merge_identity(identity, candidate)
                            return identity
                        # A Page header often exposes a vanity URL while the
                        # collector has the numeric pb.<PAGE_ID>. Because this
                        # candidate is restricted to the post heading, retain
                        # that path as an alias. Never accept a different ID.
                        if identity["ids"] and not candidate["ids"]:
                            _merge_identity(identity, candidate)
                            return identity
                        continue
                    _merge_identity(identity, candidate)
                    return identity
            except Exception:
                continue
    except Exception:
        pass

    # Photo lightboxes no longer expose a message node in the current UI, but
    # their timestamp links point to /<owner>/posts/<post>. That link is tied
    # to the viewed post and safely supplies the vanity alias for a pb.<ID> URL.
    try:
        permalink = _canonical_post_permalink(driver)
        if permalink:
            candidate = _empty_owner_identity()
            _add_identity_url(candidate, permalink, "post_permalink_owner")
            if identities_match(identity, candidate) or (
                identity["ids"] and not candidate["ids"]
            ):
                _merge_identity(identity, candidate)
            elif not owner_identity_is_proven(identity):
                _merge_identity(identity, candidate)
    except Exception:
        pass
    return identity


def owner_identity_is_proven(identity: dict) -> bool:
    return bool(
        not identity.get("conflicts")
        and (identity.get("ids") or identity.get("keys"))
    )


def identities_match(owner: dict, author: dict) -> bool:
    return bool(
        set(owner.get("ids", ())) & set(author.get("ids", ()))
        or set(owner.get("keys", ())) & set(author.get("keys", ()))
    )


def _inspect_image_payload(payload: bytes) -> dict:
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image_format = str(image.format or "").upper()
            width, height = image.size
            frames = int(getattr(image, "n_frames", 1) or 1)
            bands = image.getbands()
            has_alpha = "A" in bands or "transparency" in image.info
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError(f"invalid_image: {exc}") from exc
    return {
        "format": image_format,
        "width": int(width),
        "height": int(height),
        "frames": frames,
        "has_alpha": bool(has_alpha),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def _comment_photo_rejection(metadata: dict) -> str:
    image_format = metadata["format"]
    width = metadata["width"]
    height = metadata["height"]
    if image_format == "GIF" or metadata["frames"] > 1:
        return "animated_media"
    if image_format not in {"JPEG", "PNG", "WEBP"}:
        return "unsupported_image_format"
    if max(width, height) < MIN_COMMENT_PHOTO_LONG_EDGE:
        return "image_too_small"
    if width * height < MIN_COMMENT_PHOTO_AREA:
        return "image_area_too_small"
    aspect = width / max(height, 1)
    if metadata["has_alpha"] and max(width, height) <= 192 and 0.70 <= aspect <= 1.43:
        return "sticker_like_transparency"
    return ""


def _fetch_image_payload(
    img_url: str,
    additional_urls: tuple[str, ...] | list[str] = (),
) -> tuple[bytes | None, dict | None, str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.facebook.com/",
    }
    attempted: set[str] = set()
    last_error = "download_failed"
    ordered_urls = [img_url, *additional_urls]
    candidates: list[str] = []
    for value in ordered_urls:
        candidates.extend((strip_fb_cdn_compression(value), value))
    for candidate in candidates:
        if not candidate or candidate in attempted:
            continue
        attempted.add(candidate)
        try:
            response = requests.get(candidate, headers=headers, timeout=20)
            if response.status_code != 200:
                last_error = f"http_{response.status_code}"
                continue
            content_type = str(response.headers.get("Content-Type", "")).lower()
            if content_type and not content_type.startswith("image/"):
                last_error = f"not_image_content_type:{content_type[:80]}"
                continue
            metadata = _inspect_image_payload(response.content)
            metadata["content_type"] = content_type
            return response.content, metadata, candidate, ""
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"[:300]
    return None, None, "", last_error


def download_hd_image(img_url: str, save_image_path: str) -> bool:
    payload, _metadata, _resolved_url, error = _fetch_image_payload(img_url)
    if payload is None:
        _safe_print(f"[!] ดาวน์โหลดภาพล้มเหลว: {error}")
        return False
    try:
        Path(save_image_path).parent.mkdir(parents=True, exist_ok=True)
        Path(save_image_path).write_bytes(payload)
        return True
    except OSError as exc:
        _safe_print(f"[!] บันทึกภาพล้มเหลว: {exc}")
        return False


def init_driver(headless: bool = True, user_data_dir: str = ""):
    profile_path = Path(user_data_dir).expanduser().resolve() if user_data_dir else None
    driver = create_selenium_driver(
        headless=headless,
        **({"profile_path": profile_path} if profile_path else {}),
    )
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return driver


def quit_driver_safely(driver) -> None:
    """Close Chrome without Selenium's URL-connectability shutdown probe.

    Selenium's normal ``driver.quit()`` calls ``Service.stop()``, whose
    ``send_remote_shutdown_command()`` path has produced native Windows access
    violations in this long-running scraper.  Send the WebDriver QUIT command
    normally, then terminate the chromedriver process directly so cleanup does
    not enter that unstable urllib probe.
    """
    if driver is None:
        return

    try:
        driver.execute(Command.QUIT)
    except Exception as exc:
        _safe_print(f"[!] ปิด WebDriver session ไม่สมบูรณ์: {type(exc).__name__}: {exc}")
    finally:
        try:
            driver.stop_client()
        except Exception:
            pass
        try:
            driver.command_executor.close()
        except Exception:
            pass

        service = getattr(driver, "service", None)
        process = getattr(service, "process", None)
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except Exception:
                        process.kill()
                        process.wait(timeout=3)
            except Exception as exc:
                _safe_print(
                    f"[!] ปิด chromedriver process ไม่สมบูรณ์: "
                    f"{type(exc).__name__}: {exc}"
                )
            finally:
                try:
                    service.process = None
                except Exception:
                    pass


def dismiss_login_popup(driver):
    js_cleanup = """
    try {
        const closeBtns = document.querySelectorAll('div[aria-label="Close"], div[aria-label="ปิด"], div[role="button"][aria-label*="lose"]');
        closeBtns.forEach(b => b.click());

        const dialogs = document.querySelectorAll('div[role="dialog"]');
        dialogs.forEach(d => {
            const t = d.innerText || '';
            if (t.includes('See more on Facebook') || t.includes('เข้าสู่ระบบ') || t.includes('Log In')) {
                d.remove();
            }
        });

        document.body.style.overflow = 'auto';
        document.documentElement.style.overflow = 'auto';
    } catch(e) {}
    """
    try:
        driver.execute_script(js_cleanup)
    except Exception:
        pass


def _element_in_labeled_comment(element) -> bool:
    try:
        article = element.find_element(By.XPATH, './ancestor::div[@role="article"][1]')
        label = (article.get_attribute("aria-label") or "").casefold()
        if any(marker in label for marker in _COMMENT_LABEL_MARKERS):
            return True
        return _looks_like_comment_article(article)
    except Exception:
        return False


def _decode_json_string_fragment(value: str) -> str:
    """Decode JSON escapes without corrupting already-decoded Unicode text."""
    try:
        return json.loads(f'"{value}"')
    except (json.JSONDecodeError, TypeError):
        return value


def caption_is_interface_text(text: str) -> bool:
    """Reject Facebook chrome that can resemble a short post caption."""
    normalized = re.sub(
        r"[\s\u00a0]+", " ",
        str(text or "").replace("\ufeff", "").replace("\u200b", "").replace("\u2060", ""),
    ).strip(" ·•\t\r\n")
    if not normalized:
        return True
    folded = normalized.casefold()
    if caption_is_login_wall_text(normalized):
        return True
    absolute_month = (
        r"(?:january|february|march|april|may|june|july|august|september|"
        r"october|november|december|มกราคม|กุมภาพันธ์|มีนาคม|เมษายน|พฤษภาคม|"
        r"มิถุนายน|กรกฎาคม|สิงหาคม|กันยายน|ตุลาคม|พฤศจิกายน|ธันวาคม)"
    )
    absolute_date = (
        rf"(?:\b\d{{1,2}}\s+{absolute_month}(?:\s+\d{{4}})?"
        rf"|\b{absolute_month}\s+\d{{1,2}}(?:,?\s+\d{{4}})?)"
        r"(?:\s+(?:at|เวลา)\s+\d{1,2}:\d{2})?\b"
    )
    numeric_date = r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}(?:\s+\d{1,2}:\d{2})?\b"
    if len(normalized) <= 300 and re.search(r"(?:\bview post\b|ดูโพสต์)", folded):
        if re.search(rf"(?:{absolute_date}|{numeric_date})", folded, re.I):
            return True
    # Facebook sometimes exposes only the timestamp, optionally repeated,
    # as og:description. Remove known timestamp chrome and reject it when no
    # actual post words remain.
    if len(normalized) <= 300 and re.search(rf"(?:{absolute_date}|{numeric_date})", folded, re.I):
        timestamp_residue = re.sub(r"\bai content\b", "", folded, flags=re.I)
        timestamp_residue = re.sub(r"(?:\bview post\b|ดูโพสต์)", "", timestamp_residue, flags=re.I)
        timestamp_residue = re.sub(rf"(?:{absolute_date}|{numeric_date})", "", timestamp_residue, flags=re.I)
        timestamp_residue = re.sub(r"[\s,.;:·•|()\[\]—–-]+", "", timestamp_residue)
        if not timestamp_residue:
            return True
    if len(normalized) <= 300 and "view post" in folded and re.search(
        r"\b(?:\d+\s*(?:m|h|d|w)|just now|yesterday)\b", folded
    ):
        return True
    if len(normalized) <= 300 and re.fullmatch(
        r"(?:be the first to (?:comment|reply)|no comments?(?: yet| to show)?|"
        r"ยังไม่มีความคิดเห็น|ไม่มีความคิดเห็น|ไม่มีคอมเมนต์|"
        r"เป็นคนแรกที่แสดงความคิดเห็น|แสดงความคิดเห็นเป็นคนแรก)[.!]?",
        folded,
        re.I,
    ):
        return True
    if len(normalized) <= 500 and re.fullmatch(
        r"['\u2018\u2019\u201c\u201d]?(?:most relevant|newest|all comments)['\u2018\u2019\u201c\u201d]?"
        r"\s+is selected,?\s+so some comments may have been filtered out\.?",
        folded,
        re.I,
    ):
        return True
    if len(normalized) <= 300 and " replied" in folded and re.search(
        r"\b\d+\s+repl(?:y|ies)\b", folded
    ):
        return True
    if len(normalized) <= 300 and re.fullmatch(
        r"(?:view|see)\s+"
        r"(?:(?:all|more|previous)\s+)?"
        r"(?:\d+\s+)?"
        r"(?:(?:more|previous)\s+)?"
        r"(?:comments?|repl(?:y|ies))",
        folded,
    ):
        return True
    if len(normalized) <= 300 and any(
        marker in folded for marker in (
            "ดูความคิดเห็นเพิ่มเติม",
            "ดูความคิดเห็นก่อนหน้า",
            "ดูการตอบกลับเพิ่มเติม",
            "ดูการตอบกลับก่อนหน้า",
            "ดูการตอบกลับทั้งหมด",
        )
    ):
        return True
    if folded in {
        "all comments",
        "comments",
        "comment",
        "like",
        "log in",
        "create new account",
        "forgotten account?",
        "forgot password?",
        "most relevant",
        "newest",
        "oldest",
        "top comments",
        "view more comments",
        "view previous comments",
        "see more comments",
        "ดูความคิดเห็นเพิ่มเติม",
        "ดูความคิดเห็นก่อนหน้า",
        "ความคิดเห็นทั้งหมด",
        "ความคิดเห็น",
    }:
        return True
    return False


def extract_caption_from_metadata(driver) -> str:
    """Read the canonical social description when Facebook omits message DOM."""
    try:
        elements = driver.find_elements(
            By.XPATH,
            '//meta[@property="og:description" or @name="description"]',
        )
    except Exception:
        return ""
    for element in elements:
        try:
            value = html.unescape(element.get_attribute("content") or "")
        except Exception:
            continue
        try:
            setattr(driver, "_gf_caption_metadata_seen", True)
        except Exception:
            pass
        value = re.sub(r"\s+", " ", value).strip()
        if value.casefold().endswith(" | facebook"):
            value = value[:-11].rstrip()
        if len(value) < MIN_POST_CAPTION_CHARS or caption_is_login_wall_text(value):
            _caption_trace(driver, status="rejected", source="metadata", text=value,
                           reason="too_short_or_login_wall")
            continue
        cleaned = clean_caption_text(value)
        if caption_is_interface_text(cleaned):
            _caption_trace(driver, status="rejected", source="metadata", text=cleaned,
                           reason="interface_text")
            continue
        _caption_trace(driver, status="candidate", source="metadata", text=cleaned)
        return cleaned
    return ""


def caption_is_truncated(text: str) -> bool:
    """Detect Facebook preview text that still ends at a collapsed boundary."""
    normalized = str(text or "").rstrip()
    return bool(
        re.search(r"(?:\.\.\.|…)(?:\s*(?:see more|ดูเพิ่มเติม))?\s*$", normalized, re.I)
        or re.search(r"(?:see more|ดูเพิ่มเติม)\s*$", normalized, re.I)
    )


def _canonical_post_permalink(driver) -> str:
    """Find the post permalink exposed by a photo lightbox."""
    try:
        anchors = driver.find_elements(By.XPATH, '//a[@href]')
    except Exception:
        return ""
    candidates = []
    for anchor in anchors:
        try:
            href = anchor.get_attribute("href") or ""
        except Exception:
            continue
        lowered = href.casefold()
        try:
            parsed = urlsplit(href)
        except ValueError:
            continue
        hostname = (parsed.hostname or "").casefold()
        if hostname not in {"facebook.com", "www.facebook.com", "m.facebook.com"}:
            continue
        query = parse_qs(parsed.query)
        path = parsed.path.casefold().rstrip("/")
        is_post_permalink = (
            "/posts/" in path
            or (path.endswith("/permalink.php") and bool(query.get("story_fbid")))
            or (path.endswith("/story.php") and bool(query.get("story_fbid")))
        )
        if not is_post_permalink:
            continue
        if "comment_id" in query or "reply_comment_id" in query:
            continue
        # Prefer canonical /posts/ URLs, then permalink.php/story.php fallbacks.
        rank = 0 if "/posts/" in path else 1
        candidates.append((rank, href))
    return min(candidates, default=(99, ""))[1]


def _live_caption_candidates(driver, metadata_hint: str = "") -> list[str]:
    """Read expanded post text from both feed and photo-lightbox layouts."""
    hint = re.sub(r"(?:\.\.\.|…)\s*$", "", metadata_hint or "").strip()
    hint_prefix = hint[:80].casefold()
    try:
        elements = driver.find_elements(
            By.XPATH,
            '//div[@data-ad-comet-preview="message" or @data-ad-preview="message"]',
        )
        # ``dir=auto`` is used by nearly every Facebook label. Only inspect
        # that broad surface when canonical metadata gives us a prefix to
        # match; otherwise login/comment controls can masquerade as captions.
        if hint_prefix:
            for element in driver.find_elements(By.XPATH, '//*[@dir="auto"]'):
                if element not in elements:
                    elements.append(element)
    except Exception:
        return []
    candidates: list[tuple[int, str]] = []
    seen: set[str] = set()
    for element in elements:
        if _element_in_labeled_comment(element):
            continue
        try:
            value = driver.execute_script(
                "return arguments[0].innerText || arguments[0].textContent || '';",
                element,
            )
        except Exception:
            continue
        value = clean_caption_text(str(value or ""))
        if (
            len(value) < MIN_POST_CAPTION_CHARS
            or value in seen
            or len(value) > 20_000
            or caption_is_interface_text(value)
        ):
            continue
        seen.add(value)
        folded = value.casefold()
        score = len(value)
        if hint_prefix and hint_prefix in folded:
            score += 100_000
        if "\n" in value:
            score += 500
        if caption_is_truncated(value):
            score -= 50_000
        candidates.append((score, value))
    return [value for _score, value in sorted(candidates, reverse=True)]


def expand_post_caption(
    driver,
    *,
    metadata_hint: str = "",
    attempts: int = 3,
) -> int:
    """Expand Facebook's truncated post message and return click count.

    Facebook has used ``div``, ``span`` and ``button`` elements for the same
    See more control. Keep the search inside the post message/article so a
    similarly named comment control is not clicked by accident.
    """
    script = r"""
        const MESSAGE_SELECTOR =
            '[data-ad-comet-preview="message"], [data-ad-preview="message"]';
        const LABELS = new Set(['ดูเพิ่มเติม', 'see more']);
        const COMMENT_MARKERS = [
            'comment by', 'comment from', 'reply by', 'reply from',
            'ความคิดเห็นโดย', 'ความคิดเห็นจาก', 'การตอบกลับโดย', 'การตอบกลับจาก'
        ];

        const normalizeText = (value) => (value || '')
            .replace(/\s+/g, ' ').trim().toLocaleLowerCase();
        const hintPrefix = normalizeText(arguments[0] || '')
            .replace(/(?:\.\.\.|…)(?:\s*(?:see more|ดูเพิ่มเติม))?\s*$/u, '')
            .slice(0, 80);

        const normalizedLabel = (element) => {
            let value = (element.innerText || element.textContent ||
                         element.getAttribute('aria-label') || '').trim();
            value = value.replace(/^(?:\.\.\.|…)+\s*/u, '').trim().toLocaleLowerCase();
            return value;
        };
        const isSeeMore = (element) => LABELS.has(normalizedLabel(element));
        const messages = Array.from(document.querySelectorAll(MESSAGE_SELECTOR));
        if (hintPrefix) {
            for (const element of document.querySelectorAll('[dir="auto"]')) {
                const value = normalizeText(element.innerText || element.textContent || '');
                if (value.includes(hintPrefix) && !messages.includes(element)) {
                    messages.push(element);
                }
            }
        }
        let clicked = 0;

        for (const message of messages) {
            const article = message.closest('[role="article"]');
            const articleLabel = ((article && article.getAttribute('aria-label')) || '')
                .toLocaleLowerCase();
            if (COMMENT_MARKERS.some(marker => articleLabel.includes(marker))) continue;

            const roots = [message];
            if (article && article !== message) roots.push(article);
            const seen = new Set();
            let messageClicked = false;
            for (const root of roots) {
                const controls = root.querySelectorAll(
                    'button, [role="button"], [tabindex="0"], span, div'
                );
                for (const control of controls) {
                    if (!isSeeMore(control)) continue;
                    const interactive = control.closest(
                        'button, [role="button"], [tabindex="0"]'
                    );
                    const target = interactive && root.contains(interactive) &&
                        isSeeMore(interactive) ? interactive : control;
                    if (seen.has(target)) continue;
                    seen.add(target);
                    const nearestArticle = target.closest('[role="article"]');
                    if (article && nearestArticle && nearestArticle !== article) continue;
                    try {
                        target.scrollIntoView({block: 'center', inline: 'nearest'});
                        target.click();
                        clicked += 1;
                        messageClicked = true;
                        break;
                    } catch (_error) {}
                }
                if (messageClicked) break;
            }
        }
        return clicked;
    """

    total_clicked = 0
    for _ in range(max(1, attempts)):
        try:
            clicked = int(driver.execute_script(script, metadata_hint) or 0)
        except Exception:
            break
        total_clicked += clicked
        if not clicked:
            break
        # React replaces the message node asynchronously after a click.  A
        # quarter-second was too short on real anonymous photo pages: the
        # click succeeded, but the caller immediately re-read the old 478-char
        # preview and classified it as caption_truncated.  The live page needs
        # about one second before the complete text and "See less" are present.
        time.sleep(1.0)
    return total_clicked


def classify_post_scrape_failure(driver, *, has_image: bool) -> str:
    """Classify an empty scrape without treating every case as caption_empty."""
    try:
        current_url = str(driver.current_url or "").casefold()
    except Exception:
        current_url = ""
    try:
        ready_state = str(
            driver.execute_script("return document.readyState") or ""
        ).casefold()
    except Exception:
        ready_state = ""
    try:
        body_text = str(driver.execute_script(
            "return (document.body && document.body.innerText) || ''"
        ) or "")
    except Exception:
        body_text = ""
    folded_body = body_text.casefold()

    unavailable_markers = (
        "this content isn't available",
        "this content is unavailable",
        "เนื้อหานี้ไม่พร้อมใช้งาน",
        "ไม่สามารถดูเนื้อหานี้ได้",
    )
    if any(marker in folded_body for marker in unavailable_markers):
        return "post_unavailable"
    if ready_state and ready_state != "complete":
        return "loading_incomplete"

    try:
        post_evidence = bool(driver.find_elements(
            By.XPATH,
            '//meta[@property="og:image" or @property="og:description"] '
            '| //div[@role="article"]',
        ))
    except Exception:
        post_evidence = False
    login_markers = ("log in", "เข้าสู่ระบบ", "create new account")
    login_route = any(
        marker in current_url for marker in ("/login", "/checkpoint", "/recover")
    )
    if (
        login_route
        or caption_is_login_wall_text(body_text)
        or page_has_login_wall(driver)
        or (
            not has_image
            and not post_evidence
            and any(marker in folded_body for marker in login_markers)
        )
    ):
        return "login_wall"
    if len(body_text.strip()) < 40 and not post_evidence:
        return "loading_incomplete"

    try:
        message_nodes = driver.find_elements(
            By.XPATH,
            '//div[@data-ad-comet-preview="message" or @data-ad-preview="message"]',
        )
    except Exception:
        message_nodes = []
    if has_image and message_nodes:
        return "caption_missing"
    if has_image:
        return "dom_changed"
    return "media_missing"


# =========================================================================
# โซนลิงก์แบบที่ 1 (/photo) — รวดเร็วและแม่นยำ 100%
# =========================================================================
def extract_caption_all_sources(driver) -> str:
    metadata_caption = extract_caption_from_metadata(driver)
    # Without canonical metadata there is no reliable anonymous-page anchor.
    # DOM labels and nearby JSON routinely contain page names, comment text,
    # and login controls, so fail closed and let the bridge retry later.
    metadata_seen = bool(getattr(driver, "_gf_caption_metadata_seen", False))
    if not metadata_caption and not metadata_seen:
        return ""
    if metadata_caption:
        expand_post_caption(driver, metadata_hint=metadata_caption)

    # Live DOM is only trustworthy when canonical metadata supplies a prefix
    # tying the node to this post. Without that provenance, Facebook controls
    # such as comment sorting can reuse the same message-like attributes.
    if metadata_caption or metadata_seen:
        try:
            caption_elements = driver.find_elements(
                By.XPATH,
                '//div[@data-ad-comet-preview="message" or @data-ad-preview="message"]',
            )
            for caption_el in caption_elements:
                if _element_in_labeled_comment(caption_el):
                    continue
                txt = driver.execute_script("return arguments[0].innerText;", caption_el)
                if not txt:
                    continue
                cleaned = clean_caption_text(txt)
                if len(cleaned) < MIN_POST_CAPTION_CHARS:
                    _caption_trace(driver, status="rejected", source="message_dom",
                                   text=cleaned, reason="too_short")
                    continue
                if caption_is_truncated(cleaned):
                    _caption_trace(driver, status="rejected", source="message_dom",
                                   text=cleaned, reason="truncated")
                    continue
                if caption_is_interface_text(cleaned):
                    _caption_trace(driver, status="rejected", source="message_dom",
                                   text=cleaned, reason="interface_text")
                    continue
                _caption_trace(driver, status="selected", source="message_dom", text=cleaned)
                return cleaned
        except Exception:
            pass

        live_candidates = _live_caption_candidates(driver, metadata_caption)
        for candidate in live_candidates:
            if caption_is_truncated(candidate):
                _caption_trace(driver, status="rejected", source="live_candidate",
                               text=candidate, reason="truncated")
                continue
            if caption_is_interface_text(candidate):
                _caption_trace(driver, status="rejected", source="live_candidate",
                               text=candidate, reason="interface_text")
                continue
            _caption_trace(driver, status="selected", source="live_candidate", text=candidate)
            return candidate

    try:
        post_fbid = _media_fbid(getattr(driver, "current_url", ""))
        scripts = driver.find_elements(By.XPATH, '//script[@type="application/json"]')
        for s in scripts:
            content = s.get_attribute("innerHTML")
            if "message" in content and "text" in content:
                # JSON blobs also contain comments. Only inspect a bounded
                # region tied to the current post ID; otherwise skip it.
                if not post_fbid or post_fbid not in content:
                    continue
                position = content.find(post_fbid)
                scoped = content[max(0, position - 8000):position + 8000]
                matches = re.findall(r'"message":\{"text":"(.*?)"\}', scoped)
                for m in matches:
                    decoded = _decode_json_string_fragment(m)
                    cleaned = clean_caption_text(decoded)
                    metadata_prefix = re.sub(
                        r"(?:\.\.\.|…)\s*$", "", metadata_caption
                    ).strip()[:80].casefold()
                    if (
                        len(cleaned) > 20
                        and metadata_prefix in cleaned.casefold()
                        and not caption_is_interface_text(cleaned)
                    ):
                        _caption_trace(driver, status="selected", source="post_json", text=cleaned)
                        return cleaned
    except Exception:
        pass

    # A shortened metadata preview is evidence that the post exists, but it is
    # not safe to enqueue as an original caption. The caller can retry via the
    # canonical post permalink or report caption_truncated.
    if metadata_caption:
        _caption_trace(driver, status="selected", source="metadata_fallback", text=metadata_caption)
    return metadata_caption


def recover_full_caption_via_permalink(driver, caption: str) -> str:
    """Retry a shortened photo caption on its canonical post URL."""
    if (
        len(str(caption or "").strip()) >= MIN_POST_CAPTION_CHARS
        and not caption_is_truncated(caption)
        and not caption_is_interface_text(caption)
    ):
        return caption
    permalink = _canonical_post_permalink(driver)
    if not permalink:
        return caption
    try:
        driver.get(permalink)
        # Anonymous permalink pages hydrate the full message asynchronously.
        # A fixed 1.5-second read can see only truncated metadata even though
        # the complete message appears in the live DOM shortly afterwards.
        for delay in (1.5, 1.0, 1.0):
            time.sleep(delay)
            dismiss_login_popup(driver)
            recovered = extract_caption_all_sources(driver)
            if (
                len(str(recovered or "").strip()) >= MIN_POST_CAPTION_CHARS
                and not caption_is_truncated(recovered)
                and not caption_is_interface_text(recovered)
            ):
                return recovered
    except Exception:
        pass
    return caption


_THAI_DIGIT_TRANSLATION = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
_REACTION_TEXT_MARKERS = (
    "reaction", "reactions", "people reacted", "reacted to this",
    "ความรู้สึก", "การแสดงความรู้สึก", "ถูกใจสิ่งนี้",
)
_REACTION_MULTIPLIERS = {
    "k": 1_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
    "พัน": 1_000,
    "หมื่น": 10_000,
    "แสน": 100_000,
    "ล้าน": 1_000_000,
}


def parse_reaction_count(text: str) -> int | None:
    """Parse Facebook counts such as 1,234, 1.2K and 1.2 พัน."""
    normalized = html.unescape(str(text or "")).translate(_THAI_DIGIT_TRANSLATION)
    normalized = normalized.replace("\u00a0", " ").casefold()
    matches = re.findall(
        r"(?<!\d)(\d[\d,.]*)(?:\s*)(พัน|หมื่น|แสน|ล้าน|[kmb])?",
        normalized,
        flags=re.IGNORECASE,
    )
    values: list[int] = []
    for raw_number, raw_suffix in matches:
        suffix = raw_suffix.casefold()
        if suffix:
            compact = raw_number.replace(" ", "")
            if "," in compact and "." not in compact:
                tail = compact.rsplit(",", 1)[-1]
                compact = compact.replace(",", ".", 1) if len(tail) <= 2 else compact.replace(",", "")
            else:
                compact = compact.replace(",", "")
            try:
                values.append(int(float(compact) * _REACTION_MULTIPLIERS[suffix]))
            except (ValueError, KeyError):
                continue
        else:
            digits = re.sub(r"[^0-9]", "", raw_number)
            if digits:
                values.append(int(digits))
    return max(values) if values else None


def extract_reaction_count(
    driver,
    target_post_id: str = "",
) -> tuple[int | None, str, str]:
    """Return reactions for the primary post, never for comments below it."""
    candidates: list[tuple[int, str, str, int, float]] = []
    try:
        values = driver.execute_script(r"""
            const values = [];
            const seen = new Set();
            const reactionWords = /reaction|reacted|people reacted|ความรู้สึก|ถูกใจสิ่งนี้/i;
            const commentSortWords = /most relevant|all comments|newest|เกี่ยวข้องมากที่สุด|ความคิดเห็นทั้งหมด|ใหม่ล่าสุด/i;
            const compactCount = /^[\s\d๐-๙.,]+(?:k|m|b|พัน|หมื่น|แสน|ล้าน)?\s*$/i;
            const visible = node => {
              if (!node || !(node instanceof Element)) return false;
              const rect = node.getBoundingClientRect();
              const style = getComputedStyle(node);
              return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
            };
            const rectTop = node => Math.round(node.getBoundingClientRect().top + window.scrollY);
            const push = (value, node, priority, kind) => {
              value = String(value || '').replace(/\s+/g, ' ').trim();
              if (!value || !visible(node) || values.length >= 500) return;
              const key = `${value}|${rectTop(node)}|${kind}`;
              if (seen.has(key)) return;
              seen.add(key);
              values.push({value, top: rectTop(node), priority, kind});
            };

            let commentBoundary = Infinity;
            for (const node of document.querySelectorAll('[role="button"], [role="combobox"], span, div')) {
              if (!visible(node)) continue;
              const text = String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
              if (text.length <= 80 && commentSortWords.test(text)) {
                commentBoundary = Math.min(commentBoundary, rectTop(node));
              }
            }

            for (const node of document.querySelectorAll('[role="button"], [role="link"]')) {
              if (!visible(node) || rectTop(node) >= commentBoundary) continue;
              const context = [
                node.getAttribute('aria-label') || '',
                node.getAttribute('title') || '',
                node.innerText || ''
              ].join(' ');
              if (reactionWords.test(context)) push(context, node, 100, 'primary_control');
            }

            // Logged-out pages may render the count as a plain span beside icons.
            for (const node of document.querySelectorAll('span, div')) {
              if (!visible(node) || rectTop(node) >= commentBoundary) continue;
              const own = String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
              if (!compactCount.test(own) || own.length > 30) continue;
              let parent = node;
              for (let depth = 0; parent && depth < 6; depth++, parent = parent.parentElement) {
                const iconText = Array.from(parent.querySelectorAll('img[alt], [aria-label]'))
                  .slice(0, 30)
                  .map(el => `${el.getAttribute('alt') || ''} ${el.getAttribute('aria-label') || ''}`)
                  .join(' ');
                if (/like|love|care|haha|wow|sad|angry|ถูกใจ|รักเลย|ห่วงใย|ขำ|ว้าว|เศร้า|โกรธ/i.test(iconText)) {
                  push(own, node, 90 - depth, 'primary_icon_count');
                  break;
                }
              }
            }

            return values
              .filter(item => item.top < commentBoundary)
              .sort((a, b) => b.priority - a.priority || a.top - b.top);
        """) or []
    except Exception:
        values = []

    for index, value in enumerate(values):
        if isinstance(value, dict):
            raw_evidence = value.get("value", "")
            try:
                priority = int(value.get("priority", 0))
            except (TypeError, ValueError):
                priority = 0
            try:
                top = float(value.get("top", index))
            except (TypeError, ValueError):
                top = float(index)
            source = f"dom_{value.get('kind', 'primary')}"
        else:
            raw_evidence = value
            priority = 0
            top = float(index)
            source = "dom"
        evidence = re.sub(r"\s+", " ", str(raw_evidence or "")).strip()
        folded = evidence.casefold()
        if source == "dom" and not any(marker in folded for marker in _REACTION_TEXT_MARKERS):
            continue
        count = parse_reaction_count(evidence)
        if count is not None:
            candidates.append((count, evidence[:300], source, priority, top))

    dom_candidates = [item for item in candidates if item[2].startswith("dom")]
    if dom_candidates:
        chosen = max(dom_candidates, key=lambda item: (item[3], -item[4]))
        return chosen[0], chosen[1], chosen[2]

    # Never use a global reaction_count: those values commonly belong to comments.
    try:
        page_source = str(driver.page_source or "")
    except Exception:
        page_source = ""
    post_id = str(target_post_id or "").strip()
    if post_id and post_id.isdigit():
        id_pattern = re.compile(
            rf'(?:(?:"id"|"post_id"|"legacy_fbid"|"story_fbid")\s*:\s*"?{re.escape(post_id)}"?)',
            flags=re.IGNORECASE,
        )
        reaction_pattern = re.compile(
            r'"reaction_count"\s*:\s*(?:\{[^{}]{0,300}?"count"\s*:\s*)?(\d+)',
            flags=re.IGNORECASE,
        )
        for identity in id_pattern.finditer(page_source):
            start = max(0, identity.start() - 2_000)
            end = min(len(page_source), identity.end() + 8_000)
            window = page_source[start:end]
            matches = list(reaction_pattern.finditer(window))
            if matches:
                nearest = min(
                    matches,
                    key=lambda match: abs((start + match.start()) - identity.start()),
                )
                count = int(nearest.group(1))
                return count, nearest.group(0)[:300], "embedded_json_target_post"

    return None, "", "unavailable"


def _reaction_gate(
    driver,
    *,
    min_reactions: int,
    metadata: dict | None,
) -> str:
    target_post_id = _media_fbid(getattr(driver, "current_url", ""))
    count, evidence, source = extract_reaction_count(driver, target_post_id)
    if min_reactions > 0 and count is None:
        for _attempt in range(3):
            time.sleep(0.8)
            count, evidence, source = extract_reaction_count(driver, target_post_id)
            if count is not None:
                break
    if metadata is not None:
        metadata["reaction_count"] = count
        metadata["reaction_text"] = evidence
        metadata["reaction_source"] = source
        metadata["minimum_reactions_exclusive"] = int(min_reactions)
    if min_reactions <= 0:
        return ""
    if count is None:
        return "reaction_count_unavailable"
    if count <= min_reactions:
        return "reaction_below_threshold"
    return ""


def extract_image_all_sources(driver, save_image_path: str) -> bool:
    try:
        meta_tags = driver.find_elements(By.XPATH, '//meta[@property="og:image" or @property="og:image:secure_url"]')
        for tag in meta_tags:
            src = tag.get_attribute("content") or ""
            if ("scontent" in src or "fbcdn" in src):
                if download_hd_image(src, save_image_path):
                    return True
    except Exception:
        pass

    try:
        imgs = driver.find_elements(By.XPATH, '//img')
        candidates = []
        for img in imgs:
            src = img.get_attribute("src") or ""
            alt = img.get_attribute("alt") or ""
            aria_label = img.get_attribute("aria-label") or ""
            w = img.size.get("width", 0)
            h = img.size.get("height", 0)
            if _element_in_labeled_comment(img):
                continue
            if _contains_non_photo_marker(src, alt, aria_label):
                continue
            if ("scontent" in src or "fbcdn" in src) and (w >= MIN_POST_IMAGE_PX or h >= MIN_POST_IMAGE_PX):
                candidates.append((max(w, h), src))

        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return download_hd_image(candidates[0][1], save_image_path)
    except Exception:
        pass

    return False

# =========================================================================
# โซนลิงก์แบบที่ 2 (/posts/...) — แปลงเข้าโหมด Photo ทันที
# =========================================================================
def scrape_post_by_photo_redirect(
    driver,
    post_url: str,
    save_image_path: str,
    *,
    min_reactions: int = 0,
    metadata: dict | None = None,
):
    driver.get(post_url)
    time.sleep(1.2)  # ลดเวลาหน่วงลง
    dismiss_login_popup(driver)

    try:
        photo_links = driver.find_elements(By.XPATH, '//a[contains(@href, "/photo")]')
        if photo_links:
            ranked_links = []
            for link in photo_links:
                href = link.get_attribute("href") or ""
                if "fbid=" not in href and "/photos/" not in href:
                    continue
                score = 0
                try:
                    for image in link.find_elements(By.TAG_NAME, "img"):
                        if _contains_non_photo_marker(
                            image.get_attribute("src") or "",
                            image.get_attribute("alt") or "",
                        ):
                            continue
                        score = max(
                            score,
                            int(image.size.get("width", 0))
                            * int(image.size.get("height", 0)),
                        )
                except Exception:
                    pass
                ranked_links.append((score, href))
            ranked_links.sort(key=lambda item: item[0], reverse=True)
            target_photo_url = ranked_links[0][1] if ranked_links else ""
            if target_photo_url and "/photo" in target_photo_url:
                driver.get(target_photo_url)
                time.sleep(1.2)  # ลดเวลาหน่วงลง
                dismiss_login_popup(driver)
                reaction_error = _reaction_gate(
                    driver, min_reactions=min_reactions, metadata=metadata
                )
                if reaction_error:
                    return _caption_result(driver, post_url, False, "", reaction_error)
                has_img = extract_image_all_sources(driver, save_image_path)
                caption = extract_caption_all_sources(driver)
                caption = recover_full_caption_via_permalink(driver, caption)
                if has_img and caption:
                    if len(caption.strip()) < MIN_POST_CAPTION_CHARS:
                        return _caption_result(driver, post_url, False, "", "caption_too_short")
                    if caption_is_truncated(caption):
                        return _caption_result(driver, post_url, False, caption, "caption_truncated")
                    return _caption_result(driver, post_url, True, caption, "")
    except Exception:
        pass

    reaction_error = _reaction_gate(
        driver, min_reactions=min_reactions, metadata=metadata
    )
    if reaction_error:
        return _caption_result(driver, post_url, False, "", reaction_error)

    caption = extract_caption_all_sources(driver)
    caption = recover_full_caption_via_permalink(driver, caption)

    has_img = extract_image_all_sources(driver, save_image_path)

    if not caption:
        return _caption_result(
            driver, post_url, False, "",
            classify_post_scrape_failure(driver, has_image=has_img),
        )
    if len(caption.strip()) < MIN_POST_CAPTION_CHARS:
        return _caption_result(driver, post_url, False, "", "caption_too_short")
    if caption_is_interface_text(caption):
        return _caption_result(driver, post_url, False, "", "caption_interface_text")
    if caption_is_truncated(caption):
        return _caption_result(driver, post_url, False, caption, "caption_truncated")
    if not has_img:
        return _caption_result(driver, post_url, False, caption, "media_missing")

    return _caption_result(driver, post_url, True, caption, "")

# =========================================================================
# Main Scraper Direct
# =========================================================================
def scrape_fb_post_direct(
    post_url: str,
    save_image_path: str,
    *,
    driver=None,
    min_reactions: int = 0,
    metadata: dict | None = None,
):
    """Scrape public post data in an anonymous, temporary browser session."""
    owns_driver = driver is None
    try:
        if driver is None:
            # Never attach Facebook intake to a logged-in Chrome profile.  A
            # fresh browser data directory limits account-ban exposure and
            # keeps the public-page behavior reproducible.
            driver = init_driver(headless=True)
        try:
            setattr(driver, "_gf_caption_trace", [])
            setattr(driver, "_gf_caption_metadata_seen", False)
        except Exception:
            pass
        driver.set_page_load_timeout(25)

        if "/photo" in post_url:
            driver.get(post_url)
            time.sleep(1.2)  # ลดเวลาหน่วงลง
            dismiss_login_popup(driver)
            time.sleep(0.3)

            reaction_error = _reaction_gate(
                driver, min_reactions=min_reactions, metadata=metadata
            )
            if reaction_error:
                return _caption_result(driver, post_url, False, "", reaction_error)

            has_img = extract_image_all_sources(driver, save_image_path)
            caption = extract_caption_all_sources(driver)
            caption = recover_full_caption_via_permalink(driver, caption)

            if not caption or not has_img:
                driver.execute_script("window.scrollTo(0, 400);")
                time.sleep(0.8)
                dismiss_login_popup(driver)
                if not has_img:
                    has_img = extract_image_all_sources(driver, save_image_path)
                if not caption:
                    caption = extract_caption_all_sources(driver)
                    caption = recover_full_caption_via_permalink(driver, caption)

            if not caption:
                return _caption_result(
                    driver, post_url, False, "",
                    classify_post_scrape_failure(driver, has_image=has_img),
                )
            if len(caption.strip()) < MIN_POST_CAPTION_CHARS:
                return _caption_result(driver, post_url, False, "", "caption_too_short")
            if caption_is_interface_text(caption):
                error = "login_wall" if caption_is_login_wall_text(caption) else "caption_interface_text"
                return _caption_result(driver, post_url, False, "", error)
            if caption_is_truncated(caption):
                return _caption_result(driver, post_url, False, caption, "caption_truncated")
            if not has_img:
                return _caption_result(driver, post_url, False, caption, "media_missing")

            return _caption_result(driver, post_url, True, caption, "")

        else:
            return scrape_post_by_photo_redirect(
                driver,
                post_url,
                save_image_path,
                min_reactions=min_reactions,
                metadata=metadata,
            )

    except Exception as e:
        message = str(e)
        session_markers = (
            "invalid session id", "chrome not reachable", "disconnected",
            "tab crashed", "no such window", "target window already closed",
        )
        if owns_driver and driver is None:
            prefix = "browser_startup_failed: "
        elif any(marker in message.casefold() for marker in session_markers):
            prefix = "browser_session_failed: "
        else:
            prefix = "เกิดข้อผิดพลาด: "
        return _caption_result(driver, post_url, False, "", f"{prefix}{message}")
    finally:
        if owns_driver and driver:
            quit_driver_safely(driver)

# =========================================================================
# ฟังก์ชันครอบสำหรับใช้งานจริง (ปรับกลับเป็น headless=True เพื่อซ่อนจอเบื้องหลัง)
# =========================================================================
def scrape_fb_post_with_comments(
    post_url: str,
    save_image_path: str,
    save_comment_dir: str = None,
    *,
    source_page_id: str = "",
    source_page_url: str = "",
    collect_comments: bool = True,
    min_reactions: int = 0,
    metadata: dict | None = None,
    driver=None,
):
    """Scrape the post and owner-comment images with the supplied shared driver."""
    # 1. เรียกใช้ฟังก์ชันเดิมของคุณตรงๆ ก่อน (ดึงภาพหลัก + แคปชั่น)
    success, caption, err_msg = scrape_fb_post_direct(
        post_url,
        save_image_path,
        min_reactions=min_reactions,
        metadata=metadata,
        driver=driver,
    )
    
    # 2. เก็บคอมเมนต์ด้วย session เดิม ไม่เปิด Chrome ซ้ำต่อโพสต์
    if success and collect_comments and save_comment_dir:
        comment_driver = driver
        owns_comment_driver = comment_driver is None
        try:
            if comment_driver is None:
                comment_driver = init_driver(headless=True)
                comment_driver.get(post_url)
                time.sleep(1.0)
                dismiss_login_popup(comment_driver)
            scrape_page_comment_images(
                comment_driver,
                save_comment_dir,
                post_url=post_url,
                source_page_id=source_page_id,
                source_page_url=source_page_url,
            )
        except Exception as e:
            output_dir = Path(save_comment_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            _write_comment_manifest(output_dir, {
                "post_url": post_url,
                "status": "error",
                "error": f"{type(e).__name__}: {e}"[:500],
                "accepted": [],
                "rejected": [],
            })
            _safe_print(f"[!] ข้ามการดึงรูปคอมเมนต์เนื่องจากข้อผิดพลาด: {e}")
        finally:
            if owns_comment_driver and comment_driver:
                quit_driver_safely(comment_driver)

    return success, caption, err_msg

def _serializable_identity(identity: dict) -> dict:
    return {
        "ids": sorted(str(value) for value in identity.get("ids", ())),
        "keys": sorted(str(value) for value in identity.get("keys", ())),
        "names": sorted(str(value) for value in identity.get("names", ())),
        "sources": list(dict.fromkeys(identity.get("sources", ()))),
        "conflicts": list(dict.fromkeys(identity.get("conflicts", ()))),
    }


def _looks_like_comment_article(comment) -> bool:
    label = (comment.get_attribute("aria-label") or "").casefold()
    if any(marker in label for marker in _COMMENT_LABEL_MARKERS):
        return True
    try:
        if comment.find_elements(
            By.XPATH,
            './/div[@data-ad-comet-preview="message" or @data-ad-preview="message"]',
        ):
            return False
    except Exception:
        return False
    text = (getattr(comment, "text", "") or "").casefold()
    return "reply" in text or "ตอบกลับ" in text


def _comment_author_identity(comment) -> dict:
    identity = _empty_owner_identity()
    label = (comment.get_attribute("aria-label") or "").strip()
    label_folded = label.casefold()
    try:
        anchors = comment.find_elements(By.XPATH, './/a[@href]')
    except Exception:
        anchors = []
    preferred = []
    fallback = []
    for anchor in anchors:
        href = anchor.get_attribute("href") or ""
        if not _profile_like_anchor(href):
            continue
        name = (getattr(anchor, "text", "") or anchor.get_attribute("aria-label") or "").strip()
        item = (anchor, href, name)
        if name and name.casefold() in label_folded:
            preferred.append(item)
        else:
            fallback.append(item)
    candidates = preferred or fallback
    if not candidates:
        return identity
    _anchor, href, name = candidates[0]
    _add_identity_url(identity, href, "comment_author_link")
    if name:
        identity["names"].add(name.casefold())
    return identity


def _image_belongs_to_comment(img, comment) -> bool:
    try:
        nearest = img.find_element(By.XPATH, './ancestor::div[@role="article"][1]')
        return nearest == comment
    except Exception:
        return False


def _photo_attachment_href(img) -> str:
    try:
        anchor = img.find_element(By.XPATH, './ancestor::a[@href][1]')
        href = anchor.get_attribute("href") or ""
    except Exception:
        return ""
    lowered = href.lower()
    if "fbid=" in lowered or "/photo" in lowered or "photo.php" in lowered:
        return href
    return ""


def _best_image_source(img) -> str:
    srcset = img.get_attribute("srcset") or ""
    candidates = []
    for item in srcset.split(","):
        value = item.strip().split(" ", 1)[0]
        if value:
            candidates.append(value)
    return candidates[-1] if candidates else (img.get_attribute("src") or "")


def _select_all_comments(driver) -> bool:
    """Switch Facebook's ranking menu to All comments when available."""
    opener = r"""
        const patterns = [
          'most relevant', 'relevant most', 'newest',
          'เกี่ยวข้องมากที่สุด', 'ความคิดเห็นที่เกี่ยวข้องมากที่สุด', 'ใหม่ล่าสุด'
        ];
        const nodes = Array.from(document.querySelectorAll(
          '[role="button"], button, [aria-haspopup="menu"]'
        ));
        for (const node of nodes) {
          const value = (node.innerText || node.getAttribute('aria-label') || '')
            .replace(/\s+/g, ' ').trim().toLocaleLowerCase();
          if (!value || !patterns.some(item => value.includes(item))) continue;
          const rect = node.getBoundingClientRect();
          if (rect.width <= 0 || rect.height <= 0) continue;
          node.click();
          return true;
        }
        return false;
    """
    chooser = r"""
        const patterns = ['all comments', 'ความคิดเห็นทั้งหมด'];
        const nodes = Array.from(document.querySelectorAll(
          '[role="menuitem"], [role="option"], [role="button"], button'
        ));
        for (const node of nodes) {
          const value = (node.innerText || node.getAttribute('aria-label') || '')
            .replace(/\s+/g, ' ').trim().toLocaleLowerCase();
          if (!patterns.some(item => value === item || value.startsWith(item))) continue;
          const rect = node.getBoundingClientRect();
          if (rect.width <= 0 || rect.height <= 0) continue;
          node.click();
          return true;
        }
        return false;
    """
    try:
        if not driver.execute_script(opener):
            return False
        time.sleep(0.5)
        selected = bool(driver.execute_script(chooser))
        if selected:
            time.sleep(1.0)
        return selected
    except Exception:
        return False


def _expand_comment_surface(driver, *, rounds: int = 5) -> int:
    """Expose comments/replies that Facebook initially hides behind buttons."""
    total_clicked = 0
    _select_all_comments(driver)
    script = r"""
        const labels = [
          'view more comments', 'more comments',
          'view previous comments', 'view replies', 'more replies',
          'ดูความคิดเห็นเพิ่มเติม',
          'ดูความคิดเห็นก่อนหน้า', 'ดูการตอบกลับ', 'ดูคำตอบเพิ่มเติม'
        ];
        let clicked = 0;
        const nodes = document.querySelectorAll('[role="button"], button, a');
        for (const node of nodes) {
          const text = ((node.innerText || node.getAttribute('aria-label') || '')
            .replace(/\s+/g, ' ').trim().toLowerCase());
          const numberedReplies = /(?:^|\s)\d+\s+repl(?:y|ies)(?:\s|$)/i.test(text);
          const numberedThaiReplies = /(?:^|\s)\d+\s*(?:การตอบกลับ|คำตอบ)(?:\s|$)/i.test(text);
          if (!text || (!labels.some(label => text.includes(label)) &&
              !numberedReplies && !numberedThaiReplies)) continue;
          const rect = node.getBoundingClientRect();
          if (rect.width <= 0 || rect.height <= 0) continue;
          try { node.click(); clicked += 1; } catch (_) {}
        }
        window.scrollBy(0, Math.max(500, window.innerHeight * 0.8));
        return clicked;
    """
    quiet_rounds = 0
    for _ in range(max(1, rounds)):
        dismiss_login_popup(driver)
        try:
            clicked = int(driver.execute_script(script) or 0)
        except Exception:
            clicked = 0
        total_clicked += clicked
        quiet_rounds = quiet_rounds + 1 if clicked == 0 else 0
        time.sleep(1.0 if clicked else 0.4)
        if quiet_rounds >= 2:
            break
    return total_clicked


def _decode_embedded_url(value: str) -> str:
    candidate = html.unescape(str(value or "")).replace(r"\/", "/")
    candidate = candidate.replace(r"\u0025", "%").replace(r"\u0026", "&")
    return candidate


def _attachment_image_candidates(driver, attachment_url: str) -> list[str]:
    """Resolve a photo page to its full-size CDN image using browser cookies."""
    if not attachment_url:
        return []
    browser_candidates: list[tuple[int, str]] = []
    original_handle = None
    opened_handle = None
    try:
        original_handle = driver.current_window_handle
        driver.switch_to.new_window("tab")
        opened_handle = driver.current_window_handle
        driver.get(attachment_url)
        time.sleep(2.0)
        dismiss_login_popup(driver)
        rows = driver.execute_script(r"""
            return Array.from(document.images).map(image => ({
              src: image.currentSrc || image.src || '',
              width: image.naturalWidth || 0,
              height: image.naturalHeight || 0
            })).filter(item =>
              (item.src.includes('scontent') || item.src.includes('fbcdn')) &&
              item.width >= 200 && item.height >= 120
            );
        """) or []
        for row in rows:
            url = str(row.get("src", "") or "")
            area = int(row.get("width", 0) or 0) * int(row.get("height", 0) or 0)
            if url:
                browser_candidates.append((area, url))
    except Exception:
        pass

    finally:
        try:
            if opened_handle is not None:
                driver.close()
        except Exception:
            pass
        try:
            if original_handle is not None:
                driver.switch_to.window(original_handle)
        except Exception:
            pass

    unique_browser: list[str] = []
    for _area, url in sorted(browser_candidates, reverse=True):
        if url not in unique_browser:
            unique_browser.append(url)
    if unique_browser:
        return unique_browser

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": getattr(driver, "current_url", "") or "https://www.facebook.com/",
    }
    cookies = {}
    try:
        cookies = {
            str(item.get("name")): str(item.get("value"))
            for item in driver.get_cookies()
            if item.get("name")
        }
    except Exception:
        pass
    try:
        response = requests.get(
            attachment_url,
            headers=headers,
            cookies=cookies,
            timeout=20,
            allow_redirects=True,
        )
        if response.status_code != 200:
            return []
        page = response.text
    except Exception:
        return []

    candidates: list[str] = []
    meta_patterns = (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    )
    for pattern in meta_patterns:
        candidates.extend(re.findall(pattern, page, flags=re.IGNORECASE))
    candidates.extend(
        re.findall(r'(https?(?:\\/|/){2}(?:[^"\'<>\\]|\\.)*(?:scontent|fbcdn)(?:[^"\'<>\\]|\\.)+)', page)
    )
    unique: list[str] = []
    for value in candidates:
        decoded = _decode_embedded_url(value)
        if decoded.startswith("http") and decoded not in unique:
            unique.append(decoded)
    return unique


def _contains_non_photo_marker(*values: str) -> bool:
    combined = " ".join(str(value or "") for value in values).casefold()
    return any(marker in combined for marker in _NON_PHOTO_MEDIA_MARKERS)


def _media_fbid(url: str) -> str:
    try:
        query = parse_qs(urlsplit(url).query)
        value = str(query.get("fbid", [""])[0])
        return value if value.isdigit() else ""
    except Exception:
        return ""


def _extension_for_format(image_format: str) -> str:
    return {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(
        image_format, ".img"
    )


def _write_comment_manifest(directory: Path, manifest: dict) -> None:
    target = directory / COMMENT_MANIFEST_NAME
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, target)


# =========================================================================
# โซนเสริม: ดึงเฉพาะภาพนิ่งจากคอมเมนต์ของเจ้าของเพจ/โพสต์
# =========================================================================
def scrape_page_comment_images(
    driver,
    save_comment_dir: str,
    *,
    post_url: str = "",
    source_page_id: str = "",
    source_page_url: str = "",
) -> list:
    """Download proven owner-comment photos; skip uncertain authors and media."""
    output_dir = Path(save_comment_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded_comment_paths: list[str] = []
    owner = resolve_post_owner_identity(
        driver,
        post_url or getattr(driver, "current_url", ""),
        source_page_id=source_page_id,
        source_page_url=source_page_url,
    )
    manifest = {
        "post_url": post_url or getattr(driver, "current_url", ""),
        "owner_identity": _serializable_identity(owner),
        "accepted": [],
        "rejected": [],
        "status": "scanning",
        "comment_blocks": 0,
        "expansion_clicks": 0,
    }

    if not owner_identity_is_proven(owner):
        manifest["status"] = "owner_identity_unproven"
        _write_comment_manifest(output_dir, manifest)
        _safe_print("[!] ข้ามรูปคอมเมนต์: ไม่สามารถยืนยัน Page ID/เจ้าของโพสต์ได้")
        return []

    known_hashes: set[str] = set()
    for existing in output_dir.iterdir():
        if existing.is_file() and existing.name != COMMENT_MANIFEST_NAME:
            try:
                known_hashes.add(hashlib.sha256(existing.read_bytes()).hexdigest())
            except OSError:
                pass

    scan_error = ""
    scan_interrupted = False
    try:
        _safe_print("[*] 🔍 กำลังค้นหารูปภาพนิ่งจากคอมเมนต์ของเพจ...")
        manifest["expansion_clicks"] = _expand_comment_surface(driver)
        article_elements = driver.find_elements(By.XPATH, '//div[@role="article"]')
        comment_elements = [
            element for element in article_elements if _looks_like_comment_article(element)
        ]
        manifest["comment_blocks"] = len(comment_elements)
        _safe_print(f"[*] พบบล็อกคอมเมนต์ที่ตรวจสอบได้ {len(comment_elements)} รายการ")

        post_fbid = _media_fbid(post_url)
        img_counter = 1
        for comment_index, comment in enumerate(comment_elements, start=1):
            author = _comment_author_identity(comment)
            author_record = _serializable_identity(author)
            if not identities_match(owner, author):
                manifest["rejected"].append({
                    "comment_index": comment_index,
                    "reason": "comment_author_not_post_owner",
                    "author_identity": author_record,
                })
                continue

            try:
                comment_imgs = comment.find_elements(By.TAG_NAME, "img")
            except Exception:
                comment_imgs = []
            found_photo_attachment = False
            for img in comment_imgs:
                if not _image_belongs_to_comment(img, comment):
                    continue
                attachment_href = _photo_attachment_href(img)
                if not attachment_href:
                    continue
                found_photo_attachment = True
                src = _best_image_source(img)
                alt = img.get_attribute("alt") or ""
                aria_label = img.get_attribute("aria-label") or ""
                if _contains_non_photo_marker(src, attachment_href, alt, aria_label):
                    manifest["rejected"].append({
                        "comment_index": comment_index,
                        "reason": "sticker_gif_or_emoji_dom_marker",
                        "author_identity": author_record,
                        "attachment_url": attachment_href,
                    })
                    continue
                media_fbid = _media_fbid(attachment_href)
                if post_fbid and media_fbid == post_fbid:
                    manifest["rejected"].append({
                        "comment_index": comment_index,
                        "reason": "post_image_not_comment_attachment",
                        "author_identity": author_record,
                        "media_fbid": media_fbid,
                    })
                    continue
                if not src or not ("scontent" in src or "fbcdn" in src):
                    manifest["rejected"].append({
                        "comment_index": comment_index,
                        "reason": "missing_facebook_image_source",
                        "author_identity": author_record,
                        "attachment_url": attachment_href,
                    })
                    continue

                attachment_candidates: list[str] = []
                payload, metadata, resolved_url, error = _fetch_image_payload(src)
                initial_rejection = (
                    _comment_photo_rejection(metadata) if metadata is not None else ""
                )
                # Thumbnail URLs commonly remain only 261 px wide.  Resolve
                # the attachment page only when the direct CDN URL is absent
                # or still too small after removing stp/ctp transforms.
                if payload is None or initial_rejection in {
                    "image_too_small", "image_area_too_small"
                }:
                    attachment_candidates = _attachment_image_candidates(
                        driver, attachment_href
                    )
                    if attachment_candidates:
                        payload, metadata, resolved_url, error = _fetch_image_payload(
                            attachment_candidates[0],
                            [*attachment_candidates[1:], src],
                        )
                if payload is None or metadata is None:
                    manifest["rejected"].append({
                        "comment_index": comment_index,
                        "reason": "image_download_failed",
                        "error": error,
                        "author_identity": author_record,
                        "attachment_url": attachment_href,
                        "candidate_count": len(attachment_candidates) + 1,
                    })
                    continue
                rejection = _comment_photo_rejection(metadata)
                if rejection:
                    manifest["rejected"].append({
                        "comment_index": comment_index,
                        "reason": rejection,
                        "author_identity": author_record,
                        "attachment_url": attachment_href,
                        "resolved_image_url": resolved_url,
                        "image": metadata,
                    })
                    continue
                if metadata["sha256"] in known_hashes:
                    manifest["rejected"].append({
                        "comment_index": comment_index,
                        "reason": "duplicate_image_content",
                        "author_identity": author_record,
                        "attachment_url": attachment_href,
                        "image": metadata,
                    })
                    continue

                suffix = _extension_for_format(metadata["format"])
                comment_img_path = output_dir / f"comment_img_{img_counter}{suffix}"
                comment_img_path.write_bytes(payload)
                known_hashes.add(metadata["sha256"])
                accepted = {
                    "comment_index": comment_index,
                    "path": str(comment_img_path),
                    "author_identity": author_record,
                    "attachment_url": attachment_href,
                    "resolved_image_url": resolved_url,
                    "media_fbid": media_fbid,
                    "image": metadata,
                }
                manifest["accepted"].append(accepted)
                downloaded_comment_paths.append(str(comment_img_path))
                img_counter += 1
                _safe_print(f"[+] 📥 ดาวน์โหลดภาพนิ่งจากคอมเมนต์ของเพจ: {comment_img_path}")

            if not found_photo_attachment:
                manifest["rejected"].append({
                    "comment_index": comment_index,
                    "reason": "no_photo_attachment",
                    "author_identity": author_record,
                })

    except BaseException as exc:
        scan_error = f"{type(exc).__name__}: {exc}"[:500]
        manifest["error"] = scan_error
        scan_interrupted = isinstance(exc, (KeyboardInterrupt, SystemExit))
        if scan_interrupted:
            _safe_print(f"[!] การดึงรูปคอมเมนต์ถูกขัดจังหวะ: {type(exc).__name__}")
            raise
        _safe_print(f"[!] เกิดข้อผิดพลาดในการดึงรูปคอมเมนต์: {exc}")
    finally:
        manifest["status"] = (
            "interrupted" if scan_interrupted else "error" if scan_error else "complete"
        )
        manifest["accepted_count"] = len(manifest["accepted"])
        manifest["rejected_count"] = len(manifest["rejected"])
        _write_comment_manifest(output_dir, manifest)

    _safe_print(
        f"[*] 🏁 สรุป: รับภาพนิ่งจากคอมเมนต์ของเพจ "
        f"{len(downloaded_comment_paths)} รูป | ปฏิเสธ {len(manifest['rejected'])} รายการ"
    )
    return downloaded_comment_paths
