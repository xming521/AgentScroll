import json
import os
import re
import sys
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from . import relevance
from .request_profiles import desktop_chrome_ua, mobile_chrome_ua

COOKIE_DIR = Path.home() / ".config" / "agentscroll" / "browser_cookies"
_playwright_available: Optional[bool] = None
_BROWSER_PATH_ENV = "AGENTSCROLL_BROWSER_PATH"
_BROWSER_CHANNEL_ENV = "AGENTSCROLL_BROWSER_CHANNEL"
_DISABLE_BROWSER_ENV = "AGENTSCROLL_DISABLE_BROWSER"


def is_playwright_available() -> bool:
    """检查 Playwright 是否已安装并可用。"""
    global _playwright_available
    if _playwright_available is not None:
        return _playwright_available
    if os.environ.get(_DISABLE_BROWSER_ENV, "").lower() in ("1", "true", "yes", "on"):
        _playwright_available = False
        return _playwright_available
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401

        _playwright_available = True
    except (ImportError, OSError):
        _playwright_available = False
    return _playwright_available


def _browser_launch_kwargs() -> Dict[str, Any]:
    """Return optional Playwright launch overrides for old machines."""
    kwargs: Dict[str, Any] = {}
    browser_path = os.environ.get(_BROWSER_PATH_ENV, "").strip()
    browser_channel = os.environ.get(_BROWSER_CHANNEL_ENV, "").strip()
    if browser_path:
        kwargs["executable_path"] = os.path.expanduser(browser_path)
    elif browser_channel:
        kwargs["channel"] = browser_channel
    kwargs["args"] = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
    ]
    return kwargs


