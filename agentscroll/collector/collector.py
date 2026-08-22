"""Unified entry point for collecting raw data from Chinese platforms."""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .knowledge_store import save_knowledge_document
from .routing import SCENE_SOURCES, normalize_scene, route_topic
from .sources import (
    bilibili,
    dates,
    douyin,
    toutiao,
    wechat,
    weibo,
    xiaohongshu,
    zhihu,
)

ALL_SOURCES = (
    "weibo",
    "xiaohongshu",
    "bilibili",
    "zhihu",
    "douyin",
    "wechat",
    "toutiao",
)

_ALIASES = {"xhs": "xiaohongshu"}


def _normalize_sources(sources: Optional[Iterable[str]]) -> tuple[str, ...]:
    if sources is None:
        return ALL_SOURCES

    normalized = []
    for source in sources:
        name = _ALIASES.get(source.strip().lower(), source.strip().lower())
        if not name:
            continue
        if name not in ALL_SOURCES:
            valid = ", ".join(ALL_SOURCES)
            raise ValueError(f"未知数据源 {source!r}；可用数据源：{valid}")
        if name not in normalized:
            normalized.append(name)

    if not normalized:
        raise ValueError("至少需要指定一个数据源")
    return tuple(normalized)


def _collect_one(
    source: str,
    topic: str,
    from_date: str,
    to_date: str,
    depth: str,
) -> list[dict[str, Any]]:
    if source == "weibo":
        return weibo.search_weibo(
            topic,
            from_date,
            to_date,
            depth=depth,
        )
    if source == "xiaohongshu":
        return xiaohongshu.search_xiaohongshu(
            topic,
            from_date,
            to_date,
            depth=depth,
        )
    if source == "bilibili":
        return bilibili.search_bilibili(topic, from_date, to_date, depth=depth)
    if source == "zhihu":
        return zhihu.search_zhihu(
            topic,
            from_date,
            to_date,
            depth=depth,
        )
    if source == "douyin":
        return douyin.search_douyin(
            topic,
            from_date,
            to_date,
            depth=depth,
        )
    if source == "wechat":
        return wechat.search_wechat(
            topic,
            from_date,
            to_date,
            depth=depth,
        )
    if source == "toutiao":
        return toutiao.search_toutiao(topic, from_date, to_date, depth=depth)
    raise ValueError(f"不支持的数据源：{source}")


def _filter_by_date(
    items: list[dict[str, Any]],
    from_date: str,
    to_date: str,
) -> list[dict[str, Any]]:
    """Drop dated items outside the window and retain items with unknown dates."""
    filtered = []
    for item in items:
        item_date = item.get("date")
        if item_date is None or from_date <= item_date <= to_date:
            filtered.append(item)
    return filtered


def collect(
    topic: str,
    *,
    sources: Optional[Iterable[str]] = None,
    scene: str = "auto",
    days: int = 30,
    as_of: Optional[str] = None,
    depth: str = "default",
    output_dir: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """Collect platform data and persist one compact knowledge document.

    Args:
        topic: Search topic or keywords.
        sources: Platform names. Explicit values override scene routing;
            ``xhs`` is an alias.
        scene: ``auto``, ``formal``, or ``informal``. Used to select sources
            only when ``sources`` is omitted.
        days: Number of calendar days to look back.
        as_of: Optional end date in YYYY-MM-DD format.
        depth: Per-platform candidate limit: 5 for ``quick``, 10 for
            ``default``, and 20 for ``deep``. Some sources also expand their
            search paths at higher depths.
        output_dir: Directory for the compact knowledge file. Defaults to
            ``./outputs/knowledge``.

    Returns:
        A JSON-serializable dictionary containing per-source items and errors.
    """
    topic = topic.strip()
    if not topic:
        raise ValueError("topic 不能为空")
    if days <= 0:
        raise ValueError("days 必须大于 0")
    if depth not in {"quick", "default", "deep"}:
        raise ValueError("depth 必须是 quick、default 或 deep")

    requested_scene = normalize_scene(scene)
    routing = route_topic(topic, requested_scene)
    if sources is None:
        active_sources = SCENE_SOURCES[routing["resolved_scene"]]
        routing["selection"] = "scene-route"
    else:
        active_sources = _normalize_sources(sources)
        routing["selection"] = "explicit-sources"
    routing["sources"] = list(active_sources)

    from_date, to_date = dates.get_date_range(days, as_of=as_of)
    source_results: Dict[str, Dict[str, Any]] = {
        source: {"items": [], "error": None} for source in active_sources
    }

    with ThreadPoolExecutor(max_workers=len(active_sources)) as executor:
        futures = {
            executor.submit(
                _collect_one,
                source,
                topic,
                from_date,
                to_date,
                depth,
            ): source
            for source in active_sources
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                items = future.result()
                source_results[source]["items"] = _filter_by_date(
                    items, from_date, to_date
                )
            except Exception as exc:
                source_results[source]["error"] = f"{type(exc).__name__}: {exc}"

    result = {
        "topic": topic,
        "from_date": from_date,
        "to_date": to_date,
        "depth": depth,
        "routing": routing,
        "sources": source_results,
    }
    knowledge_file = save_knowledge_document(result, output_dir=output_dir)
    result["knowledge_file"] = str(knowledge_file)
    result["knowledge_metadata_file"] = str(knowledge_file.with_suffix(".json"))
    return result


def _parse_source_argument(value: str) -> tuple[str, ...]:
    return _normalize_sources(value.split(","))


def _parse_scene_argument(value: str) -> str:
    return normalize_scene(value)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AgentScroll 独立数据采集器")
    parser.add_argument("topic", help="要搜索的话题或关键词")
    parser.add_argument(
        "--sources",
        type=_parse_source_argument,
        default=None,
        help="逗号分隔的数据源；显式指定后优先于场景路由",
    )
    parser.add_argument(
        "--scene",
        type=_parse_scene_argument,
        default="auto",
        help="搜索场景：auto、formal（正式）或 informal（非正式）",
    )
    parser.add_argument("--days", type=int, default=30, help="回溯天数，默认 30")
    parser.add_argument("--as-of", help="查询终点日期，格式为 YYYY-MM-DD")
    parser.add_argument(
        "--depth",
        choices=("quick", "default", "deep"),
        default="default",
        help="采集深度",
    )
    parser.add_argument(
        "--output-dir",
        help="聚合知识文件目录；默认保存到 ./outputs/knowledge",
    )
    args = parser.parse_args(argv)

    try:
        result = collect(
            args.topic,
            sources=args.sources,
            scene=args.scene,
            days=args.days,
            as_of=args.as_of,
            depth=args.depth,
            output_dir=args.output_dir,
        )
    except ValueError as exc:
        parser.error(str(exc))

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0
