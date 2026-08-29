"""Fetch grouped hot lists from a self-hosted NewsNow instance."""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit

from .sources.http import get

NEWSNOW_BASE_URL_ENV = "AGENTSCROLL_NEWSNOW_BASE_URL"
DEFAULT_NEWSNOW_BASE_URL = "http://127.0.0.1:4444"

# These groups are based on the category column in the NewsNow source table in
# docs/usage.md. "综合" is the default cross-platform group for agents.
# Source order determines the order seen by downstream agents.
NEWSNOW_GROUPS: dict[str, tuple[str, ...]] = {
    "社区/科技": ("v2ex-share",),
    "综合": (
        "weibo",
        "hupu",
        "tieba",
        "bilibili-hot-search",
        "zhihu",
    ),
    "社媒": ("weibo",),
    "新闻": ("zaobao", "toutiao", "thepaper", "ifeng", "tencent-hot"),
    "科技社区": ("coolapk", "pcbeta-windows11"),
    "财经": (
        "mktnews-flash",
        "wallstreetcn-quick",
        "wallstreetcn-news",
        "wallstreetcn-hot",
        "cls-telegraph",
        "cls-depth",
        "cls-hot",
        "gelonghui",
        "fastbull-express",
        "fastbull-news",
        "jin10",
    ),
    "科技/创投": ("36kr-quick", "36kr-renqi"),
    "短视频": ("douyin", "kuaishou"),
    "体育/社区": ("hupu",),
    "体育": ("dongqiudi",),
    "AI": ("aihot",),
    "社区": (
        "tieba",
        "chongbuluo-latest",
        "chongbuluo-hot",
        "hupu",
        "v2ex-share",
        "coolapk",
    ),
    "科技": ("ithome", "solidot", "sspai"),
    "国际新闻": ("sputniknewscn", "cankaoxiaoxi"),
    "金融/投资": ("xueqiu-hotstock",),
    "海外科技社区": ("hackernews",),
    "产品/创业": ("producthunt",),
    "开源": ("github-trending-today",),
    "视频/社媒": ("bilibili-hot-search",),
    "新闻聚合": ("kaopu",),
    "搜索": ("baidu",),
    "求职/社区": ("nowcoder",),
    "开发者社区": ("juejin",),
    "影视": ("douban",),
    "游戏": ("steam",),
    "网络安全": ("freebuf",),
    "视频/影视": ("qqvideo-tv-hotsearch", "iqiyi-hot-ranklist"),
}

_write_lock = threading.Lock()


def list_newsnow_groups() -> dict[str, tuple[str, ...]]:
    """Return a copy of the configured category-to-source mapping."""
    return dict(NEWSNOW_GROUPS)


def _normalize_groups(groups: str | Iterable[str]) -> tuple[str, ...]:
    values = (groups,) if isinstance(groups, str) else groups
    normalized: list[str] = []
    for value in values:
        name = str(value).strip()
        if not name:
            continue
        if name not in NEWSNOW_GROUPS:
            valid = "、".join(NEWSNOW_GROUPS)
            raise ValueError(f"未知 NewsNow 类别 {name!r}；可用类别：{valid}")
        if name not in normalized:
            normalized.append(name)
    if not normalized:
        raise ValueError("至少需要指定一个 NewsNow 类别")
    return tuple(normalized)


def _normalize_base_url(configured: Optional[str]) -> str:
    base_url = (
        configured
        or os.environ.get(NEWSNOW_BASE_URL_ENV, "").strip()
        or DEFAULT_NEWSNOW_BASE_URL
    ).rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("NewsNow base_url 必须是有效的 http(s) 地址")
    if parsed.query or parsed.fragment:
        raise ValueError("NewsNow base_url 不能包含查询参数或 fragment")
    return base_url


def _output_dir(configured: Optional[str | Path]) -> Path:
    if configured is not None:
        return Path(configured).expanduser().resolve()
    return (Path.cwd() / "outputs" / "hotlists").resolve()


def _valid_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return url
    return ""


def _normalize_item(
    raw: Any,
    *,
    source_id: str,
    category: str,
    rank: int,
) -> Optional[dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return None
    title = str(raw.get("title") or "").strip()
    url = _valid_url(raw.get("url")) or _valid_url(raw.get("mobileUrl"))
    if not title or not url:
        return None

    item: dict[str, Any] = {
        "source_id": source_id,
        "category": category,
        "rank": rank,
        "id": str(raw.get("id") or ""),
        "title": title,
        "url": url,
        "published_at": raw.get("pubDate"),
    }
    mobile_url = _valid_url(raw.get("mobileUrl"))
    if mobile_url and mobile_url != url:
        item["mobile_url"] = mobile_url
    if isinstance(raw.get("extra"), Mapping):
        item["extra"] = dict(raw["extra"])
    return item


def _url_dedupe_key(url: str) -> str:
    """Normalize URL identity without dropping potentially meaningful queries."""
    parsed = urlsplit(url.strip())
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            parsed.query,
            "",
        )
    )


