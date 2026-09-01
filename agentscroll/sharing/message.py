"""Shared rendering for outbound share messages."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def render_share_messages(share: Mapping[str, Any]) -> tuple[str, str]:
    text = str(share.get("text", "")).strip()
    url = str(share.get("url", "")).strip()
    comment = str(share.get("comment", "")).strip()
    if not text or not url or not comment:
        raise ValueError("分享项缺少 text、url 或 comment")
    return "\n".join((text, url)), comment


__all__ = ["render_share_messages"]