def _browser_status() -> Dict[str, Any]:
    """Describe the selected browser without starting it."""
    browser_path = os.environ.get(_BROWSER_PATH_ENV, "").strip()
    browser_channel = os.environ.get(_BROWSER_CHANNEL_ENV, "").strip()
    disabled = os.environ.get(_DISABLE_BROWSER_ENV, "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    expanded_path = os.path.expanduser(browser_path) if browser_path else ""
    return {
        "mode": "disabled"
        if disabled
        else (
            "external-path"
            if browser_path
            else ("channel" if browser_channel else "managed")
        ),
        "path": expanded_path or None,
        "path_exists": bool(expanded_path and Path(expanded_path).is_file()),
        "channel": browser_channel or None,
        "disable_env": _DISABLE_BROWSER_ENV,
    }


def _ensure_cookie_dir():
    COOKIE_DIR.mkdir(parents=True, exist_ok=True)


def _get_cookie_path(platform: str) -> Path:
    _ensure_cookie_dir()
    return COOKIE_DIR / f"{platform}_cookies.json"


def save_cookies(platform: str, cookies: list):
    path = _get_cookie_path(platform)
    path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")


def load_cookies(platform: str) -> Optional[list]:
    path = _get_cookie_path(platform)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _clean_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", str(text))
    return re.sub(r"\s+", " ", text).strip()


@contextmanager
def _launch_browser_context(platform: str, mobile: bool = False, headless: bool = True):
    """统一构造 Playwright 浏览器上下文，自动加载并回写 cookies。

    Yields:
        (browser, context, page) 三元组
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, **_browser_launch_kwargs())
        try:
            device_name = "Pixel 7" if mobile else "Desktop Chrome HiDPI"
            context_options = dict(p.devices.get(device_name, {}))
            has_device_descriptor = bool(context_options)
            context_options.pop("default_browser_type", None)
            version = browser.version or "151.0.7922.34"
            context_options["user_agent"] = (
                mobile_chrome_ua(version) if mobile else desktop_chrome_ua(version)
            )
            if not has_device_descriptor:
                context_options.update(
                    {
                        "viewport": (
                            {"width": 412, "height": 839}
                            if mobile
                            else {"width": 1280, "height": 720}
                        ),
                        "device_scale_factor": 2.625 if mobile else 2,
                        "is_mobile": mobile,
                        "has_touch": mobile,
                    }
                )
            context_options.update(
                {
                    "locale": "zh-CN",
                    "timezone_id": "Asia/Shanghai",
                    "extra_http_headers": {
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
                    },
                }
            )
            context = browser.new_context(**context_options)
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            cookies = load_cookies(platform)
            if cookies:
                try:
                    context.add_cookies(cookies)
                except Exception as e:
                    sys.stderr.write(f"[爬虫-{platform}] 加载 Cookie 失败: {e}\n")

            page = context.new_page()
            try:
                yield browser, context, page
            finally:
                try:
                    save_cookies(platform, context.cookies())
                except Exception:
                    pass
        finally:
            try:
                browser.close()
            except Exception:
                pass


def _wait_for(page, predicate, timeout_ms: int = 8000, interval_ms: int = 500) -> bool:
    """轮询等待条件满足，返回是否在超时前命中。"""
    elapsed = 0
    while elapsed < timeout_ms:
        try:
            if predicate():
                return True
        except Exception:
            pass
        page.wait_for_timeout(interval_ms)
        elapsed += interval_ms
    return False


def crawl_xiaohongshu(topic: str, limit: int = 20) -> List[Dict[str, Any]]:
    """通过浏览器自动化爬取小红书搜索结果。

    v3.2：不再绑定单一 endpoint。新版小红书曾将
    `/api/sns/web/v1/search/notes` 替换为只返回 `sug_items` 的
    `/api/sns/web/v1/search/recommend`；这里按响应 payload 中的笔记卡片识别
    搜索结果，避免把联想词误当成笔记。失败时回退到 DOM/站内搜索。
    """
    if not is_playwright_available():
        return []

    items: List[Dict[str, Any]] = []
    try:
        with _launch_browser_context("xiaohongshu") as (browser, context, page):
            captured: Dict[str, Any] = {"items": [], "endpoint": ""}

            def _on_response(resp):
                try:
                    url = resp.url
                    lowered = url.lower()
                    if resp.status != 200 or "xiaohongshu.com" not in lowered:
                        return
                    if "/api/" not in lowered or "search" not in lowered:
                        return
                    payload = resp.json()
                    note_items = _extract_xhs_note_items(payload)
                    if note_items and not captured["items"]:
                        captured["items"] = note_items
                        captured["endpoint"] = url.split("?", 1)[0]
                except Exception:
                    pass

            page.on("response", _on_response)

            search_url = (
                f"https://www.xiaohongshu.com/search_result?"
                f"keyword={urllib.parse.quote(topic, safe='')}&source=web_search_result_notes"
            )
            try:
                page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                sys.stderr.write(f"[爬虫-小红书] 页面加载失败: {e}\n")

            # Some current page builds only request note results after the search
            # box is submitted; the recommendation request alone contains no notes.
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
                    "[爬虫-小红书] 未捕获包含笔记卡片的搜索响应；search/recommend 仅返回联想词，"
                    "可能需要重新登录、通过验证码，或平台接口已调整；"
                    "将继续尝试 DOM/站内搜索兜底。\n"
                )

            if not items:
                note_elements = page.query_selector_all(
                    "section.note-item, div[class*='note-item'], a[class*='cover'], a[href*='/explore/']"
                )
                for elem in note_elements[:limit]:
                    try:
                        title_el = elem.query_selector(
                            "span[class*='title'], div[class*='title']"
                        )
                        title = title_el.inner_text() if title_el else ""
                        link = elem.get_attribute("href") or ""
                        if link and not link.startswith("http"):
                            link = f"https://www.xiaohongshu.com{link}"

                        if not title or "/explore/" not in link:
                            continue

                        author_el = elem.query_selector(
                            "span[class*='name'], div[class*='author']"
                        )
                        author = author_el.inner_text() if author_el else ""

                        likes_el = elem.query_selector(
                            "span[class*='like'], span[class*='count']"
                        )
                        likes_text = likes_el.inner_text() if likes_el else "0"
                        likes = _parse_count(likes_text)

                        items.append(
                            {
                                "title": title,
                                "desc": "",
                                "url": link,
                                "author_name": author,
                                "author_id": "",
                                "date": None,
                                "engagement": {
                                    "likes": likes,
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
                    "[爬虫-小红书] Playwright 未解析到结果；这通常是登录态失效、反爬验证或页面结构变更导致。\n"
                )
    except Exception as e:
        sys.stderr.write(f"[爬虫-小红书] 浏览器爬取失败: {e}\n")
    return items


def _submit_xhs_search(page, topic: str) -> bool:
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
    """Find note-card records in changing XHS search response envelopes."""

    def is_note_record(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        card = value.get("note_card")
        if isinstance(card, dict):
            return bool(
                (card.get("note_id") or value.get("id"))
                and (card.get("title") or card.get("display_title") or card.get("desc"))
            )
        return bool(
            (value.get("note_id") or value.get("id"))
            and (value.get("title") or value.get("display_title") or value.get("desc"))
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
    """Normalize one XHS note-card record captured by Playwright."""
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
        "title": note_card.get("display_title")
        or note_card.get("title")
        or raw.get("title", ""),
        "desc": note_card.get("desc")
        or note_card.get("description")
        or raw.get("desc", ""),
        "url": note_url,
        "author_name": user.get("nickname")
        or user.get("nick_name")
        or user.get("name", ""),
        "author_id": user.get("user_id") or user.get("userid") or user.get("id", ""),
        "date": None,
        "engagement": {
            "likes": _parse_count(
                str(interact.get("liked_count", raw.get("liked_count", "0")))
            ),
            "collects": _parse_count(
                str(interact.get("collected_count", raw.get("collected_count", "0")))
            ),
            "comments": _parse_count(
                str(interact.get("comment_count", raw.get("comment_count", "0")))
            ),
            "shares": _parse_count(
                str(interact.get("share_count", raw.get("share_count", "0")))
            ),
        },
        "hashtags": [],
        "images": [
            img.get("url_default") or img.get("url", "")
            for img in (note_card.get("image_list") or raw.get("image_list") or [])
            if isinstance(img, dict)
        ],
        "source": "crawler-xhr",
    }


def _crawl_xhs_public_pages(page, topic: str, limit: int) -> List[Dict[str, Any]]:
    """Use indexed IDs only for discovery, then read each note with Playwright."""
    from . import xiaohongshu

    items: List[Dict[str, Any]] = []
    for result in xiaohongshu._discover_site_results(topic, limit):
        try:
            page.goto(result["url"], wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(500)
            if urllib.parse.urlsplit(page.url).path.rstrip("/") == "/404":
                continue
            title = (
                page.locator("meta[property='og:title']").first.get_attribute("content")
                or page.title()
                or ""
            )
            desc = (
                page.locator("meta[property='og:description']").first.get_attribute(
                    "content"
                )
                or page.locator("meta[name='description']").first.get_attribute(
                    "content"
                )
                or ""
            )
            title = re.sub(r"\s*-\s*小红书\s*$", "", title).strip()
            desc = _clean_html(desc)
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


def crawl_douyin(topic: str, limit: int = 20) -> List[Dict[str, Any]]:
    """通过浏览器自动化爬取抖音搜索结果。

    v2.1：优先拦截 XHR `/aweme/v1/web/search/item/`，DOM 解析作为兜底。
    """
    if not is_playwright_available():
        return []

    items: List[Dict[str, Any]] = []
    try:
        with _launch_browser_context("douyin", mobile=True) as (browser, context, page):
            captured: Dict[str, Any] = {"payload": None}

            def _on_response(resp):
                try:
                    url = resp.url
                    if "/aweme/v1/web/search/item" in url and resp.status == 200:
                        data = resp.json()
                        if isinstance(data, dict) and data.get("data"):
                            captured["payload"] = data
                except Exception:
                    pass

            page.on("response", _on_response)

            search_url = f"https://www.douyin.com/search/{topic}?type=video"
            try:
                page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                sys.stderr.write(f"[爬虫-抖音] 页面加载失败: {e}\n")

            for _ in range(5):
                if captured["payload"]:
                    break
                try:
                    page.mouse.wheel(0, 2000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)

            payload = captured["payload"]
            if isinstance(payload, dict):
                for entry in (payload.get("data") or [])[:limit]:
                    aweme = entry.get("aweme_info") or entry
                    if not isinstance(aweme, dict):
                        continue
                    aweme_id = aweme.get("aweme_id", "")
                    author = aweme.get("author") or {}
                    stats = aweme.get("statistics") or {}
                    items.append(
                        {
                            "text": aweme.get("desc", ""),
                            "url": f"https://www.douyin.com/video/{aweme_id}"
                            if aweme_id
                            else "",
                            "author_name": author.get("nickname", ""),
                            "author_id": author.get("uid", ""),
                            "date": None,
                            "engagement": {
                                "views": stats.get("play_count", 0),
                                "likes": stats.get("digg_count", 0),
                                "comments": stats.get("comment_count", 0),
                                "shares": stats.get("share_count", 0),
                            },
                            "hashtags": [],
                            "duration": (aweme.get("duration") or 0) // 1000,
                            "source": "crawler-xhr",
                        }
                    )

            if not items:
                video_elements = page.query_selector_all(
                    "div[class*='video-card'], li[class*='search-result'], "
                    "div[class*='search-result-card']"
                )
                for elem in video_elements[:limit]:
                    try:
                        title_el = elem.query_selector(
                            "a[class*='title'], span[class*='title'], p[class*='desc']"
                        )
                        title = title_el.inner_text() if title_el else ""

                        link_el = elem.query_selector("a[href*='/video/']")
                        link = ""
                        if link_el:
                            href = link_el.get_attribute("href") or ""
                            if href.startswith("/"):
                                link = f"https://www.douyin.com{href}"
                            else:
                                link = href

                        if not title or "/video/" not in link:
                            continue

                        author_el = elem.query_selector(
                            "span[class*='author'], span[class*='nickname']"
                        )
                        author = author_el.inner_text() if author_el else ""

                        likes_el = elem.query_selector(
                            "span[class*='like'], span[class*='digg']"
                        )
                        likes = _parse_count(likes_el.inner_text() if likes_el else "0")

                        items.append(
                            {
                                "text": title,
                                "url": link,
                                "author_name": author,
                                "author_id": "",
                                "date": None,
                                "engagement": {
                                    "views": 0,
                                    "likes": likes,
                                    "comments": 0,
                                    "shares": 0,
                                },
                                "hashtags": [],
                                "duration": 0,
                                "source": "crawler-dom",
                            }
                        )
                    except Exception:
                        continue
            if not items:
                items = _crawl_douyin_public_pages(page, topic, limit)
    except Exception as e:
        sys.stderr.write(f"[爬虫-抖音] 浏览器爬取失败: {e}\n")
    return items


def _crawl_douyin_public_pages(page, topic: str, limit: int) -> List[Dict[str, Any]]:
    """Read public mobile share pages after the desktop search hits CAPTCHA."""
    from . import douyin

    items: List[Dict[str, Any]] = []
    for result in douyin._discover_site_results(topic, limit):
        match = re.search(r"/video/(\d+)", result["url"])
        if not match:
            continue
        aweme_id = match.group(1)
        try:
            page.goto(
                f"https://www.iesdouyin.com/share/video/{aweme_id}/",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            description = (
                page.locator("meta[name='description']").first.get_attribute("content")
                or page.locator("meta[property='og:description']").first.get_attribute(
                    "content"
                )
                or ""
            )
            text = _clean_html(description)
            if not text or relevance.token_overlap_relevance(topic, text) <= 0:
                continue
            items.append(
                {
                    "text": text,
                    "url": f"https://www.douyin.com/video/{aweme_id}",
                    "author_name": "",
                    "author_id": "",
                    "date": None,
                    "engagement": {"views": 0, "likes": 0, "comments": 0, "shares": 0},
                    "hashtags": re.findall(r"#([^#\s]+)#?", text),
                    "duration": 0,
                    "source": "crawler-public-page",
                }
            )
            if len(items) >= limit:
                break
        except Exception:
            continue
    if items:
        sys.stderr.write(
            f"[爬虫-抖音] 搜索页触发验证码，已从公开分享页读取 {len(items)} 条内容。\n"
        )
    return items


def crawl_bilibili(topic: str, limit: int = 20) -> List[Dict[str, Any]]:
    """通过浏览器自动化爬取B站搜索结果。"""
    if not is_playwright_available():
        return []

    items: List[Dict[str, Any]] = []
    try:
        with _launch_browser_context("bilibili") as (browser, context, page):
            search_url = (
                f"https://search.bilibili.com/all?keyword={topic}&order=totalrank"
            )
            try:
                page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                sys.stderr.write(f"[爬虫-B站] 页面加载失败: {e}\n")

            _wait_for(
                page,
                lambda: bool(
                    page.query_selector(
                        "div.bili-video-card, div[class*='video-list-item'], a[href*='/video/BV']"
                    )
                ),
                timeout_ms=8000,
            )

            video_elements = page.query_selector_all(
                "div.bili-video-card, div[class*='video-list-item'], "
                "div[class*='video-item']"
            )
            for elem in video_elements[:limit]:
                try:
                    title_el = elem.query_selector(
                        "h3[class*='title'], a[class*='title']"
                    )
                    title = _clean_html(title_el.inner_text()) if title_el else ""

                    link_el = elem.query_selector("a[href*='/video/']")
                    link = ""
                    if link_el:
                        href = link_el.get_attribute("href") or ""
                        if href.startswith("//"):
                            link = f"https:{href}"
                        elif href.startswith("/"):
                            link = f"https://www.bilibili.com{href}"
                        else:
                            link = href

                    if not title or "/video/" not in link:
                        continue

                    author_el = elem.query_selector(
                        "span[class*='name'], span.bili-video-card__info--author"
                    )
                    author = author_el.inner_text() if author_el else ""

                    views_el = elem.query_selector(
                        "span[class*='play'], span[class*='view']"
                    )
                    views = _parse_count(views_el.inner_text() if views_el else "0")

                    items.append(
                        {
                            "title": title,
                            "url": link,
                            "bvid": "",
                            "channel_name": author,
                            "author_mid": "",
                            "date": None,
                            "duration": "",
                            "description": "",
                            "engagement": {
                                "views": views,
                                "danmaku": 0,
                                "comments": 0,
                                "favorites": 0,
                                "likes": 0,
                            },
                            "source": "crawler",
                        }
                    )
                except Exception:
                    continue

            if not items:
                seen = set()
                link_elements = page.query_selector_all("a[href*='/video/BV']")
                for link_el in link_elements:
                    try:
                        href = link_el.get_attribute("href") or ""
                        if href.startswith("//"):
                            link = f"https:{href}"
                        elif href.startswith("/"):
                            link = f"https://www.bilibili.com{href}"
                        else:
                            link = href
                        link = link.split("?", 1)[0]
                        if not link or link in seen:
                            continue
                        title = _clean_html(
                            link_el.get_attribute("title") or link_el.inner_text() or ""
                        )
                        if not title:
                            continue
                        seen.add(link)
                        match = re.search(r"/video/(BV[0-9A-Za-z]+)", link)
                        items.append(
                            {
                                "title": title,
                                "url": link,
                                "bvid": match.group(1) if match else "",
                                "channel_name": "",
                                "author_mid": "",
                                "date": None,
                                "duration": "",
                                "description": "",
                                "engagement": {
                                    "views": 0,
                                    "danmaku": 0,
                                    "comments": 0,
                                    "favorites": 0,
                                    "likes": 0,
                                },
                                "source": "crawler-link",
                            }
                        )
                        if len(items) >= limit:
                            break
                    except Exception:
                        continue
    except Exception as e:
        sys.stderr.write(f"[爬虫-B站] 浏览器爬取失败: {e}\n")
    return items


def _parse_count(text: str) -> int:
    """解析数量文本，支持 '1.2万' / '1.2w' / '12k' 等格式。"""
    if not text:
        return 0
    text = str(text).strip().replace(",", "")
    try:
        if "万" in text or text.lower().endswith("w"):
            num = float(re.sub(r"[万wW]", "", text))
            return int(num * 10000)
        elif "亿" in text:
            num = float(text.replace("亿", ""))
            return int(num * 100000000)
        elif text.lower().endswith("k"):
            num = float(text[:-1])
            return int(num * 1000)
        return int(float(re.sub(r"[^\d.]", "", text or "0")))
    except (ValueError, TypeError):
        return 0


def get_crawler_status() -> Dict[str, Any]:
    """获取爬虫引擎的状态信息。"""
    pw_available = is_playwright_available()
    cached_platforms = []

    if COOKIE_DIR.exists():
        for f in COOKIE_DIR.glob("*_cookies.json"):
            platform = f.stem.replace("_cookies", "")
            cached_platforms.append(platform)

    return {
        "playwright_available": pw_available,
        "cached_logins": cached_platforms,
        "cookie_dir": str(COOKIE_DIR),
        "browser": _browser_status(),
    }
