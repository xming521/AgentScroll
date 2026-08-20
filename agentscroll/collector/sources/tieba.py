"""Resolve Tieba hot topics to readable posts and public floor replies."""

from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping
from urllib.parse import urljoin, urlsplit

from . import comments, crawler_bridge, page_content, throttle


_TIEBA_HOSTS = {"tieba.baidu.com"}
_HOT_TOPIC_PATH = "/hottopic/browse/hottopic"
_POST_PATH_RE = re.compile(r"/p/(\d+)")
_PAGE_DATA_RE = re.compile(r"\bPageData\.topic\s*=\s*")


def _validate_tieba_url(url: str, *, post: bool = False) -> None:
    parsed = urlsplit(url)
    valid_path = bool(_POST_PATH_RE.fullmatch(parsed.path)) if post else (
        parsed.path == _HOT_TOPIC_PATH
    )
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in _TIEBA_HOSTS
        or not valid_path
    ):
        kind = "帖子" if post else "热榜话题"
        raise ValueError(f"不支持的贴吧{kind} URL: {url!r}")


def _topic_data(page_html: str) -> dict[str, Any]:
    match = _PAGE_DATA_RE.search(page_html)
    if not match:
        return {}
    start = page_html.find("{", match.end())
    if start < 0:
        return {}
    try:
        value, _ = json.JSONDecoder().raw_decode(page_html[start:])
    except json.JSONDecodeError:
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


