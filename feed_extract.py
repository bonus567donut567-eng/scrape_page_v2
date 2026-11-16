"""Pure helpers for extracting post-like articles from HTML.

The module performs no network access and deliberately avoids Facebook private APIs.
"""

from __future__ import annotations

import hashlib
import html as html_module
import re
from dataclasses import asdict, dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag


TRACKING_KEYS = {
    "__cft__",
    "__tn__",
    "comment_id",
    "notif_id",
    "notif_t",
    "ref",
    "refid",
    "refsrc",
}


@dataclass(frozen=True)
class Post:
    id: str
    text: str
    permalink: str | None
    time_text: str | None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def canonicalize_url(href: str | None, base_url: str) -> str | None:
    if not href or href.startswith(("javascript:", "#")):
        return None
    absolute = urljoin(base_url, href)
    parts = urlsplit(absolute)
    if parts.scheme not in {"http", "https"}:
        return None
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        is_tracking = (
            lowered in TRACKING_KEYS
            or lowered.startswith("utm_")
            or lowered.startswith("__cft__")
            or lowered.startswith("__tn__")
        )
        if not is_tracking:
            query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, urlencode(query), ""))


def _best_permalink(node: Tag, base_url: str) -> str | None:
    candidates: list[tuple[int, str]] = []
    for anchor in node.select("a[href]"):
        href = canonicalize_url(anchor.get("href"), base_url)
        if not href:
            continue
        score = _permalink_score(href)
        if score == 0:
            continue
        if anchor.find("time") or anchor.get("aria-label"):
            score += 2
        candidates.append((score, href))
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def _permalink_score(url: str) -> int:
    """Recognize common public Facebook post permalink shapes.

    Avoid treating profile, navigation, reaction, and comment links as posts.
    """
    parts = urlsplit(url)
    path = parts.path.lower().rstrip("/")
    query_keys = {key.lower() for key, _ in parse_qsl(parts.query, keep_blank_values=True)}
    if "/posts/" in path:
        return 50
    if path.endswith("/photo") or path.endswith("/photo.php"):
        return 50 if "fbid" in query_keys else 0
    if path.endswith("/permalink.php") or path.endswith("/story.php"):
        return 45 if "story_fbid" in query_keys else 0
    if "/videos/" in path or "/reel/" in path:
        return 40
    return 0


def _time_text(node: Tag) -> str | None:
    time_node = node.find("time")
    if time_node:
        value = time_node.get("datetime") or time_node.get_text(" ", strip=True)
        return clean_text(value) or None

    for selector in ('abbr[data-utime]', 'a[aria-label]'):
        candidate = node.select_one(selector)
        if candidate:
            value = candidate.get("data-utime") or candidate.get("aria-label")
            if value:
                return clean_text(str(value))
    return None


def _article_nodes(soup: BeautifulSoup) -> list[Tag]:
    nodes = list(soup.select('[role="article"]'))
    if not nodes:
        nodes = list(soup.select("article"))
    return nodes


def extract_posts(html: str, base_url: str) -> list[Post]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[Post] = []
    seen: set[str] = set()

    for node in _article_nodes(soup):
        text = clean_text(node.get_text(" ", strip=True))
        if not text:
            continue
        permalink = _best_permalink(node, base_url)
        identity = permalink or text
        post_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        if post_id in seen:
            continue
        seen.add(post_id)
        results.append(
            Post(
                id=post_id,
                text=text,
                permalink=permalink,
                time_text=_time_text(node),
            )
        )
    return results


def candidate_count(html: str) -> int:
    return len(_article_nodes(BeautifulSoup(html, "html.parser")))


def extract_embedded_permalinks(html: str, base_url: str) -> list[str]:
    """Extract post/photo/video permalinks from rendered DOM and embedded JSON.

    Facebook often includes links for visible photo-grid items in serialized React
    data even when those items are not represented as feed articles.
    """
    soup = BeautifulSoup(html, "html.parser")
    raw_candidates = [str(tag.get("href")) for tag in soup.select("a[href]")]

    normalized = html_module.unescape(
        html.replace(r"\/", "/")
        .replace(r"\u0025", "%")
        .replace(r"\u0026", "&")
        .replace(r"\u003A", ":")
        .replace(r"\u003D", "=")
        .replace(r"\u003F", "?")
    )
    patterns = (
        r"https?://(?:www\.)?facebook\.com/[^\"'<>\s]+/posts/[A-Za-z0-9]+[^\"'<>\s]*",
        r"(?:https?://(?:www\.)?facebook\.com)?/photo(?:\.php)?/?\?[^\"'<>\s]{0,900}?fbid=\d+[^\"'<>\s]*",
        r"https?://(?:www\.)?facebook\.com/(?:reel|watch)/[^\"'<>\s]+",
        r"https?://(?:www\.)?facebook\.com/[^\"'<>\s]+/videos/[^\"'<>\s]+",
    )
    for pattern in patterns:
        raw_candidates.extend(re.findall(pattern, normalized, flags=re.IGNORECASE))

    return dedupe_permalinks(raw_candidates, base_url)


def dedupe_permalinks(candidates: list[str], base_url: str = "https://www.facebook.com/") -> list[str]:
    """Canonicalize links and keep one preferred URL per Facebook content ID."""
    links_by_content: dict[str, str] = {}
    for candidate in candidates:
        url = canonicalize_url(candidate.replace("\\", ""), base_url)
        if not url or _permalink_score(url) == 0:
            continue
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if "fbid" in query and parts.path.lower().rstrip("/").endswith(("/photo", "/photo.php")):
            identity = "photo:" + query["fbid"]
        else:
            identity = parts.path.rstrip("/").lower()

        current = links_by_content.get(identity)
        # Prefer an album-specific photo URL (set=a.*) over the generic profile
        # photo stream (set=pb.*), matching links users get from an open post.
        new_set = query.get("set", "")
        current_set = dict(parse_qsl(urlsplit(current).query)).get("set", "") if current else ""
        if current is None or (new_set.startswith("a.") and not current_set.startswith("a.")):
            links_by_content[identity] = url
    return list(links_by_content.values())
