"""Shared public web-search fallback helpers."""

from __future__ import annotations

import base64
import html
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Callable, Dict, Iterable, List, Optional

from .request_profiles import navigation_headers


class _BingResultsParser(HTMLParser):
    """Extract result links without depending on Bing's exact nested markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: List[Dict[str, str]] = []
        self._active = False
        self._in_h2 = False
        self._in_title_link = False
        self._in_snippet = False
        self._href = ""
        self._title: List[str] = []
        self._snippet: List[str] = []

    @staticmethod
    def _attrs(attrs) -> Dict[str, str]:
        return {key.lower(): value or "" for key, value in attrs}

    def _finish(self) -> None:
        if not self._active:
            return
        self.results.append({
            "url": self._href.strip(),
            "title": _clean_text(" ".join(self._title)),
            "snippet": _clean_text(" ".join(self._snippet)),
        })
        self._active = False
        self._in_h2 = False
        self._in_title_link = False
        self._in_snippet = False
        self._href = ""
        self._title = []
        self._snippet = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_dict = self._attrs(attrs)
        classes = attrs_dict.get("class", "").split()
        if tag == "li" and "b_algo" in classes:
            self._finish()
            self._active = True
            return
        if not self._active:
            return
        if tag == "h2":
            self._in_h2 = True
        elif tag == "a" and self._in_h2 and not self._href:
            self._href = attrs_dict.get("href", "")
            self._in_title_link = True
        elif tag == "p" and not self._snippet:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if not self._active:
            return
        if tag == "a":
            self._in_title_link = False
        elif tag == "h2":
            self._in_h2 = False
            self._in_title_link = False
        elif tag == "p":
            self._in_snippet = False
        elif tag == "li":
            self._finish()

    def handle_data(self, data: str) -> None:
        if self._in_title_link:
            self._title.append(data)
        elif self._in_snippet:
            self._snippet.append(data)

    def close(self) -> None:
        super().close()
        self._finish()


class _DuckDuckGoResultsParser(HTMLParser):
    """Extract organic results from DuckDuckGo's no-JavaScript HTML page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: List[Dict[str, str]] = []
        self._active = False
        self._div_depth = 0
        self._in_title = False
        self._in_snippet = False
        self._href = ""
        self._title: List[str] = []
        self._snippet: List[str] = []

    @staticmethod
    def _attrs(attrs) -> Dict[str, str]:
        return {key.lower(): value or "" for key, value in attrs}

    def _finish(self) -> None:
        if not self._active:
            return
        self.results.append({
            "url": self._href.strip(),
            "title": _clean_text(" ".join(self._title)),
            "snippet": _clean_text(" ".join(self._snippet)),
        })
        self._active = False
        self._div_depth = 0
        self._in_title = False
        self._in_snippet = False
        self._href = ""
        self._title = []
        self._snippet = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_dict = self._attrs(attrs)
        classes = attrs_dict.get("class", "").split()
        if tag == "div" and "result" in classes and not self._active:
            self._active = True
            self._div_depth = 1
            return
        if not self._active:
            return
        if tag == "div":
            self._div_depth += 1
        if tag == "a" and "result__a" in classes:
            self._href = attrs_dict.get("href", "")
            self._in_title = True
        elif "result__snippet" in classes:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if not self._active:
            return
        if tag == "a":
            self._in_title = False
            self._in_snippet = False
        elif tag == "div":
            self._div_depth -= 1
            if self._div_depth == 0:
                self._finish()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title.append(data)
        elif self._in_snippet:
            self._snippet.append(data)

    def close(self) -> None:
        super().close()
        self._finish()


