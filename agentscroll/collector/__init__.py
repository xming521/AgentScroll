"""Standalone data collection layer for AgentScroll."""

from typing import Any

from .search import ALL_SOURCES, collect
from .routing import SCENE_SOURCES, route_topic


def fetch_newsnow_hotlists(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Call the NewsNow integration without loading it during package startup."""
    from .newsnow import fetch_newsnow_hotlists as fetch

    return fetch(*args, **kwargs)


def list_newsnow_groups() -> dict[str, tuple[str, ...]]:
    """Return the configured NewsNow category groups."""
    from .newsnow import list_newsnow_groups as list_groups

    return list_groups()


def open_selected_weibo_hotlists(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Resolve model-selected NewsNow Weibo titles without changing snapshots."""
    from .hotlist import open_selected_weibo_hotlists as open_selected

    return open_selected(*args, **kwargs)


def collect_selected_hotlist_evidence(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Collect compact evidence for first-pass selected hot-list topics."""
    from .hotlist import collect_selected_hotlist_evidence as collect_evidence

    return collect_evidence(*args, **kwargs)


__all__ = [
    "ALL_SOURCES",
    "SCENE_SOURCES",
    "collect",
    "collect_selected_hotlist_evidence",
    "fetch_newsnow_hotlists",
    "list_newsnow_groups",
    "open_selected_weibo_hotlists",
    "route_topic",
]
