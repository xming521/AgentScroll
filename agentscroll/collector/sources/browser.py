"""Shared Playwright lifecycle, browser configuration, and cookie persistence."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .request_profiles import desktop_chrome_ua, mobile_chrome_ua


COOKIE_DIR = Path.home() / ".config" / "agentscroll" / "browser_cookies"
_playwright_available: bool | None = None
_BROWSER_PATH_ENV = "AGENTSCROLL_BROWSER_PATH"
_BROWSER_CHANNEL_ENV = "AGENTSCROLL_BROWSER_CHANNEL"
_DISABLE_BROWSER_ENV = "AGENTSCROLL_DISABLE_BROWSER"


def is_playwright_available() -> bool:
    """Return whether Playwright is installed and browser use is enabled."""
    global _playwright_available
    if _playwright_available is not None:
        return _playwright_available
    if os.environ.get(_DISABLE_BROWSER_ENV, "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        _playwright_available = False
        return _playwright_available
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401

        _playwright_available = True
    except (ImportError, OSError):
        _playwright_available = False
    return _playwright_available


def _browser_launch_kwargs() -> dict[str, Any]:
    """Return optional Playwright launch overrides for old machines."""
    kwargs: dict[str, Any] = {}
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


def _browser_status() -> dict[str, Any]:
    """Describe the selected browser without starting it."""
    browser_path = os.environ.get(_BROWSER_PATH_ENV, "").strip()
    browser_channel = os.environ.get(_BROWSER_CHANNEL_ENV, "").strip()
    disabled = os.environ.get(_DISABLE_BROWSER_ENV, "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    expanded_path = os.path.expanduser(browser_path) if browser_path else ""
    return {
        "mode": (
            "disabled"
            if disabled
            else (
                "external-path"
                if browser_path
                else ("channel" if browser_channel else "managed")
            )
        ),
        "path": expanded_path or None,
        "path_exists": bool(expanded_path and Path(expanded_path).is_file()),
        "channel": browser_channel or None,
        "disable_env": _DISABLE_BROWSER_ENV,
    }


def _cookie_path(platform: str) -> Path:
    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    return COOKIE_DIR / f"{platform}_cookies.json"


def _save_cookies(platform: str, cookies: list[Any]) -> None:
    _cookie_path(platform).write_text(
        json.dumps(cookies, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_cookies(platform: str) -> list[Any] | None:
    path = _cookie_path(platform)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, list) else None


@contextmanager
def browser_context(
    platform: str,
    *,
    mobile: bool = False,
    headless: bool = True,
) -> Iterator[tuple[Any, Any, Any]]:
    """Yield one browser, context, and page with persisted platform cookies."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        playwright_browser = playwright.chromium.launch(
            headless=headless,
            **_browser_launch_kwargs(),
        )
        try:
            device_name = "Pixel 7" if mobile else "Desktop Chrome HiDPI"
            context_options = dict(playwright.devices.get(device_name, {}))
            has_device_descriptor = bool(context_options)
            context_options.pop("default_browser_type", None)
            version = playwright_browser.version or "151.0.7922.34"
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
            context = playwright_browser.new_context(**context_options)
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined});"
            )
            cookies = _load_cookies(platform)
            if cookies:
                try:
                    context.add_cookies(cookies)
                except Exception as exc:
                    sys.stderr.write(f"[爬虫-{platform}] 加载 Cookie 失败: {exc}\n")

            page = context.new_page()
            try:
                yield playwright_browser, context, page
            finally:
                try:
                    _save_cookies(platform, context.cookies())
                except Exception:
                    pass
        finally:
            try:
                playwright_browser.close()
            except Exception:
                pass


def wait_for(
    page: Any,
    predicate: Callable[[], bool],
    *,
    timeout_ms: int = 8000,
    interval_ms: int = 500,
) -> bool:
    """Poll a page condition until it succeeds or the timeout expires."""
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


def get_browser_status() -> dict[str, Any]:
    """Return Playwright availability, cached platforms, and launch settings."""
    cached_platforms = []
    if COOKIE_DIR.exists():
        cached_platforms = sorted(
            path.stem.removesuffix("_cookies")
            for path in COOKIE_DIR.glob("*_cookies.json")
        )
    return {
        "playwright_available": is_playwright_available(),
        "cached_logins": cached_platforms,
        "cookie_dir": str(COOKIE_DIR),
        "browser": _browser_status(),
    }


__all__ = [
    "browser_context",
    "get_browser_status",
    "is_playwright_available",
    "wait_for",
]
