"""Consistent browser request profiles shared by all collectors."""

from __future__ import annotations

import os
import re
from typing import Dict, Optional


_FALLBACK_CHROME_VERSION = "151.0.7922.34"
_VERSION_ENV = "AGENTSCROLL_HTTP_CHROME_VERSION"


def _chrome_version() -> str:
    configured = os.environ.get(_VERSION_ENV, "").strip()
    if re.fullmatch(r"\d+(?:\.\d+){0,3}", configured):
        return configured
    return _FALLBACK_CHROME_VERSION


CHROME_VERSION = _chrome_version()


def desktop_chrome_ua(version: str = CHROME_VERSION) -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36"
    )


def mobile_chrome_ua(version: str = CHROME_VERSION) -> str:
    return (
        "Mozilla/5.0 (Linux; Android 14; Pixel 7) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} "
        "Mobile Safari/537.36"
    )


DESKTOP_CHROME_UA = desktop_chrome_ua()
MOBILE_CHROME_UA = mobile_chrome_ua()


def browser_headers(
    *,
    referer: Optional[str] = None,
    mobile: bool = False,
    api: bool = False,
) -> Dict[str, str]:
    """Build one coherent Chrome header profile for navigation or JSON fetches.

    Client Hint and Sec-Fetch headers are intentionally not fabricated here:
    their exact values depend on the target URL and browser negotiation. A
    smaller internally consistent profile is less suspicious than contradictory
    Windows/Mac/mobile hints.
    """
    headers = {
        "User-Agent": MOBILE_CHROME_UA if mobile else DESKTOP_CHROME_UA,
        "Accept": (
            "application/json, text/plain, */*"
            if api
            else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    if not api:
        headers["Upgrade-Insecure-Requests"] = "1"
    return headers


def navigation_headers(
    referer: Optional[str] = None,
    *,
    mobile: bool = False,
) -> Dict[str, str]:
    return browser_headers(referer=referer, mobile=mobile, api=False)


def api_headers(
    referer: Optional[str] = None,
    *,
    mobile: bool = False,
) -> Dict[str, str]:
    return browser_headers(referer=referer, mobile=mobile, api=True)
