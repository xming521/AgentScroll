"""微信公众号搜索模块 - 搜索微信公众号文章。

Author: Jesse (https://github.com/Jesseovo)

使用搜狗微信搜索获取微信公众号文章。
"""

import html as html_module
import http.cookiejar
import json
import re
import sys
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from . import comments, dates, live_artifacts, page_content, relevance, throttle
from .request_profiles import api_headers, navigation_headers


def search_wechat(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
) -> List[Dict[str, Any]]:
    """搜索微信公众号文章。

    Args:
        topic: 搜索关键词
        from_date: 起始日期
        to_date: 结束日期
        depth: 搜索深度

    Returns:
        微信公众号文章列表
    """
    limit_map = {"quick": 5, "default": 10, "deep": 20}
    limit = limit_map.get(depth, 10)

    items, detail_opener, detail_referer = _search_via_sogou(topic, limit)

    items = items[:limit]
    live_artifacts.save_stage("wechat", "01_search", topic, items)
    _enrich_wechat_articles(
        items,
        opener=detail_opener,
        referer=detail_referer,
    )
    live_artifacts.save_stage("wechat", "02_content", topic, items)
    _enrich_wechat_comments(items, opener=detail_opener)
    live_artifacts.save_stage("wechat", "03_comments", topic, items)
    items = page_content.retain_readable_details(
        items,
        allowed_sources={"wechat-article-page"},
    )

    scored = []
    for i, item in enumerate(items):
        title = item.get("title", "")
        snippet = item.get("snippet", "")
        combined = f"{title} {snippet}"
        rel = relevance.token_overlap_relevance(topic, combined)
        item["id"] = f"WX{i+1}"
        item["relevance"] = rel
        item["why_relevant"] = f"微信公众号：{title[:50]}"
        scored.append(item)

    scored.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    return scored[:limit]


def _search_via_sogou(
    topic: str,
    limit: int,
) -> Tuple[
    List[Dict[str, Any]],
    Optional[urllib.request.OpenerDirector],
    str,
]:
    """通过搜狗微信搜索。"""
    items: List[Dict[str, Any]] = []
    opener: Optional[urllib.request.OpenerDirector] = None
    url = "https://weixin.sogou.com/"
    try:
        encoded = urllib.parse.quote(topic)
        url = f"https://weixin.sogou.com/weixin?type=2&query={encoded}&ie=utf8"
        cookie_jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookie_jar)
        )
        req = urllib.request.Request(
            url,
            headers=navigation_headers("https://weixin.sogou.com/"),
        )
        with opener.open(req, timeout=15) as response:
            html = response.read().decode("utf-8")
        items = _parse_sogou_results(html, limit)
    except Exception as e:
        sys.stderr.write(f"[微信] 搜狗搜索失败: {e}\n")
    return items, opener, url


def _clean_html(value: str) -> str:
    value = re.sub(r"<!--.*?-->", "", value or "", flags=re.S)
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", html_module.unescape(value)).strip()


def _parse_sogou_results(page_html: str, limit: int) -> List[Dict[str, Any]]:
    """Parse only real article cards, excluding navigation/search links."""
    items: List[Dict[str, Any]] = []
    blocks = re.findall(
        r'<li[^>]+id="sogou_vr_11002601_box_\d+"[^>]*>(.*?)</li>',
        page_html,
        flags=re.S,
    )
    for block in blocks:
        title_match = re.search(
            r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>\s*</h3>',
            block,
            flags=re.S,
        )
        if not title_match:
            continue
        title = _clean_html(title_match.group(2))
        if not title:
            continue
        href = urllib.parse.urljoin(
            "https://weixin.sogou.com/",
            html_module.unescape(title_match.group(1)),
        )
        snippet_match = re.search(
            r'<p[^>]+class="[^"]*txt-info[^"]*"[^>]*>(.*?)</p>',
            block,
            flags=re.S,
        )
        account_match = re.search(
            r'<span[^>]+class="[^"]*all-time-y2[^"]*"[^>]*>(.*?)</span>',
            block,
            flags=re.S,
        )
        date_match = re.search(r"timeConvert\('?(\d+)'?\)", block)
        items.append({
            "title": title,
            "snippet": _clean_html(snippet_match.group(1)) if snippet_match else "",
            "url": href,
            "source_name": _clean_html(account_match.group(1)) if account_match else "",
            "wechat_id": "",
            "date": dates.timestamp_to_date(int(date_match.group(1))) if date_match else None,
            "engagement": {},
            "source": "sogou-wechat-search",
        })
        if len(items) >= limit:
            break
    return items