def _title_dedupe_key(title: str) -> str:
    """Normalize harmless Unicode, casing, and whitespace title differences."""
    normalized = unicodedata.normalize("NFKC", title)
    return " ".join(normalized.split()).casefold()


def _deduplicate_source_items(source_results: Mapping[str, dict[str, Any]]) -> int:
    """Deduplicate globally, keeping the first item in source/rank order."""
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    duplicate_total = 0

    for source_result in source_results.values():
        unique_items: list[dict[str, Any]] = []
        duplicate_items = 0
        for item in source_result["items"]:
            url_key = _url_dedupe_key(item["url"])
            title_key = _title_dedupe_key(item["title"])
            if url_key in seen_urls or title_key in seen_titles:
                duplicate_items += 1
                continue
            seen_urls.add(url_key)
            seen_titles.add(title_key)
            unique_items.append(item)

        source_result["items"] = unique_items
        source_result["item_count"] = len(unique_items)
        source_result["duplicate_items"] = duplicate_items
        duplicate_total += duplicate_items

    return duplicate_total


def _snapshot_filename(groups: tuple[str, ...], recorded_at: datetime) -> str:
    group_slug = "_".join(groups)
    group_slug = re.sub(r"[^\w.-]+", "_", group_slug, flags=re.UNICODE)
    group_slug = group_slug.strip("._")[:80] or "hotlists"
    timestamp = recorded_at.strftime("%Y%m%d-%H%M%S-%f%z")
    return f"{timestamp}_newsnow_{group_slug}.json"


def render_newsnow_titles(result: Mapping[str, Any]) -> str:
    """Render one deduplicated title per line without any metadata."""
    titles: list[str] = []
    sources = result.get("sources")
    if isinstance(sources, Mapping):
        for source in sources.values():
            if not isinstance(source, Mapping):
                continue
            for item in source.get("items") or []:
                if not isinstance(item, Mapping):
                    continue
                title = " ".join(str(item.get("title") or "").split())
                if title:
                    titles.append(title)
    return "\n".join(titles) + ("\n" if titles else "")


def _save_snapshot(
    result: dict[str, Any],
    groups: tuple[str, ...],
    *,
    output_dir: Optional[str | Path],
) -> tuple[Path, Path]:
    destination = _output_dir(output_dir)
    snapshot_path = destination / _snapshot_filename(
        groups, datetime.now().astimezone()
    )
    title_path = snapshot_path.with_suffix(".txt")
    result["snapshot_file"] = str(snapshot_path)
    temporary = snapshot_path.with_suffix(f"{snapshot_path.suffix}.tmp")
    title_temporary = title_path.with_suffix(f"{title_path.suffix}.tmp")
    serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    title_text = render_newsnow_titles(result)
    with _write_lock:
        destination.mkdir(parents=True, exist_ok=True)
        temporary.write_text(serialized, encoding="utf-8")
        title_temporary.write_text(title_text, encoding="utf-8")
        temporary.replace(snapshot_path)
        title_temporary.replace(title_path)
    return snapshot_path, title_path


