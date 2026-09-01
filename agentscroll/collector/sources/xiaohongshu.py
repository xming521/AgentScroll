"""小红书搜索模块 - 搜索小红书笔记。

Author: Jesse (https://github.com/Jesseovo)

支持两种数据获取方式（按优先级自动切换）：
1. 本地 Playwright 浏览器搜索（无需 API Key）
2. 站外搜索发现公开笔记 URL
"""

import json
import re
import sys
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from . import (
    browser,
    comments,
    dates,
    live_artifacts,
    page_content,
    relevance,
    throttle,
    web_search,
)
from .request_profiles import api_headers, navigation_headers

def search_xiaohongshu(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    *,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """搜索小红书笔记。

    Args:
        topic: 搜索关键词
        from_date: 起始日期 YYYY-MM-DD
        to_date: 结束日期 YYYY-MM-DD
        depth: 搜索深度 quick/default/deep
        limit: 不超过当前深度上限的候选条目数
    Returns:
        小红书笔记列表
    """
    limit_map = {"quick": 5, "default": 10, "deep": 20}
    depth_limit = limit_map.get(depth, 10)
    if limit is None:
        limit = depth_limit
    elif limit <= 0:
        raise ValueError("limit 必须大于 0")
    else:
        limit = min(limit, depth_limit)

    items: List[Dict[str, Any]] = []

    if not items and depth != "quick":
        try:
            if browser.is_playwright_available():
                sys.stderr.write("[小红书] 尝试 Playwright 浏览器搜索...\n")
                items = crawl_xiaohongshu(topic, limit)
                if items:
                    sys.stderr.write(f"[小红书] 爬虫模式获取 {len(items)} 条结果\n")
        except Exception as e:
            sys.stderr.write(f"[小红书] 爬虫模式失败: {e}\n")

    if not items:
        items = _search_via_site_search(topic, limit)

    items = items[:limit]
    live_artifacts.save_stage("xiaohongshu", "01_search", topic, items)
    _enrich_note_contents(items, topic)
    live_artifacts.save_stage("xiaohongshu", "02_content", topic, items)
    _enrich_note_comments(items)
    live_artifacts.save_stage("xiaohongshu", "03_comments", topic, items)
    items = page_content.retain_readable_details(
        items,
        allowed_sources={"xiaohongshu-initial-state", "xiaohongshu-detail-dom"},
    )

    scored = []
    for i, item in enumerate(items):
        title = item.get("title", "")
        desc = item.get("desc", "")
        combined = f"{title} {desc}"
        rel = relevance.token_overlap_relevance(topic, combined)
        item["id"] = f"XHS{i+1}"
        item["relevance"] = rel
        source = item.get("source", "xiaohongshu")
        item["why_relevant"] = f"小红书来源({source}): {title[:50]}"
        scored.append(item)

    scored.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    return scored[:limit]


def crawl_xiaohongshu(topic: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Use Playwright to collect Xiaohongshu search results."""
    if not browser.is_playwright_available():
        return []

    items: List[Dict[str, Any]] = []
    try:
        with browser.browser_context("xiaohongshu") as (_, _, page):
            captured: Dict[str, Any] = {"items": [], "endpoint": ""}

            def _on_response(response) -> None:
                try:
                    url = response.url
                    lowered = url.lower()
                    if response.status != 200 or "xiaohongshu.com" not in lowered:
                        return
                    if "/api/" not in lowered or "search" not in lowered:
                        return
                    note_items = _extract_xhs_note_items(response.json())
                    if note_items and not captured["items"]:
                        captured["items"] = note_items
                        captured["endpoint"] = url.split("?", 1)[0]
                except Exception:
                    pass

            page.on("response", _on_response)
            search_url = (
                "https://www.xiaohongshu.com/search_result?"
                f"keyword={urllib.parse.quote(topic, safe='')}"
                "&source=web_search_result_notes"
            )
            try:
                page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                sys.stderr.write(f"[爬虫-小红书] 页面加载失败: {exc}\n")

            if not captured["items"]:
                _submit_xhs_search(page, topic)

            for _ in range(8):
                if captured["items"]:
                    break
                try:
                    page.mouse.wheel(0, 2000)
                except Exception:
                    pass
                page.wait_for_timeout(1000)

            for raw in captured["items"][:limit]:
                parsed = _parse_crawler_xhs_note(raw)
                if parsed:
                    items.append(parsed)

            if not captured["items"]:
                sys.stderr.write(
                    "[爬虫-小红书] 未捕获包含笔记卡片的搜索响应；"
                    "可能需要重新登录、通过验证码，或平台接口已调整；"
                    "将继续尝试 DOM/站内搜索兜底。\n"
                )

            if not items:
                note_elements = page.query_selector_all(
                    "section.note-item, div[class*='note-item'], "
                    "a[class*='cover'], a[href*='/explore/']"
                )
                for element in note_elements[:limit]:
                    try:
                        title_element = element.query_selector(
                            "span[class*='title'], div[class*='title']"
                        )
                        title = title_element.inner_text() if title_element else ""
                        link = element.get_attribute("href") or ""
                        if link and not link.startswith("http"):
                            link = f"https://www.xiaohongshu.com{link}"
                        if not title or "/explore/" not in link:
                            continue

                        author_element = element.query_selector(
                            "span[class*='name'], div[class*='author']"
                        )
                        likes_element = element.query_selector(
                            "span[class*='like'], span[class*='count']"
                        )
                        items.append(
                            {
                                "title": title,
                                "desc": "",
                                "url": link,
                                "author_name": (
                                    author_element.inner_text()
                                    if author_element
                                    else ""
                                ),
                                "author_id": "",
                                "date": None,
                                "engagement": {
                                    "likes": comments.count(
                                        likes_element.inner_text()
                                        if likes_element
                                        else "0"
                                    ),
                                    "collects": 0,
                                    "comments": 0,
                                    "shares": 0,
                                },
                                "hashtags": [],
                                "images": [],
                                "source": "crawler-dom",
                            }
                        )
                    except Exception:
                        continue
            if not items:
                items = _crawl_xhs_public_pages(page, topic, limit)
            if not items:
                sys.stderr.write(
                    "[爬虫-小红书] Playwright 未解析到结果；"
                    "这通常是登录态失效、反爬验证或页面结构变更导致。\n"
                )
    except Exception as exc:
        sys.stderr.write(f"[爬虫-小红书] 浏览器爬取失败: {exc}\n")
    return items


def _submit_xhs_search(page: Any, topic: str) -> bool:
    """Submit the search box when the page does not auto-run the query."""
    selectors = (
        "input[placeholder*='搜索']",
        "input[placeholder*='搜']",
        "input[type='search']",
    )
    for selector in selectors:
        try:
            for element in page.query_selector_all(selector):
                if not element.is_visible():
                    continue
                element.fill(topic)
                element.press("Enter")
                return True
        except Exception:
            continue
    return False


def _extract_xhs_note_items(payload: Any) -> List[Dict[str, Any]]:
    """Find note-card records in changing search-response envelopes."""

    def is_note_record(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        card = value.get("note_card")
        if isinstance(card, dict):
            return bool(
                (card.get("note_id") or value.get("id"))
                and (
                    card.get("title")
                    or card.get("display_title")
                    or card.get("desc")
                )
            )
        return bool(
            (value.get("note_id") or value.get("id"))
            and (
                value.get("title")
                or value.get("display_title")
                or value.get("desc")
            )
        )

    def walk(value: Any) -> List[Dict[str, Any]]:
        if isinstance(value, list):
            records = [item for item in value if is_note_record(item)]
            if records:
                return records
            for item in value:
                records = walk(item)
                if records:
                    return records
        elif isinstance(value, dict):
            for child in value.values():
                records = walk(child)
                if records:
                    return records
        return []

    return walk(payload)


def _parse_crawler_xhs_note(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize one note-card record captured by Playwright."""
    note_card = raw.get("note_card") if isinstance(raw.get("note_card"), dict) else raw
    note_id = raw.get("id") or note_card.get("note_id") or raw.get("note_id", "")
    if not note_id:
        return None
    xsec_token = raw.get("xsec_token") or note_card.get("xsec_token") or ""
    note_url = f"https://www.xiaohongshu.com/explore/{note_id}"
    if xsec_token:
        note_url = f"{note_url}?{urllib.parse.urlencode({'xsec_token': xsec_token, 'xsec_source': 'pc_search'})}"
    user = note_card.get("user") or raw.get("user") or {}
    interact = note_card.get("interact_info") or raw.get("interact_info") or {}
    return {
        "title": (
            note_card.get("display_title")
            or note_card.get("title")
            or raw.get("title", "")
        ),
        "desc": (
            note_card.get("desc")
            or note_card.get("description")
            or raw.get("desc", "")
        ),
        "url": note_url,
        "author_name": (
            user.get("nickname") or user.get("nick_name") or user.get("name", "")
        ),
        "author_id": user.get("user_id") or user.get("userid") or user.get("id", ""),
        "date": None,
        "engagement": {
            "likes": comments.count(
                interact.get("liked_count", raw.get("liked_count", "0"))
            ),
            "collects": comments.count(
                interact.get("collected_count", raw.get("collected_count", "0"))
            ),
            "comments": comments.count(
                interact.get("comment_count", raw.get("comment_count", "0"))
            ),
            "shares": comments.count(
                interact.get("share_count", raw.get("share_count", "0"))
            ),
        },
        "hashtags": [],
        "images": [
            image.get("url_default") or image.get("url", "")
            for image in (
                note_card.get("image_list") or raw.get("image_list") or []
            )
            if isinstance(image, dict)
        ],
        "source": "crawler-xhr",
    }


def _crawl_xhs_public_pages(
    page: Any,
    topic: str,
    limit: int,
) -> List[Dict[str, Any]]:
    """Read indexed public note pages through the active browser context."""
    items: List[Dict[str, Any]] = []
    for result in _discover_site_results(topic, limit):
        try:
            page.goto(result["url"], wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(500)
            if urllib.parse.urlsplit(page.url).path.rstrip("/") == "/404":
                continue
            title = (
                page.locator("meta[property='og:title']")
                .first.get_attribute("content")
                or page.title()
                or ""
            )
            desc = (
                page.locator("meta[property='og:description']")
                .first.get_attribute("content")
                or page.locator("meta[name='description']")
                .first.get_attribute("content")
                or ""
            )
            title = re.sub(r"\s*-\s*小红书\s*$", "", title).strip()
            desc = _clean_text(desc)
            if not title or "访问的页面不见了" in title:
                continue
            if relevance.token_overlap_relevance(topic, f"{title} {desc}") <= 0:
                continue
            items.append(
                {
                    "title": title,
                    "desc": desc,
                    "url": result["url"],
                    "author_name": "",
                    "author_id": "",
                    "date": None,
                    "engagement": {
                        "likes": 0,
                        "collects": 0,
                        "comments": 0,
                        "shares": 0,
                    },
                    "hashtags": re.findall(r"#([^#\s]+)#?", f"{title} {desc}"),
                    "images": [],
                    "source": "crawler-public-page",
                }
            )
            if len(items) >= limit:
                break
        except Exception:
            continue
    if items:
        sys.stderr.write(
            f"[爬虫-小红书] 搜索页要求登录，已从公开笔记详情页读取 {len(items)} 条内容。\n"
        )
    return items


def _fetch_html(url: str, timeout: int = 8) -> str:
    headers = navigation_headers("https://cn.bing.com/")
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _search_via_site_search(topic: str, limit: int) -> List[Dict[str, Any]]:
    """Discover indexed note URLs; detail hydration is a separate stage."""
    results = _discover_site_results(topic, limit)
    items: List[Dict[str, Any]] = []
    for result in results:
        title = result["title"]
        snippet = result["snippet"]
        items.append({
            "title": title,
            "desc": snippet,
            "url": result["url"],
            "author_name": "",
            "author_id": "",
            "date": None,
            "engagement": {"likes": 0, "collects": 0, "comments": 0, "shares": 0},
            "hashtags": re.findall(r"#([^#\s]+)#?", f"{title} {snippet}"),
            "images": [],
            "source": "web-search-discovery",
        })
    if items:
        sys.stderr.write(
            f"[小红书] 搜索接口无结果，发现 {len(items)} 条公开笔记链接，准备逐条读取详情。\n"
        )
    return items


def _discover_site_results(topic: str, limit: int) -> List[Dict[str, str]]:
    """Discover candidate public note URLs without treating snippets as content."""
    query = f"site:xiaohongshu.com/explore {topic}"
    allowed = (
        "xiaohongshu.com/explore/",
        "xiaohongshu.com/discovery/item/",
    )
    providers = (
        lambda: web_search.search_bing(
            query, allowed_url_fragments=allowed, limit=limit, fetcher=_fetch_html
        ),
        lambda: web_search.search_yahoo(
            query, allowed_url_fragments=allowed, limit=limit
        ),
        lambda: web_search.search_duckduckgo(
            query, allowed_url_fragments=allowed, limit=limit
        ),
    )
    for provider in providers:
        try:
            results = provider()
            if results:
                return results
        except Exception:
            continue
    return []


def _fetch_public_note(url: str, topic: str = "") -> Optional[Dict[str, Any]]:
    """Read post text from Xiaohongshu's embedded state or dedicated detail DOM."""
    try:
        throttle.wait_for_detail_request("xiaohongshu")
        headers = navigation_headers("https://www.xiaohongshu.com/")
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as response:
            page_html = response.read().decode("utf-8", errors="replace")
            final_url = response.geturl()

        if urllib.parse.urlsplit(final_url).path.rstrip("/") == "/404":
            return None
        metadata = web_search.parse_page_metadata(page_html)
        note_id_match = re.search(r"/(?:explore|discovery/item)/([0-9A-Za-z]+)", url)
        note_id = note_id_match.group(1) if note_id_match else ""
        extracted = _extract_xiaohongshu_content(page_html, note_id)
        if not extracted:
            return None
        title = extracted.get("title") or re.sub(
            r"\s*-\s*小红书\s*$", "", metadata["title"]
        ).strip()
        desc = str(extracted["content"]).strip()
        if not title or "访问的页面不见了" in title:
            return None
        if topic and relevance.token_overlap_relevance(topic, f"{title} {desc}") <= 0:
            return None

        # The canonical URL currently redirects some otherwise public notes to
        # /404.  Keep the discovered access URL (including xsec_token) because
        # the detail page uses that token to issue its signed comment request.
        item = {
            "title": title,
            "desc": desc,
            "url": url,
            "author_name": "",
            "author_id": "",
            "date": extracted.get("date"),
            "engagement": extracted.get("engagement") or {
                "likes": 0,
                "collects": 0,
                "comments": 0,
                "shares": 0,
            },
            "hashtags": re.findall(r"#([^#\s]+)#?", f"{title} {desc}"),
            "images": [],
            "note_id": note_id,
            "source": "public-note-page",
            "extraction_evidence": extracted["evidence"],
        }
        page_content.apply_content(
            item,
            desc,
            content_type="post-description",
            content_source=str(extracted["source"]),
            content_url=url,
        )
        return item
    except Exception:
        return None


def _extract_xiaohongshu_content(
    page_html: str,
    note_id: str,
) -> Optional[Dict[str, Any]]:
    state_match = re.search(
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>",
        page_html,
        flags=re.S,
    )
    if state_match:
        normalized = re.sub(
            r"([:\[,]\s*)undefined(?=\s*[,}\]])",
            r"\1null",
            state_match.group(1),
        )
        try:
            state = json.loads(normalized)
            note_store = state.get("note", {})
            note_map = note_store.get("noteDetailMap", {})
            selected_id = note_id or note_store.get("currentNoteId", "")
            entry = note_map.get(selected_id, {})
            note = entry.get("note", {}) if isinstance(entry, dict) else {}
            desc = str(note.get("desc") or "").strip()
            if desc and (not note_id or str(note.get("noteId") or note_id) == note_id):
                published_at = None
                if note.get("time") not in (None, ""):
                    try:
                        published_at = dates.timestamp_to_date(
                            float(note["time"]) / 1000
                        )
                    except (TypeError, ValueError, OverflowError, OSError):
                        pass
                interact = (
                    note.get("interactInfo")
                    if isinstance(note.get("interactInfo"), dict)
                    else {}
                )
                return {
                    "title": str(note.get("title") or "").strip(),
                    "content": desc,
                    "date": published_at,
                    "engagement": {
                        "likes": _safe_int(interact.get("likedCount")),
                        "collects": _safe_int(interact.get("collectedCount")),
                        "comments": _safe_int(interact.get("commentCount")),
                        "shares": _safe_int(interact.get("shareCount")),
                    },
                    "source": "xiaohongshu-initial-state",
                    "evidence": {
                        "kind": "embedded-json",
                        "object": "window.__INITIAL_STATE__",
                        "path": f"note.noteDetailMap[{selected_id}].note.desc",
                    },
                }
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

    desc = page_content.extract_selector_text(
        page_html,
        preferred_ids=("detail-desc",),
        preferred_classes=("note-text",),
        min_chars=1,
    )
    if not desc:
        return None
    return {
        "title": "",
        "content": desc,
        "source": "xiaohongshu-detail-dom",
        "evidence": {
            "kind": "dom",
            "selector": "#detail-desc .note-text",
        },
    }


def _enrich_note_contents(items: List[Dict[str, Any]], topic: str = "") -> None:
    """Read each note detail page when only a search card was returned."""
    for item in items:
        if item.get("content_source") in {
            "xiaohongshu-initial-state",
            "xiaohongshu-detail-dom",
        } and item.get("content"):
            continue
        url = str(item.get("url") or "")
        # Site-search providers occasionally return unrelated, previously
        # indexed share URLs. Validate the hydrated title/body against the
        # original topic before accepting it as a real Xiaohongshu result.
        detail = _fetch_public_note(url, topic) if url else None
        if detail and detail.get("content"):
            item["title"] = detail.get("title") or item.get("title", "")
            detail_desc = str(detail.get("desc") or "")
            if len(detail_desc) > len(str(item.get("desc") or "")):
                item["desc"] = detail_desc
            # Do not replace a working tokenized discovery URL with the bare
            # canonical URL: Xiaohongshu may serve the latter as /404 and then
            # never load the comment API.
            item["url"] = url or detail.get("url", "")
            item["source"] = "public-note-page"
            item["extraction_evidence"] = detail.get("extraction_evidence", {})
            if detail.get("note_id"):
                item["note_id"] = detail["note_id"]
            if detail.get("date"):
                item["date"] = detail["date"]
            if detail.get("engagement"):
                item["engagement"] = detail["engagement"]
            page_content.apply_content(
                item,
                detail["content"],
                content_type="post-description",
                content_source=str(detail.get("content_source") or ""),
                content_url=url or detail.get("content_url"),
            )
            continue
        page_content.apply_summary_fallback(
            item,
            item.get("desc"),
            item.get("title"),
        )


def _enrich_note_comments(items: List[Dict[str, Any]]) -> None:
    """Capture Xiaohongshu's page-signed comment response in one browser context."""
    pending: List[tuple[Dict[str, Any], str, str]] = []
    for item in items:
        if item.get("content_source") not in {
            "xiaohongshu-initial-state",
            "xiaohongshu-detail-dom",
        } or not item.get("content"):
            continue
        note_id = str(item.get("note_id") or "")
        if not note_id:
            match = re.search(
                r"/(?:explore|discovery/item)/([0-9A-Za-z]+)",
                str(item.get("url") or ""),
            )
            note_id = match.group(1) if match else ""
        if note_id:
            item["note_id"] = note_id
            pending.append((item, note_id, str(item.get("url") or "")))
        else:
            comments.apply(
                item,
                [],
                status="unavailable",
                source="xiaohongshu-comment-api",
                evidence={"reason": "missing-note-id"},
            )

    if not pending:
        return
    if not comments.browser_allowed():
        for item, note_id, _ in pending:
            comments.apply(
                item,
                [],
                status="unavailable",
                source="xiaohongshu-comment-api",
                evidence={"reason": "browser-disabled", "note_id": note_id},
            )
        return

    try:
        with browser.browser_context(
            "xiaohongshu",
            mobile=False,
        ) as (_, _, page):
            captured: Dict[str, Any] = {"note_id": "", "payload": None}

            def _on_response(response) -> None:
                try:
                    if (
                        "/api/sns/web/v2/comment/page" in response.url
                        and captured["note_id"]
                        and f"note_id={captured['note_id']}" in response.url
                        and response.status == 200
                    ):
                        captured["payload"] = response.json()
                except Exception:
                    pass

            page.on("response", _on_response)
            for item, note_id, access_url in pending:
                captured["note_id"] = note_id
                captured["payload"] = None
                throttle.wait_for_detail_request("xiaohongshu")
                try:
                    page.goto(
                        access_url
                        or f"https://www.xiaohongshu.com/explore/{note_id}",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    browser.wait_for(
                        page,
                        lambda: isinstance(captured["payload"], dict),
                        timeout_ms=10000,
                    )
                    payload = captured["payload"]
                    raw_comments = (
                        (payload.get("data") or {}).get("comments")
                        if isinstance(payload, dict)
                        else None
                    )
                    parsed = _parse_xiaohongshu_comments(raw_comments)
                    comments.apply(
                        item,
                        parsed,
                        status="empty" if isinstance(raw_comments, list) else "blocked",
                        source="xiaohongshu-comment-api",
                        evidence={
                            "kind": "browser-response",
                            "endpoint": "/api/sns/web/v2/comment/page",
                            "note_id": note_id,
                        },
                    )
                except Exception as exc:
                    sys.stderr.write(f"[小红书] 评论读取失败({note_id}): {exc}\n")
                    comments.apply(
                        item,
                        [],
                        status="unavailable",
                        source="xiaohongshu-comment-api",
                        evidence={"kind": "browser-response", "note_id": note_id},
                    )
    except Exception as exc:
        sys.stderr.write(f"[小红书] Chromium 评论读取失败: {exc}\n")
        for item, note_id, _ in pending:
            if "comments_status" not in item:
                comments.apply(
                    item,
                    [],
                    status="unavailable",
                    source="xiaohongshu-comment-api",
                    evidence={"kind": "browser-response", "note_id": note_id},
                )


def _parse_xiaohongshu_comments(raw_comments: Any) -> List[Dict[str, Any]]:
    parsed: List[Dict[str, Any]] = []
    for raw in raw_comments if isinstance(raw_comments, list) else []:
        if not isinstance(raw, dict):
            continue
        parent_id = raw.get("id")
        value = comments.entry(
            parent_id,
            raw.get("content"),
            created_at=comments.timestamp(raw.get("create_time"), milliseconds=True),
            likes=raw.get("like_count"),
            reply_count=raw.get("sub_comment_count"),
        )
        if value:
            parsed.append(value)
        for reply in raw.get("sub_comments") or []:
            if not isinstance(reply, dict):
                continue
            value = comments.entry(
                reply.get("id"),
                reply.get("content"),
                created_at=comments.timestamp(
                    reply.get("create_time"), milliseconds=True
                ),
                likes=reply.get("like_count"),
                parent_comment_id=parent_id,
            )
            if value:
                parsed.append(value)
            if len(parsed) >= comments.limit():
                return parsed
        if len(parsed) >= comments.limit():
            break
    return parsed


def _parse_note(note: dict) -> Dict[str, Any]:
    """解析小红书笔记数据。"""
    note_id = note.get("note_id") or note.get("id", "")
    title = note.get("title") or note.get("display_title", "")
    desc = note.get("desc") or note.get("description", "")
    user = note.get("user", {}) if isinstance(note.get("user"), dict) else {}
    liked_count = note.get("liked_count") or note.get("likes", 0)
    collected_count = note.get("collected_count") or note.get("collects", 0)
    comment_count = note.get("comment_count") or note.get("comments", 0)
    share_count = note.get("share_count") or note.get("shares", 0)

    hashtags = re.findall(r"#([^#\s]+)#?", f"{title} {desc}")

    date_str = note.get("time") or note.get("created_time") or note.get("date")
    if date_str and len(str(date_str)) == 13:
        date_str = dates.timestamp_to_date(int(date_str) / 1000)
    elif date_str and len(str(date_str)) == 10 and str(date_str).isdigit():
        date_str = dates.timestamp_to_date(int(date_str))

    return {
        "title": title,
        "desc": desc,
        "url": f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else "",
        "note_id": str(note_id or ""),
        "author_name": user.get("nickname") or user.get("name", ""),
        "author_id": user.get("user_id") or user.get("id", ""),
        "date": date_str,
        "engagement": {
            "likes": _safe_int(liked_count),
            "collects": _safe_int(collected_count),
            "comments": _safe_int(comment_count),
            "shares": _safe_int(share_count),
        },
        "hashtags": hashtags,
        "images": note.get("images_list") or note.get("image_list", []),
    }


def _safe_int(val) -> int:
    """安全转换为整数。"""
    if val is None:
        return 0
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0
