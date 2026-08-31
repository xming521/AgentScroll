"""Shared rendering for one outbound share."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def render_share_message(share: Mapping[str, Any]) -> str:
    text = str(share.get("text", "")).strip()
    url = str(share.get("url", "")).strip()
    comment = str(share.get("comment", "")).strip()
    if not text or not url or not comment:
        raise ValueError("分享项缺少 text、url 或 comment")
    label = "网友评论" if share.get("comment_type") == "platform" else "我的评论"
    return "\n".join((text, url, f"{label}：{comment}"))


__all__ = ["render_share_message"]