def _sogou_redirect_target(page_html: str) -> str:
    parts = re.findall(r"url\s*\+=\s*'([^']*)'", page_html)
    if parts:
        return (
            "".join(parts)
            .replace("&amp;", "&")
            .replace("\\u0026", "&")
            .replace("\\x26", "&")
            .replace("\\/", "/")
            .replace("@", "")
        )
    refresh = re.search(
        r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+url=([^"\'>\s]+)',
        page_html,
        flags=re.I,
    )
    return html_module.unescape(refresh.group(1)) if refresh else ""


def _fetch_wechat_article(
    url: str,
    *,
    opener: Optional[urllib.request.OpenerDirector] = None,
    referer: str = "https://weixin.sogou.com/",
) -> Optional[Dict[str, Any]]:
    try:
        final_url, page_html = page_content.fetch_page(
            url,
            referer=referer,
            opener=opener,
            max_bytes=4_000_000,
            throttle_key="wechat",
        )
        target = _sogou_redirect_target(page_html)
        if target:
            final_url, page_html = page_content.fetch_page(
                target,
                referer=url,
                opener=opener,
                max_bytes=4_000_000,
                throttle_key="wechat",
            )
        hostname = urllib.parse.urlsplit(final_url).hostname or ""
        if "mp.weixin.qq.com" not in hostname.lower():
            return None
        content = page_content.extract_main_text(
            page_html,
            preferred_ids=("js_content",),
            preferred_classes=("rich_media_content",),
        )
        if not content:
            return None
        return {
            "content": content,
            "url": final_url,
            "comment_context": _extract_wechat_comment_context(page_html),
        }
    except Exception:
        return None


def _enrich_wechat_articles(
    items: List[Dict[str, Any]],
    *,
    opener: Optional[urllib.request.OpenerDirector] = None,
    referer: str = "https://weixin.sogou.com/",
) -> None:
    for item in items:
        if item.get("content_type") == "article-body" and item.get("content"):
            continue
        url = str(item.get("url") or "")
        detail = _fetch_wechat_article(url, opener=opener, referer=referer) if url else None
        if detail:
            if detail["url"] != url:
                item["search_url"] = url
                item["url"] = detail["url"]
            page_content.apply_content(
                item,
                detail["content"],
                content_type="article-body",
                content_source="wechat-article-page",
                content_url=detail["url"],
            )
            context = detail.get("comment_context") or {}
            if context:
                item.update({
                    "wechat_comment_id": context.get("comment_id", ""),
                    "wechat_biz": context.get("biz", ""),
                    "wechat_appmsgid": context.get("appmsgid", ""),
                    "wechat_idx": context.get("idx", ""),
                })
            continue
        page_content.apply_summary_fallback(
            item,
            item.get("snippet"),
            item.get("title"),
        )


def _extract_wechat_comment_context(page_html: str) -> Dict[str, str]:
    """Extract public article identifiers needed by WeChat's comment endpoint."""
    patterns = {
        "biz": r'\bvar\s+biz\s*=\s*["\']([^"\']+)',
        "appmsgid": r'\bvar\s+mid\s*=\s*["\']([^"\']+)',
        "idx": r'\bvar\s+idx\s*=\s*["\']([^"\']+)',
        "comment_id": r'\bcomment_id\s*:\s*["\']([^"\']+)',
    }
    context: Dict[str, str] = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, page_html)
        if match:
            context[key] = match.group(1).strip()
    required = {"biz", "appmsgid", "idx", "comment_id"}
    return context if required.issubset(context) else {}


def _enrich_wechat_comments(
    items: List[Dict[str, Any]],
    *,
    opener: Optional[urllib.request.OpenerDirector],
) -> None:
    """Read selected article comments; retry page-signed requests in Chromium."""
    browser_pending: List[tuple[Dict[str, Any], Dict[str, str]]] = []
    for item in items:
        context = _wechat_context_from_item(item)
        if not context:
            # Articles without a declared comment_id have no public comment area.
            continue
        payload = _fetch_wechat_comment_payload(
            context,
            str(item.get("content_url") or item.get("url") or ""),
            opener,
        )
        parsed = _parse_wechat_comments(payload)
        status = _wechat_comment_status(payload)
        if parsed or status == "empty":
            comments.apply(
                item,
                parsed,
                status=status,
                source="wechat-appmsg-comment-api",
                evidence=_wechat_comment_evidence(context, "platform-api"),
            )
        else:
            browser_pending.append((item, context))

    if not browser_pending:
        return
    if not comments.browser_allowed():
        for item, context in browser_pending:
            comments.apply(
                item,
                [],
                status="blocked",
                source="wechat-appmsg-comment-api",
                evidence=_wechat_comment_evidence(context, "platform-api"),
            )
        return

    try:
        from . import crawler_bridge
        with crawler_bridge._launch_browser_context(
            "wechat",
            mobile=True,
        ) as (_, _, page):
            captured: Dict[str, Any] = {"comment_id": "", "payload": None}

            def _on_response(response) -> None:
                try:
                    if (
                        "/mp/appmsg_comment?action=getcomment" in response.url
                        and captured["comment_id"]
                        and f"comment_id={captured['comment_id']}" in response.url
                        and response.status == 200
                    ):
                        captured["payload"] = response.json()
                except Exception:
                    pass

            page.on("response", _on_response)
            for item, context in browser_pending:
                captured["comment_id"] = context["comment_id"]
                captured["payload"] = None
                throttle.wait_for_detail_request("wechat")
                try:
                    page.goto(
                        str(item.get("content_url") or item.get("url") or ""),
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    crawler_bridge._wait_for(
                        page,
                        lambda: isinstance(captured["payload"], dict),
                        timeout_ms=10000,
                    )
                    payload = captured["payload"]
                    comments.apply(
                        item,
                        _parse_wechat_comments(payload),
                        status=_wechat_comment_status(payload),
                        source="wechat-appmsg-comment-api",
                        evidence=_wechat_comment_evidence(context, "browser-response"),
                    )
                except Exception as exc:
                    sys.stderr.write(
                        f"[微信] 评论读取失败({context['comment_id']}): {exc}\n"
                    )
                    comments.apply(
                        item,
                        [],
                        status="unavailable",
                        source="wechat-appmsg-comment-api",
                        evidence=_wechat_comment_evidence(context, "browser-response"),
                    )
    except Exception as exc:
        sys.stderr.write(f"[微信] Chromium 评论读取失败: {exc}\n")
        for item, context in browser_pending:
            if "comments_status" not in item:
                comments.apply(
                    item,
                    [],
                    status="unavailable",
                    source="wechat-appmsg-comment-api",
                    evidence=_wechat_comment_evidence(context, "browser-response"),
                )


def _wechat_context_from_item(item: Dict[str, Any]) -> Dict[str, str]:
    context = {
        "comment_id": str(item.get("wechat_comment_id") or ""),
        "biz": str(item.get("wechat_biz") or ""),
        "appmsgid": str(item.get("wechat_appmsgid") or ""),
        "idx": str(item.get("wechat_idx") or ""),
    }
    return context if all(context.values()) else {}


def _fetch_wechat_comment_payload(
    context: Dict[str, str],
    referer: str,
    opener: Optional[urllib.request.OpenerDirector],
) -> Optional[Dict[str, Any]]:
    query = urllib.parse.urlencode({
        "action": "getcomment",
        "scene": 0,
        "appmsgid": context["appmsgid"],
        "idx": context["idx"],
        "__biz": context["biz"],
        "comment_id": context["comment_id"],
        "offset": 0,
        "limit": comments.limit(),
    })
    request = urllib.request.Request(
        f"https://mp.weixin.qq.com/mp/appmsg_comment?{query}",
        headers=api_headers(referer, mobile=True),
    )
    try:
        throttle.wait_for_detail_request("wechat")
        request_opener = opener or urllib.request.build_opener()
        with request_opener.open(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _wechat_comment_status(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "unavailable"
    base_response = payload.get("base_resp") or {}
    if base_response.get("ret") not in (None, 0, "0"):
        return "blocked"
    raw_comments = payload.get("elected_comment")
    if raw_comments is None and isinstance(payload.get("data"), dict):
        raw_comments = payload["data"].get("elected_comment")
    return "empty" if isinstance(raw_comments, list) else "unavailable"


def _parse_wechat_comments(payload: Any) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    raw_comments = payload.get("elected_comment")
    if raw_comments is None and isinstance(payload.get("data"), dict):
        raw_comments = payload["data"].get("elected_comment")
    parsed: List[Dict[str, Any]] = []
    for raw in raw_comments if isinstance(raw_comments, list) else []:
        if not isinstance(raw, dict):
            continue
        parent_id = raw.get("content_id") or raw.get("id")
        value = comments.entry(
            parent_id,
            raw.get("content"),
            created_at=comments.timestamp(raw.get("create_time")),
            likes=raw.get("like_num"),
            reply_count=raw.get("reply_count"),
        )
        if value:
            parsed.append(value)
        reply_container = raw.get("reply") if isinstance(raw.get("reply"), dict) else {}
        replies = reply_container.get("reply_list") or raw.get("reply_list") or []
        for reply in replies:
            if not isinstance(reply, dict):
                continue
            value = comments.entry(
                reply.get("content_id") or reply.get("id"),
                reply.get("content"),
                created_at=comments.timestamp(reply.get("create_time")),
                likes=reply.get("like_num"),
                parent_comment_id=parent_id,
            )
            if value:
                parsed.append(value)
            if len(parsed) >= comments.limit():
                return parsed
        if len(parsed) >= comments.limit():
            break
    return parsed


def _wechat_comment_evidence(
    context: Dict[str, str],
    kind: str,
) -> Dict[str, Any]:
    return {
        "kind": kind,
        "endpoint": "/mp/appmsg_comment?action=getcomment",
        "comment_id": context["comment_id"],
        "appmsgid": context["appmsgid"],
        "idx": context["idx"],
    }