def fetch_newsnow_hotlists(
    groups: str | Iterable[str] = "综合",
    *,
    base_url: Optional[str] = None,
    latest: bool = False,
    per_source_limit: Optional[int] = None,
    timeout: int = 15,
    output_dir: Optional[str | Path] = None,
    save: bool = True,
) -> dict[str, Any]:
    """Fetch NewsNow boards selected by the documented category and optionally save them.

    Args:
        groups: One category or an iterable of categories from
            ``NEWSNOW_GROUPS``. Defaults to the cross-platform ``综合`` group.
        base_url: NewsNow deployment URL. Defaults to
            ``AGENTSCROLL_NEWSNOW_BASE_URL`` or http://127.0.0.1:4444.
        latest: Pass ``latest=true`` to NewsNow. NewsNow may still apply its
            own upstream refresh interval and cache policy.
        per_source_limit: Optional local item limit for each source. By default
            all items returned by NewsNow are retained.
        timeout: Per-source HTTP timeout in seconds.
        output_dir: Snapshot directory. Defaults to ``./outputs/hotlists``.
        save: Save paired full JSON and title-only text snapshots when true.

    Returns:
        A JSON-serializable dictionary. Each requested source has independent
        ``items`` and ``error`` fields so one upstream failure does not discard
        successful hot lists.
    """
    selected_groups = _normalize_groups(groups)
    normalized_base_url = _normalize_base_url(base_url)
    if per_source_limit is not None and per_source_limit <= 0:
        raise ValueError("per_source_limit 必须大于 0")
    if timeout <= 0:
        raise ValueError("timeout 必须大于 0")

    group_sources = {name: list(NEWSNOW_GROUPS[name]) for name in selected_groups}
    source_categories: dict[str, str] = {}
    for category, source_ids in group_sources.items():
        for source_id in source_ids:
            source_categories.setdefault(source_id, category)

    source_results: dict[str, dict[str, Any]] = {}
    for source_id, category in source_categories.items():
        params: dict[str, str] = {"id": source_id}
        if latest:
            params["latest"] = "true"
        url = f"{normalized_base_url}/api/s?{urlencode(params)}"
        source_result: dict[str, Any] = {
            "category": category,
            "status": "error",
            "updated_time": None,
            "item_count": 0,
            "discarded_items": 0,
            "duplicate_items": 0,
            "items": [],
            "error": None,
        }
        try:
            payload = get(url, timeout=timeout, retries=1)
            if not isinstance(payload, Mapping):
                raise ValueError("NewsNow 返回值不是 JSON object")
            upstream_status = str(payload.get("status") or "").strip()
            raw_items = payload.get("items")
            if upstream_status not in {"success", "cache"}:
                raise ValueError(
                    f"NewsNow 返回异常状态：{upstream_status or 'missing'}"
                )
            if not isinstance(raw_items, list):
                raise ValueError("NewsNow items 不是数组")

            selected_items = raw_items
            if per_source_limit is not None:
                selected_items = raw_items[:per_source_limit]
            normalized_items = [
                item
                for rank, raw in enumerate(selected_items, start=1)
                if (
                    item := _normalize_item(
                        raw,
                        source_id=source_id,
                        category=category,
                        rank=rank,
                    )
                )
                is not None
            ]
            if not normalized_items:
                raise ValueError("NewsNow 返回 0 条有效热榜数据")

            source_result.update(
                {
                    "status": upstream_status,
                    "updated_time": payload.get("updatedTime"),
                    "item_count": len(normalized_items),
                    "discarded_items": len(selected_items) - len(normalized_items),
                    "items": normalized_items,
                }
            )
        except Exception as exc:
            source_result["error"] = f"{type(exc).__name__}: {exc}"
        source_results[source_id] = source_result

    duplicate_items = _deduplicate_source_items(source_results)
    total_items = sum(source["item_count"] for source in source_results.values())

    result: dict[str, Any] = {
        "provider": "newsnow",
        "base_url": normalized_base_url,
        "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "requested_groups": list(selected_groups),
        "group_sources": group_sources,
        "latest": latest,
        "per_source_limit": per_source_limit,
        "total_items": total_items,
        "duplicate_items": duplicate_items,
        "sources": source_results,
    }
    if save:
        _, title_path = _save_snapshot(result, selected_groups, output_dir=output_dir)
        result["snapshot_text_file"] = str(title_path)
    return result


def _parse_group_argument(value: str) -> tuple[str, ...]:
    return _normalize_groups(value.split(","))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="按类别调用 NewsNow 热榜")
    parser.add_argument(
        "--groups",
        type=_parse_group_argument,
        default=("综合",),
        help="逗号分隔的类别，默认：综合",
    )
    parser.add_argument("--base-url", help="NewsNow 部署地址")
    parser.add_argument(
        "--latest", action="store_true", help="向 NewsNow 传 latest=true"
    )
    parser.add_argument("--per-source-limit", type=int, help="每个榜单最多保留多少条")
    parser.add_argument("--timeout", type=int, default=15, help="单榜单超时秒数")
    parser.add_argument("--output-dir", help="热榜 JSON 快照目录")
    parser.add_argument("--no-save", action="store_true", help="不保存本地快照")
    parser.add_argument(
        "--list-groups", action="store_true", help="列出所有类别及 Source ID"
    )
    args = parser.parse_args(argv)

    if args.list_groups:
        print(json.dumps(list_newsnow_groups(), ensure_ascii=False, indent=2))
        return 0
    try:
        result = fetch_newsnow_hotlists(
            args.groups,
            base_url=args.base_url,
            latest=args.latest,
            per_source_limit=args.per_source_limit,
            timeout=args.timeout,
            output_dir=args.output_dir,
            save=not args.no_save,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["total_items"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
