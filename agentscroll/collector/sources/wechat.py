"""微信公众号搜索模块 - 搜索微信公众号文章。

Author: Jesse (https://github.com/Jesseovo)

使用搜狗微信搜索获取微信公众号文章。
"""

import html as html_module
import http.cookiejar
import re
import sys
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from . import dates, live_artifacts, page_content, relevance
from .request_profiles import navigation_headers


def search_wechat(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    *,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """搜索微信公众号文章。

    Args:
        topic: 搜索关键词
        from_date: 起始日期
        to_date: 结束日期
        depth: 搜索深度
        limit: 不超过当前深度上限的候选条目数

    Returns:
        微信公众号文章列表
    """
    limit_map = {"quick": 5, "default": 10, "deep": 20}
    depth_limit = limit_map.get(depth, 10)
    if limit is None:
        limit = depth_limit
    elif limit <= 0:
        raise ValueError("limit 必须大于 0")
    else:
        limit = min(limit, depth_limit)

    items, detail_opener, detail_referer = _search_via_sogou(topic, limit)

    items = items[:limit]
    live_artifacts.save_stage("wechat", "01_search", topic, items)
    _enrich_wechat_articles(
        items,
        opener=detail_opener,
        referer=detail_referer,
    )
    live_artifacts.save_stage("wechat", "02_content", topic, items)
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
            continue
        page_content.apply_summary_fallback(
            item,
            item.get("snippet"),
            item.get("title"),
        )