class _YahooResultsParser(HTMLParser):
    """Collect result anchors; target-domain validation happens after decoding."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: List[Dict[str, str]] = []
        self._href = ""
        self._title: List[str] = []

    @staticmethod
    def _attrs(attrs) -> Dict[str, str]:
        return {key.lower(): value or "" for key, value in attrs}

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "a" or self._href:
            return
        self._href = self._attrs(attrs).get("href", "")
        self._title = []

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._href:
            return
        self.results.append({
            "url": self._href,
            "title": _clean_text(" ".join(self._title)),
            "snippet": "",
        })
        self._href = ""
        self._title = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._title.append(data)


class _PageMetadataParser(HTMLParser):
    """Extract stable metadata from a public detail page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_title = False
        self._title: List[str] = []
        self.meta: Dict[str, str] = {}
        self.canonical = ""

    @staticmethod
    def _attrs(attrs) -> Dict[str, str]:
        return {key.lower(): value or "" for key, value in attrs}

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_dict = self._attrs(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (attrs_dict.get("property") or attrs_dict.get("name", "")).lower()
            if key in {"description", "og:title", "og:description"}:
                self.meta[key] = _clean_text(attrs_dict.get("content", ""))
        elif tag == "link" and "canonical" in attrs_dict.get("rel", "").lower().split():
            self.canonical = html.unescape(attrs_dict.get("href", "")).strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title.append(data)

    @property
    def title(self) -> str:
        return _clean_text(" ".join(self._title))


def _clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", html.unescape(value or ""))
    return re.sub(r"\s+", " ", value).strip()


def parse_page_metadata(page_html: str) -> Dict[str, str]:
    """Return title/description/canonical fields from a platform detail page."""
    parser = _PageMetadataParser()
    parser.feed(page_html or "")
    parser.close()
    return {
        "title": parser.meta.get("og:title") or parser.title,
        "description": parser.meta.get("og:description") or parser.meta.get("description", ""),
        "canonical": parser.canonical,
    }


def decode_bing_url(url: str) -> str:
    """Resolve the destination embedded in a Bing ``/ck/a`` redirect URL."""
    candidate = html.unescape(url or "").strip()
    if not candidate:
        return ""

    parsed = urllib.parse.urlsplit(candidate)
    host = (parsed.hostname or "").lower()
    if "bing.com" not in host or "/ck/a" not in parsed.path:
        return candidate

    params = urllib.parse.parse_qs(parsed.query)
    for key in ("u", "url", "rurl"):
        for raw_value in params.get(key, []):
            value = urllib.parse.unquote(raw_value)
            if value.startswith(("http://", "https://")):
                return value
            encoded = value[2:] if value.startswith("a1") else value
            try:
                padding = "=" * (-len(encoded) % 4)
                decoded = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                continue
            if decoded.startswith(("http://", "https://")):
                return decoded
    return candidate


def decode_duckduckgo_url(url: str) -> str:
    """Resolve the destination in DuckDuckGo's ``/l/?uddg=`` redirect."""
    candidate = html.unescape(url or "").strip()
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    if not candidate:
        return ""

    parsed = urllib.parse.urlsplit(candidate)
    host = (parsed.hostname or "").lower()
    if host not in {"duckduckgo.com", "www.duckduckgo.com"} or parsed.path != "/l/":
        return candidate
    values = urllib.parse.parse_qs(parsed.query).get("uddg", [])
    return values[0] if values else candidate


def decode_yahoo_url(url: str) -> str:
    """Resolve the destination in Yahoo's ``/RU=.../RK=`` redirect."""
    candidate = html.unescape(url or "").strip()
    if not candidate:
        return ""

    parsed = urllib.parse.urlsplit(candidate)
    host = (parsed.hostname or "").lower()
    if host != "r.search.yahoo.com":
        return candidate
    match = re.search(r"/RU=(.*?)/RK=", parsed.path, flags=re.IGNORECASE)
    return urllib.parse.unquote(match.group(1)) if match else candidate


def parse_bing_results(
    page_html: str,
    *,
    allowed_url_fragments: Iterable[str] = (),
    limit: int = 10,
) -> List[Dict[str, str]]:
    """Parse and validate organic results from a Bing HTML response."""
    parser = _BingResultsParser()
    parser.feed(page_html or "")
    parser.close()

    allowed = tuple(fragment.lower() for fragment in allowed_url_fragments)
    results: List[Dict[str, str]] = []
    seen = set()
    for raw in parser.results:
        target = decode_bing_url(raw.get("url", ""))
        lowered = target.lower()
        if not target or (allowed and not any(fragment in lowered for fragment in allowed)):
            continue
        if target in seen or not raw.get("title"):
            continue
        seen.add(target)
        results.append({
            "title": raw["title"],
            "snippet": raw.get("snippet", ""),
            "url": target,
        })
        if len(results) >= limit:
            break
    return results


def parse_duckduckgo_results(
    page_html: str,
    *,
    allowed_url_fragments: Iterable[str] = (),
    limit: int = 10,
) -> List[Dict[str, str]]:
    """Parse and validate organic results from DuckDuckGo HTML."""
    parser = _DuckDuckGoResultsParser()
    parser.feed(page_html or "")
    parser.close()

    allowed = tuple(fragment.lower() for fragment in allowed_url_fragments)
    results: List[Dict[str, str]] = []
    seen = set()
    for raw in parser.results:
        target = decode_duckduckgo_url(raw.get("url", ""))
        lowered = target.lower()
        if not target or (allowed and not any(fragment in lowered for fragment in allowed)):
            continue
        if target in seen or not raw.get("title"):
            continue
        seen.add(target)
        results.append({
            "title": raw["title"],
            "snippet": raw.get("snippet", ""),
            "url": target,
        })
        if len(results) >= limit:
            break
    return results


def parse_yahoo_results(
    page_html: str,
    *,
    allowed_url_fragments: Iterable[str] = (),
    limit: int = 10,
) -> List[Dict[str, str]]:
    """Parse Yahoo result links and keep only validated target URLs."""
    parser = _YahooResultsParser()
    parser.feed(page_html or "")
    parser.close()

    allowed = tuple(fragment.lower() for fragment in allowed_url_fragments)
    results: List[Dict[str, str]] = []
    seen = set()
    for raw in parser.results:
        target = decode_yahoo_url(raw.get("url", ""))
        lowered = target.lower()
        if not target or (allowed and not any(fragment in lowered for fragment in allowed)):
            continue
        if target in seen or not raw.get("title"):
            continue
        seen.add(target)
        results.append({
            "title": raw["title"],
            "snippet": "",
            "url": target,
        })
        if len(results) >= limit:
            break
    return results


def fetch_html(url: str, timeout: int = 10) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if "duckduckgo.com" in host:
        referer = "https://duckduckgo.com/"
    elif "yahoo.com" in host:
        referer = "https://search.yahoo.com/"
    else:
        referer = "https://cn.bing.com/"
    headers = navigation_headers(referer)
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def search_bing(
    query: str,
    *,
    allowed_url_fragments: Iterable[str] = (),
    limit: int = 10,
    fetcher: Optional[Callable[[str], str]] = None,
) -> List[Dict[str, str]]:
    """Fetch one Bing result page and return validated destination links."""
    encoded = urllib.parse.quote(query)
    url = f"https://cn.bing.com/search?q={encoded}&setmkt=zh-CN&ensearch=0&count={limit}"
    page_html = (fetcher or fetch_html)(url)
    return parse_bing_results(
        page_html,
        allowed_url_fragments=allowed_url_fragments,
        limit=limit,
    )


def search_duckduckgo(
    query: str,
    *,
    allowed_url_fragments: Iterable[str] = (),
    limit: int = 10,
    fetcher: Optional[Callable[[str], str]] = None,
) -> List[Dict[str, str]]:
    """Fetch DuckDuckGo's no-JavaScript page and return validated links."""
    encoded = urllib.parse.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}&kl=cn-zh"
    page_html = (fetcher or fetch_html)(url)
    return parse_duckduckgo_results(
        page_html,
        allowed_url_fragments=allowed_url_fragments,
        limit=limit,
    )


def search_yahoo(
    query: str,
    *,
    allowed_url_fragments: Iterable[str] = (),
    limit: int = 10,
    fetcher: Optional[Callable[[str], str]] = None,
) -> List[Dict[str, str]]:
    """Fetch Yahoo's public result page and return validated destination links."""
    encoded = urllib.parse.quote(query)
    url = f"https://search.yahoo.com/search?p={encoded}"
    page_html = (fetcher or fetch_html)(url)
    return parse_yahoo_results(
        page_html,
        allowed_url_fragments=allowed_url_fragments,
        limit=limit,
    )