class _HotThreadParser(HTMLParser):
    """Extract server-rendered thread cards without executing page scripts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._li_depth = 0
        self._item: dict[str, Any] | None = None
        self._capture_tag = ""
        self._capture_parts: list[str] = []
        self.items: list[dict[str, Any]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.lower()
        attributes = {key.lower(): value or "" for key, value in attrs}
        classes = set(attributes.get("class", "").split())
        if tag == "li":
            if self._item is not None:
                self._li_depth += 1
            elif "thread-item" in classes:
                self._item = {}
                self._li_depth = 1
        if self._item is None or self._capture_tag:
            return
        if tag == "a" and "title" in classes:
            self._item["url"] = urljoin(
                "https://tieba.baidu.com/",
                attributes.get("href", ""),
            )
            self._capture_tag = tag
        elif tag == "p" and "content" in classes:
            self._capture_tag = tag
        elif tag == "span" and "reply-num" in classes:
            self._capture_tag = tag
        if self._capture_tag:
            self._capture_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_tag:
            self._capture_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._item is not None and tag == self._capture_tag:
            value = comments.clean_text("".join(self._capture_parts))
            if tag == "a":
                self._item["title"] = value
            elif tag == "p":
                self._item["summary"] = value
            elif tag == "span":
                self._item["replies"] = comments.count(value)
            self._capture_tag = ""
            self._capture_parts = []
        if self._item is None or tag != "li":
            return
        self._li_depth -= 1
        if self._li_depth:
            return
        url = str(self._item.get("url") or "")
        if _POST_PATH_RE.fullmatch(urlsplit(url).path):
            self.items.append(self._item)
        self._item = None


def _thread_candidates(page_html: str) -> list[dict[str, Any]]:
    parser = _HotThreadParser()
    parser.feed(page_html)
    parser.close()
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in parser.items:
        url = str(item.get("url") or "")
        if url in seen:
            continue
        seen.add(url)
        unique.append(item)
    return unique


def _prepare_topic(hotlist_item: Mapping[str, Any]) -> dict[str, Any]:
    query = str(hotlist_item.get("title") or "").strip()
    url = html.unescape(str(hotlist_item.get("url") or ""))
    _validate_tieba_url(url)
    final_url, page_html = page_content.fetch_page(
        url,
        referer="https://tieba.baidu.com/",
        timeout=20,
        throttle_key="tieba",
    )
    _validate_tieba_url(final_url)
    topic = _topic_data(page_html)
    return {
        "query": query,
        "topic": {
            "topic_id": str(topic.get("topic_id") or ""),
            "title": str(topic.get("topic_name") or query).strip(),
            "summary": comments.clean_text(topic.get("topic_desc")),
            "url": final_url,
        },
        "candidates": _thread_candidates(page_html),
    }


def _dom_texts(page: Any, selector: str) -> list[str]:
    values = page.eval_on_selector_all(
        selector,
        """nodes => nodes.map(node => {
            const text = (node.innerText || '').trim();
            const images = Array.from(node.querySelectorAll('img'))
                .map(img => (img.alt || img.title || '').trim())
                .filter(Boolean);
            return [text, ...images].filter(Boolean).join(' ');
        })""",
    )
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values if isinstance(values, list) else []:
        text = comments.clean_text(value)
        if text and text not in seen:
            seen.add(text)
            normalized.append(text)
    return normalized


def _comment_date(metadata: str) -> str | None:
    match = re.search(
        r"(?:\d{4}-)?\d{2}-\d{2}(?:\s+\d{2}:\d{2})?",
        metadata,
    )
    return match.group(0) if match else None


def _parse_dom_comments(page: Any) -> list[dict[str, Any]]:
    raw_values = page.eval_on_selector_all(
        ".pb-comment-item",
        """roots => roots.map((root, rootIndex) => {
            const textOf = node => {
                if (!node) return '';
                const text = (node.innerText || '').trim();
                const images = Array.from(node.querySelectorAll('img'))
                    .map(img => (img.alt || img.title || '').trim())
                    .filter(Boolean);
                return [text, ...images].filter(Boolean).join(' ');
            };
            const direct = root.querySelector(
                ':scope > .comment-content > .pb-rich-text'
            );
            const metadata = root.querySelector(
                ':scope > .comment-content > .pc-pb-comments-desc'
            );
            const rootId = root.getAttribute('data-id') || `floor-${rootIndex + 1}`;
            const nested = Array.from(root.querySelectorAll('.pb-lzl-item')).map(
                (reply, replyIndex) => ({
                    id: reply.getAttribute('data-id') || `${rootId}-${replyIndex + 1}`,
                    text: textOf(reply.querySelector('.pb-rich-text')),
                    metadata: textOf(reply.querySelector('.pc-pb-comments-desc')),
                })
            );
            return {
                id: rootId,
                text: textOf(direct),
                metadata: textOf(metadata),
                nested,
            };
        })""",
    )
    parsed: list[dict[str, Any]] = []
    for root in raw_values if isinstance(raw_values, list) else []:
        if not isinstance(root, Mapping):
            continue
        root_id = str(root.get("id") or "")
        metadata = comments.clean_text(root.get("metadata"))
        reply_match = re.search(r"(\d+)\s*回复", metadata)
        entry = comments.entry(
            root_id,
            root.get("text"),
            created_at=_comment_date(metadata),
            reply_count=reply_match.group(1) if reply_match else None,
        )
        if entry:
            parsed.append(entry)
        nested = root.get("nested")
        for reply in nested if isinstance(nested, list) else []:
            if not isinstance(reply, Mapping):
                continue
            reply_metadata = comments.clean_text(reply.get("metadata"))
            value = comments.entry(
                reply.get("id"),
                reply.get("text"),
                created_at=_comment_date(reply_metadata),
                parent_comment_id=root_id,
            )
            if value:
                parsed.append(value)
            if len(parsed) >= comments.limit():
                return parsed
    return parsed


def _read_post(page: Any, candidate: Mapping[str, Any]) -> dict[str, Any]:
    url = str(candidate.get("url") or "")
    _validate_tieba_url(url, post=True)
    throttle.wait_for_detail_request("tieba")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    except Exception:
        # Tieba may keep background requests open after the usable DOM is ready.
        pass
    page.wait_for_selector(".pb-content-wrap", timeout=15_000)
    page.wait_for_timeout(1_500)

    final_url = page.url
    _validate_tieba_url(final_url, post=True)
    body_parts = _dom_texts(page, ".pb-content-wrap .richtext-item")
    content = "\n".join(body_parts)
    if not content:
        raise ValueError("贴吧帖子没有可读正文")

    title = str(page.title() or candidate.get("title") or "").strip()
    title = re.sub(r"\s*[-_]百度贴吧\s*$", "", title).strip()
    path_match = _POST_PATH_RE.fullmatch(urlsplit(final_url).path)
    total_replies = comments.count(candidate.get("replies"))
    if not total_replies:
        page_text = page.locator("body").inner_text(timeout=5_000)
        match = re.search(r"全部回复\s*[（(](\d+)[）)]", page_text)
        total_replies = comments.count(match.group(1)) if match else 0

    item: dict[str, Any] = {
        "platform_id": path_match.group(1) if path_match else "",
        "title": title,
        "text": content,
        "url": final_url,
        "author_handle": "",
        "author_id": "",
        "date": None,
        "engagement": {"replies": total_replies},
        "source": "tieba-browser-dom",
    }
    page_content.apply_content(
        item,
        content,
        content_type="post-body",
        content_source="tieba-browser-dom",
        content_url=final_url,
    )
    parsed_comments = _parse_dom_comments(page)
    comments.apply(
        item,
        parsed_comments,
        status="unavailable" if total_replies else "empty",
        source="tieba-browser-dom",
        evidence={
            "kind": "browser-dom",
            "root_selector": ".pb-comment-item",
            "nested_selector": ".pb-lzl-item",
        },
    )
    return item


def collect_hotlist_threads(
    hotlist_items: Iterable[Mapping[str, Any]],
    *,
    posts_per_topic: int = 1,
) -> list[dict[str, Any]]:
    """Open selected Tieba hot topics and read their leading threads."""
    if posts_per_topic <= 0:
        raise ValueError("posts_per_topic 必须大于 0")
    prepared: list[dict[str, Any]] = []
    for hotlist_item in hotlist_items:
        query = str(hotlist_item.get("title") or "").strip()
        try:
            prepared.append(_prepare_topic(hotlist_item))
        except Exception as exc:
            prepared.append({
                "query": query,
                "topic": None,
                "candidates": [],
                "prepare_error": f"{type(exc).__name__}: {exc}",
            })

    if not crawler_bridge.is_playwright_available():
        browser_error = "Playwright 浏览器不可用，无法打开贴吧帖子页"
        return [
            {
                "query": item["query"],
                "status": "error",
                "candidate_count": len(item["candidates"]),
                "post_count": 0,
                "topic": item["topic"],
                "posts": [],
                "error": item.get("prepare_error") or browser_error,
            }
            for item in prepared
        ]

    results: list[dict[str, Any]] = []
    with crawler_bridge._launch_browser_context("tieba") as (_, __, page):
        for item in prepared:
            error = item.get("prepare_error")
            posts: list[dict[str, Any]] = []
            if not error:
                for candidate in item["candidates"][:posts_per_topic]:
                    try:
                        posts.append(_read_post(page, candidate))
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {exc}"
            results.append({
                "query": item["query"],
                "status": "readable" if posts else ("error" if error else "empty"),
                "candidate_count": len(item["candidates"]),
                "post_count": len(posts),
                "topic": item["topic"],
                "posts": posts,
                "error": error,
            })
    return results
