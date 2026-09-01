"""Shared comment result schema; platform fetching stays in each source module."""

from __future__ import annotations

import html
import os
import re
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from . import dates


COMMENT_LIMIT_ENV = "AGENTSCROLL_COMMENT_LIMIT"
_DEFAULT_COMMENT_LIMIT = 20
_MAX_COMMENT_LIMIT = 100


def limit() -> int:
    """Return the maximum number of textual comments retained per post."""
    raw = os.environ.get(COMMENT_LIMIT_ENV, "").strip()
    try:
        configured = int(raw) if raw else _DEFAULT_COMMENT_LIMIT
    except ValueError:
        configured = _DEFAULT_COMMENT_LIMIT
    return max(1, min(configured, _MAX_COMMENT_LIMIT))


def clean_text(value: Any) -> str:
    """Normalize comment HTML/text without retaining author information."""
    text = re.sub(r"<[^>]+>", "", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def count(value: Any) -> int:
    """Normalize abbreviated counters such as ``1.2万``, ``1.2w``, or ``12k``."""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip().replace(",", "").rstrip("+")
    multiplier = 1
    lowered = text.lower()
    if text.endswith("万") or lowered.endswith("w"):
        multiplier = 10_000
        text = text[:-1]
    elif text.endswith("亿"):
        multiplier = 100_000_000
        text = text[:-1]
    elif lowered.endswith("k"):
        multiplier = 1_000
        text = text[:-1]
    try:
        return max(0, int(float(text) * multiplier))
    except ValueError:
        return 0


def timestamp(value: Any, *, milliseconds: bool = False) -> Optional[str]:
    """Convert an epoch value to an ISO timestamp in the collector timezone."""
    if value in (None, ""):
        return None
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return str(value).strip() or None
    if milliseconds:
        raw /= 1000
    try:
        return datetime.fromtimestamp(raw, tz=dates.CST).isoformat()
    except (OverflowError, OSError, ValueError):
        return str(value).strip() or None


def entry(
    comment_id: Any,
    text: Any,
    *,
    created_at: Any = None,
    likes: Any = None,
    reply_count: Any = None,
    parent_comment_id: Any = None,
) -> Optional[Dict[str, Any]]:
    """Build one author-free, JSON-serializable comment entry."""
    normalized_text = clean_text(text)
    if not normalized_text:
        return None
    result: Dict[str, Any] = {
        "comment_id": str(comment_id or ""),
        "text": normalized_text,
    }
    if created_at not in (None, ""):
        result["created_at"] = str(created_at)
    if likes not in (None, ""):
        result["likes"] = count(likes)
    if reply_count not in (None, ""):
        result["reply_count"] = count(reply_count)
    if parent_comment_id not in (None, ""):
        result["parent_comment_id"] = str(parent_comment_id)
    return result


def apply(
    item: Dict[str, Any],
    values: Iterable[Optional[Dict[str, Any]]],
    *,
    status: str,
    source: str,
    evidence: Optional[Dict[str, Any]] = None,
) -> None:
    """Attach normalized comments and explicit access status to one post."""
    retained = [value for value in values if value][:limit()]
    item["comments"] = retained
    item["comments_status"] = "readable" if retained else status
    item["comments_source"] = source
    item["comment_extraction_evidence"] = evidence or {}


def browser_allowed() -> bool:
    """Whether signed comment requests may be captured from a real browser."""
    disabled = os.environ.get("AGENTSCROLL_DISABLE_BROWSER", "").strip().lower()
    if disabled in {"1", "true", "yes", "on"}:
        detail_override = os.environ.get(
            "AGENTSCROLL_ALLOW_DETAIL_BROWSER", ""
        ).strip().lower()
        comment_override = os.environ.get(
            "AGENTSCROLL_ALLOW_COMMENT_BROWSER", ""
        ).strip().lower()
        return detail_override in {"1", "true", "yes", "on"} or comment_override in {
            "1", "true", "yes", "on",
        }
    return True
