"""Read Hupu thread bodies and first-page replies from NewsNow detail URLs."""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from . import comments, dates, page_content


_HUPU_HOSTS = {"bbs.hupu.com"}
_THREAD_PATH_RE = re.compile(r"/\d+(?:_\d+)?\.html$")
_NEXT_DATA_RE = re.compile(
    r"<script[^>]*\bid=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>",
    flags=re.I | re.S,
)


def _validate_hupu_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in _HUPU_HOSTS
        or not _THREAD_PATH_RE.fullmatch(parsed.path)
    ):
        raise ValueError(f"不支持的虎扑帖子 URL: {url!r}")


def _parse_next_data(page_html: str) -> Mapping[str, Any]:
    match = _NEXT_DATA_RE.search(page_html)
    if not match:
        raise ValueError("虎扑详情页缺少 __NEXT_DATA__")
    payload = json.loads(match.group(1))
    detail = ((payload.get("props") or {}).get("pageProps") or {}).get("detail")
    if not isinstance(detail, Mapping):
        raise ValueError("虎扑 __NEXT_DATA__ 缺少 detail object")
    return detail


def _thread_body(thread: Mapping[str, Any]) -> str:
    content = comments.clean_text(thread.get("content"))
    if content:
        return content
    try:
        format_payload = json.loads(str(thread.get("format") or "{}"))
    except json.JSONDecodeError:
        return ""
    if not isinstance(format_payload, Mapping):
        return ""
    return comments.clean_text(format_payload.get("htmlV3"))


def _parse_replies(detail: Mapping[str, Any]) -> list[dict[str, Any]]:
    reply_page = detail.get("replies")
    raw_replies = reply_page.get("list") if isinstance(reply_page, Mapping) else []
    parsed: list[dict[str, Any]] = []
    for raw in raw_replies if isinstance(raw_replies, list) else []:
        if not isinstance(raw, Mapping):
            continue
        created_at = comments.timestamp(raw.get("createdAt"), milliseconds=True)
        value = comments.entry(
            raw.get("pid"),
            raw.get("content"),
            created_at=created_at,
            likes=raw.get("allLightCount") or raw.get("count"),
            reply_count=raw.get("replyNum"),
        )
        if value:
            parsed.append(value)
        if len(parsed) >= comments.limit():
            break
    return parsed


def read_hupu_thread(url: str, *, expected_title: str = "") -> dict[str, Any]:
    """Return one readable Hupu thread with its first public reply page."""
    _validate_hupu_url(url)
    final_url, page_html = page_content.fetch_page(
        url,
        referer="https://bbs.hupu.com/",
        timeout=20,
        throttle_key="hupu",
    )
    _validate_hupu_url(final_url)
    detail = _parse_next_data(page_html)
    thread = detail.get("thread")
    if not isinstance(thread, Mapping):
        raise ValueError("虎扑详情数据缺少 thread object")
    content = _thread_body(thread)
    if not content:
        raise ValueError("虎扑帖子没有可读正文")

    author = thread.get("author") if isinstance(thread.get("author"), Mapping) else {}
    created_at = thread.get("createdAt")
    item: dict[str, Any] = {
        "platform_id": str(thread.get("tid") or ""),
        "title": str(thread.get("title") or expected_title).strip(),
        "text": content,
        "url": final_url,
        "author_handle": str(author.get("puname") or ""),
        "author_id": str(thread.get("authorId") or author.get("puid") or ""),
        "date": (
            dates.timestamp_to_date(float(created_at) / 1000)
            if created_at not in (None, "")
            else None
        ),
        "engagement": {
            "views": comments.count(thread.get("read")),
            "likes": comments.count(thread.get("lights")),
            "replies": comments.count(thread.get("replies")),
            "recommends": comments.count(thread.get("recommend")),
        },
        "source": "hupu-next-data",
    }
    page_content.apply_content(
        item,
        content,
        content_type="post-body",
        content_source="hupu-next-data",
        content_url=final_url,
    )
    parsed_comments = _parse_replies(detail)
    expected_comments = comments.count(thread.get("replies")) > 0
    comments.apply(
        item,
        parsed_comments,
        status="unavailable" if expected_comments else "empty",
        source="hupu-next-data-replies",
        evidence={
            "kind": "embedded-json",
            "object": "__NEXT_DATA__",
            "path": "props.pageProps.detail.replies.list",
            "page": 1,
        },
    )
    return item


def collect_hotlist_threads(
    hotlist_items: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Read selected Hupu hot-list URLs while preserving input order."""
    results: list[dict[str, Any]] = []
    for hotlist_item in hotlist_items:
        query = str(hotlist_item.get("title") or "").strip()
        try:
            post = read_hupu_thread(
                str(hotlist_item.get("url") or ""),
                expected_title=query,
            )
            results.append({
                "query": query,
                "status": "readable",
                "post_count": 1,
                "posts": [post],
                "error": None,
            })
        except Exception as exc:
            results.append({
                "query": query,
                "status": "error",
                "post_count": 0,
                "posts": [],
                "error": f"{type(exc).__name__}: {exc}",
            })
    return results
