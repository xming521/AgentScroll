"""Build and persist compact, model-facing search artifacts."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional
from urllib.parse import urlsplit

CONTENT_CHAR_LIMIT_ENV = "AGENTSCROLL_CONTENT_CHAR_LIMIT"
_DEFAULT_COMMENT_LIMIT = 10
_DEFAULT_CONTENT_CHAR_LIMIT = 2_000
_MIN_CONTENT_CHAR_LIMIT = 200
_MAX_CONTENT_CHAR_LIMIT = 20_000
_write_lock = threading.Lock()
_WEIBO_PLACEHOLDER_COMMENT_RE = re.compile(
    r"^(?:回复@.*?[:：]\s*)?(?:图片评论(?:\s*网页链接)?|转发微博)$"
)


def _output_dir(configured: Optional[str | Path]) -> Path:
    if configured is not None:
        return Path(configured).expanduser().resolve()
    return (Path.cwd() / "outputs" / "knowledge").resolve()


def _filename(topic: str, recorded_at: datetime) -> str:
    slug = re.sub(r"[^\w.-]+", "_", topic.strip(), flags=re.UNICODE)
    slug = slug.strip("._")[:60] or "search"
    timestamp = recorded_at.strftime("%Y%m%d-%H%M%S-%f%z")
    return f"{timestamp}_{slug}.txt"


def _compact_engagement(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): count
        for key, count in value.items()
        if isinstance(count, (int, float)) and not isinstance(count, bool) and count > 0
    }


def _compact_comments(
    value: Any,
    *,
    platform: str = "",
) -> list[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    retained: list[Dict[str, Any]] = []
    seen_texts: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        text = str(raw.get("text") or "").strip()
        if not text or text in seen_texts:
            continue
        if platform == "weibo" and _WEIBO_PLACEHOLDER_COMMENT_RE.fullmatch(text):
            continue
        seen_texts.add(text)
        comment: Dict[str, Any] = {"text": text}
        likes = raw.get("likes")
        if (
            isinstance(likes, (int, float))
            and not isinstance(likes, bool)
            and likes > 0
        ):
            comment["likes"] = likes
        retained.append(comment)

    # Python's sort is stable, so equal-like and unknown-like comments retain
    # the platform's original order while the most endorsed comments come first.
    retained.sort(key=lambda comment: comment.get("likes", 0), reverse=True)
    return retained[:_DEFAULT_COMMENT_LIMIT]


def _knowledge_url(item: Mapping[str, Any]) -> str:
    candidates: Iterable[Any] = (item.get("url"), item.get("content_url"))
    for candidate in candidates:
        url = str(candidate or "").strip()
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return url
    return ""


def _content_char_limit() -> int:
    raw = os.environ.get(CONTENT_CHAR_LIMIT_ENV, "").strip()
    try:
        configured = int(raw) if raw else _DEFAULT_CONTENT_CHAR_LIMIT
    except ValueError:
        configured = _DEFAULT_CONTENT_CHAR_LIMIT
    return max(_MIN_CONTENT_CHAR_LIMIT, min(configured, _MAX_CONTENT_CHAR_LIMIT))


def _compact_content(value: Any) -> tuple[str, bool, int]:
    content = str(value or "").strip()
    original_chars = len(content)
    limit = _content_char_limit()
    if original_chars <= limit:
        return content, False, original_chars

    suffix = f"…（正文已截断，原文共{original_chars}字）"
    prefix_budget = max(1, limit - len(suffix))
    prefix = content[:prefix_budget].rstrip()
    minimum_boundary = max(80, int(prefix_budget * 0.65))
    boundary = -1
    for separator in ("\n\n", "\n", "。", "！", "？", "；", ".", "!", "?", ";"):
        index = prefix.rfind(separator)
        if index >= minimum_boundary:
            boundary = max(boundary, index + len(separator))
    if boundary > 0:
        prefix = prefix[:boundary].rstrip()
    return f"{prefix}{suffix}", True, original_chars


def _compact_item(platform: str, item: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(item, Mapping):
        return None
    content, content_truncated, original_chars = _compact_content(item.get("content"))
    url = _knowledge_url(item)
    if not content or not url:
        return None

    compact: Dict[str, Any] = {
        "platform": platform,
        "published_at": item.get("date"),
        "content": content,
        "url": url,
    }
    if content_truncated:
        compact["content_truncated"] = True
        compact["content_original_chars"] = original_chars
    title = str(item.get("title") or "").strip()
    if title:
        compact["title"] = title

    hashtags = []
    for value in item.get("hashtags") or []:
        tag = str(value or "").strip().lstrip("#")
        if tag and tag not in hashtags:
            hashtags.append(tag)
        if len(hashtags) >= 10:
            break
    if hashtags:
        compact["hashtags"] = hashtags

    engagement = _compact_engagement(item.get("engagement"))
    if engagement:
        compact["engagement"] = engagement

    retained_comments = _compact_comments(item.get("comments"), platform=platform)
    if retained_comments:
        compact["comments"] = retained_comments
    return compact


def build_knowledge_document(result: Mapping[str, Any]) -> Dict[str, Any]:
    """Reduce the collector response to fields useful for model learning."""
    compact_items: list[Dict[str, Any]] = []
    sources = result.get("sources")
    if isinstance(sources, Mapping):
        for platform, payload in sources.items():
            if not isinstance(payload, Mapping):
                continue
            for item in payload.get("items") or []:
                compact = _compact_item(str(platform), item)
                if compact:
                    compact_items.append(compact)

    # Unknown dates come first. Known dates are ascending, so the newest
    # material is closest to the end of the model-facing context.
    compact_items.sort(
        key=lambda item: (
            bool(str(item.get("published_at") or "").strip()),
            str(item.get("published_at") or "").strip(),
        )
    )

    return {
        "query": str(result.get("topic") or ""),
        "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "routing": result.get("routing") or {},
        "date_range": {
            "from": result.get("from_date"),
            "to": result.get("to_date"),
        },
        "platforms": list(sources) if isinstance(sources, Mapping) else [],
        "items": compact_items,
    }


def _single_line(value: Any) -> str:
    """Collapse arbitrary text to one compact line."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def render_knowledge_text(document: Mapping[str, Any]) -> str:
    """Render a compact plain-text knowledge artifact."""
    lines = [f"查询: {_single_line(document.get('query'))}"]

    for item in document.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        published_at = _single_line(item.get("published_at"))
        platform = _single_line(item.get("platform")) or "unknown"
        title = _single_line(item.get("title"))
        heading = f"[{published_at}|{platform}]" if published_at else f"[{platform}]"
        if title:
            heading = f"{heading} {title}"
        lines.extend(("", heading, _single_line(item.get("content"))))

        hashtags = item.get("hashtags") or []
        if hashtags:
            lines.append(
                "标签: " + " ".join(f"#{_single_line(tag)}" for tag in hashtags)
            )

        engagement = item.get("engagement")
        if isinstance(engagement, Mapping) and engagement:
            lines.append(
                "互动: "
                + " ".join(f"{key}={value}" for key, value in engagement.items())
            )

        comments = item.get("comments") or []
        if comments:
            lines.append("评论:")
            for comment in comments:
                if not isinstance(comment, Mapping):
                    continue
                lines.append(f"- {_single_line(comment.get('text'))}")

    return "\n".join(lines).rstrip() + "\n"


def save_knowledge_document(
    result: Mapping[str, Any],
    *,
    output_dir: Optional[str | Path] = None,
) -> Path:
    """Save paired JSON metadata and compact text artifacts for this search."""
    document = build_knowledge_document(result)
    recorded_at = datetime.now().astimezone()
    destination = _output_dir(output_dir)
    text_path = destination / _filename(document["query"], recorded_at)
    metadata_path = text_path.with_suffix(".json")
    text_temporary = text_path.with_suffix(f"{text_path.suffix}.tmp")
    metadata_temporary = metadata_path.with_suffix(f"{metadata_path.suffix}.tmp")
    serialized_text = render_knowledge_text(document)
    serialized_metadata = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    with _write_lock:
        destination.mkdir(parents=True, exist_ok=True)
        text_temporary.write_text(serialized_text, encoding="utf-8")
        metadata_temporary.write_text(serialized_metadata, encoding="utf-8")
        metadata_temporary.replace(metadata_path)
        text_temporary.replace(text_path)
    return text_path
