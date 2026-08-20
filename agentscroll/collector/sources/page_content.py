"""Fetch public detail pages and extract their readable main text."""

from __future__ import annotations

import html
import ipaddress
import json
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

from . import throttle
from .request_profiles import navigation_headers


_SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "template"}
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "div", "figcaption",
    "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "li", "main", "nav", "p", "section", "table", "td", "th", "tr",
}
_CONTENT_MARKERS = {
    "article-body", "article_body", "article-content", "article_content",
    "content-body", "content_body", "entry-content", "entry_content",
    "js_content", "main-content", "main_content", "mw-content-text",
    "note-content", "post-content", "post_content", "rich_media_content",
    "syl-article-base",
}


def _clean_text(value: str) -> str:
    value = html.unescape(value or "").replace("\u200b", "").replace("\ufeff", "")
    value = re.sub(r"[\t\f\v ]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _validate_public_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"不支持的正文 URL: {url!r}")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".local"):
        raise ValueError(f"拒绝访问本地正文 URL: {url!r}")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError(f"拒绝访问非公网正文 URL: {url!r}")


def _decode_page(raw: bytes, charset: Optional[str], content_type: str) -> str:
    encodings: List[str] = []
    if charset:
        encodings.append(charset)
    head = raw[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", head, re.I)
    if match:
        encodings.append(match.group(1))
    if "gb" in content_type.lower():
        encodings.append("gb18030")
    encodings.extend(("utf-8", "gb18030"))
    for encoding in dict.fromkeys(encodings):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_page(
    url: str,
    *,
    referer: Optional[str] = None,
    mobile: bool = False,
    timeout: int = 15,
    max_bytes: int = 3_000_000,
    opener: Optional[urllib.request.OpenerDirector] = None,
    throttle_key: Optional[str] = None,
) -> Tuple[str, str]:
    """Return ``(final_url, html)`` for one public HTTP(S) page."""
    _validate_public_url(url)
    platform = throttle_key or urllib.parse.urlsplit(url).hostname or "detail"
    throttle.wait_for_detail_request(platform)
    request = urllib.request.Request(
        url,
        headers=navigation_headers(referer, mobile=mobile),
    )
    request_opener = opener or urllib.request.build_opener()
    with request_opener.open(request, timeout=timeout) as response:
        final_url = response.geturl()
        _validate_public_url(final_url)
        raw = response.read(max_bytes + 1)[:max_bytes]
        charset = response.headers.get_content_charset()
        content_type = response.headers.get("Content-Type", "")
    return final_url, _decode_page(raw, charset, content_type)


class _ReadableTextParser(HTMLParser):
    def __init__(
        self,
        preferred_ids: set[str],
        preferred_classes: set[str],
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.preferred_ids = preferred_ids
        self.preferred_classes = preferred_classes
        self.depth = 0
        self.skip_depth = 0
        self.captures: List[Dict[str, Any]] = []
        self.candidates: List[Tuple[int, str]] = []
        self.paragraph_depth: Optional[int] = None
        self.paragraph_parts: List[str] = []
        self.paragraphs: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        self.depth += 1
        if self.skip_depth:
            self.skip_depth += 1
            return
        if tag in _SKIP_TAGS:
            self.skip_depth = 1
            return

        attributes = {key.lower(): (value or "") for key, value in attrs}
        element_id = attributes.get("id", "").lower()
        class_tokens = set(attributes.get("class", "").lower().split())
        priority = 0
        if element_id in self.preferred_ids or class_tokens & self.preferred_classes:
            priority = 5
        elif element_id in _CONTENT_MARKERS or class_tokens & _CONTENT_MARKERS:
            priority = 4
        elif tag == "article":
            priority = 3
        elif tag == "main":
            priority = 2
        if priority:
            self.captures.append({
                "depth": self.depth,
                "priority": priority,
                "parts": [],
            })

        if tag in _BLOCK_TAGS:
            for capture in self.captures:
                capture["parts"].append("\n")
        if tag == "p" and self.paragraph_depth is None:
            self.paragraph_depth = self.depth
            self.paragraph_parts = []
        if tag in _VOID_TAGS:
            self.depth = max(0, self.depth - 1)

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self.skip_depth or not data.strip():
            return
        for capture in self.captures:
            capture["parts"].append(data)
        if self.paragraph_depth is not None:
            self.paragraph_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skip_depth:
            self.skip_depth -= 1
            self.depth = max(0, self.depth - 1)
            return

        if tag in _BLOCK_TAGS:
            for capture in self.captures:
                capture["parts"].append("\n")
        if tag == "p" and self.paragraph_depth is not None:
            paragraph = _clean_text("".join(self.paragraph_parts))
            if len(paragraph) >= 20:
                self.paragraphs.append(paragraph)
            self.paragraph_depth = None
            self.paragraph_parts = []

        remaining = []
        for capture in self.captures:
            if capture["depth"] == self.depth:
                text = _clean_text("".join(capture["parts"]))
                if text:
                    self.candidates.append((capture["priority"], text))
            else:
                remaining.append(capture)
        self.captures = remaining
        self.depth = max(0, self.depth - 1)

    def close(self) -> None:
        super().close()
        for capture in self.captures:
            text = _clean_text("".join(capture["parts"]))
            if text:
                self.candidates.append((capture["priority"], text))
        self.captures = []


def _article_body_from_json(value: Any) -> str:
    if isinstance(value, dict):
        body = value.get("articleBody")
        if isinstance(body, str) and len(_clean_text(body)) >= 40:
            return _clean_text(body)
        for child in value.values():
            found = _article_body_from_json(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _article_body_from_json(child)
            if found:
                return found
    return ""


def _extract_json_ld_body(page_html: str) -> str:
    scripts = re.findall(
        r"<script[^>]+type=['\"]application/ld\+json['\"][^>]*>(.*?)</script>",
        page_html,
        flags=re.I | re.S,
    )
    for raw in scripts:
        try:
            parsed = json.loads(html.unescape(raw).strip())
        except (json.JSONDecodeError, TypeError):
            continue
        body = _article_body_from_json(parsed)
        if body:
            return body
    return ""


def extract_main_text(
    page_html: str,
    *,
    preferred_ids: Tuple[str, ...] = (),
    preferred_classes: Tuple[str, ...] = (),
    max_chars: int = 100_000,
) -> str:
    """Extract article-like text without treating navigation/scripts as content."""
    json_body = _extract_json_ld_body(page_html)
    if json_body:
        return json_body[:max_chars]

    parser = _ReadableTextParser(
        {value.lower() for value in preferred_ids},
        {value.lower() for value in preferred_classes},
    )
    parser.feed(page_html)
    parser.close()
    candidates = [item for item in parser.candidates if len(item[1]) >= 40]
    if candidates:
        priority = max(item[0] for item in candidates)
        best = max(
            (text for item_priority, text in candidates if item_priority == priority),
            key=len,
        )
        return best[:max_chars]

    paragraphs = _clean_text("\n\n".join(parser.paragraphs))
    return paragraphs[:max_chars] if len(paragraphs) >= 80 else ""


def extract_selector_text(
    page_html: str,
    *,
    preferred_ids: Tuple[str, ...] = (),
    preferred_classes: Tuple[str, ...] = (),
    min_chars: int = 1,
    max_chars: int = 100_000,
) -> str:
    """Extract only an explicitly named platform container, without generic fallback."""
    parser = _ReadableTextParser(
        {value.lower() for value in preferred_ids},
        {value.lower() for value in preferred_classes},
    )
    parser.feed(page_html)
    parser.close()
    candidates = [
        text
        for priority, text in parser.candidates
        if priority == 5 and len(text) >= min_chars
    ]
    return max(candidates, key=len)[:max_chars] if candidates else ""


def apply_content(
    item: Dict[str, Any],
    content: str,
    *,
    content_type: str,
    content_source: str,
    content_url: Optional[str] = None,
) -> None:
    """Attach the common detail-content contract to one source item."""
    item["content"] = _clean_text(content)
    item["content_type"] = content_type
    item["content_source"] = content_source
    item["content_url"] = content_url or item.get("url", "")


def apply_summary_fallback(
    item: Dict[str, Any],
    *values: Any,
    content_source: str = "search-result",
) -> None:
    """Expose a summary explicitly when a detail page could not be read."""
    content = next(
        (_clean_text(str(value)) for value in values if _clean_text(str(value or ""))),
        "",
    )
    apply_content(
        item,
        content,
        content_type="search-summary" if content else "unavailable",
        content_source=content_source,
    )


def retain_readable_details(
    items: List[Dict[str, Any]],
    *,
    allowed_sources: set[str],
    min_chars: int = 2,
) -> List[Dict[str, Any]]:
    """Drop link/summary-only results that did not yield readable detail content."""
    return [
        item
        for item in items
        if item.get("content_type") in {"article-body", "post-description"}
        and item.get("content_source") in allowed_sources
        and len(str(item.get("content") or "").strip()) >= min_chars
        and bool(item.get("content_url"))
    ]
