"""Route a search topic to formal or informal content sources.

The scene names describe the expected content style, not source credibility:
``formal`` favors long-form/news-style material, while ``informal`` favors
platform-native conversation, memes, and lived usage.
"""

from __future__ import annotations

import re
from typing import Any, Literal


Scene = Literal["formal", "informal"]
RequestedScene = Literal["auto", "formal", "informal"]

SCENE_SOURCES: dict[Scene, tuple[str, ...]] = {
    "formal": ("zhihu", "wechat", "toutiao"),
    "informal": ("weibo", "bilibili", "douyin"),
}

# These are intent signals rather than topic-specific entities. In particular,
# no current meme, company, person, or event name is encoded here.
_FORMAL_SIGNALS = (
    "官方",
    "公告",
    "声明",
    "通报",
    "政策",
    "法规",
    "法律",
    "监管",
    "标准",
    "规范",
    "报告",
    "研究",
    "论文",
    "数据",
    "统计",
    "行业",
    "财报",
    "新闻",
    "事件",
    "进展",
    "回应",
    "调查",
    "事实核查",
    "分析",
    "解读",
    "趋势",
    "预测",
    "official",
    "announcement",
    "policy",
    "regulation",
    "report",
    "research",
    "paper",
    "statistics",
    "news",
    "analysis",
)

_INFORMAL_SIGNALS = (
    "梗",
    "热梗",
    "玩梗",
    "网络用语",
    "流行语",
    "热词",
    "黑话",
    "什么意思",
    "啥意思",
    "笑点",
    "段子",
    "吐槽",
    "表情包",
    "评论区",
    "网友",
    "二创",
    "鬼畜",
    "整活",
    "名场面",
    "爆火",
    "火了",
    "meme",
    "slang",
    "joke",
    "viral",
)

_SCENE_ALIASES: dict[str, RequestedScene] = {
    "auto": "auto",
    "formal": "formal",
    "informal": "informal",
    "正式": "formal",
    "非正式": "informal",
}


def normalize_scene(scene: str) -> RequestedScene:
    """Normalize English or Chinese scene names."""
    normalized = _SCENE_ALIASES.get(str(scene or "").strip().lower())
    if normalized is None:
        valid = "auto、formal（正式）、informal（非正式）"
        raise ValueError(f"未知场景 {scene!r}；可用场景：{valid}")
    return normalized


def _contains_signal(topic: str, signal: str) -> bool:
    if signal.isascii():
        return bool(
            re.search(
                rf"(?<![a-z0-9]){re.escape(signal)}(?![a-z0-9])",
                topic,
                flags=re.I,
            )
        )
    return signal in topic


def _matched_signals(topic: str, candidates: tuple[str, ...]) -> list[str]:
    return [signal for signal in candidates if _contains_signal(topic, signal)]


def route_topic(topic: str, scene: str = "auto") -> dict[str, Any]:
    """Resolve one query to a scene and return an auditable decision."""
    requested = normalize_scene(scene)
    if requested != "auto":
        return {
            "requested_scene": requested,
            "resolved_scene": requested,
            "reason": "explicit-scene",
            "signals": [],
        }

    formal_hits = _matched_signals(topic, _FORMAL_SIGNALS)
    informal_hits = _matched_signals(topic, _INFORMAL_SIGNALS)
    if formal_hits or informal_hits:
        # More matching intent signals win. A tie favors informal material,
        # because it preserves native usage for mixed questions about a meme.
        resolved: Scene = (
            "formal" if len(formal_hits) > len(informal_hits) else "informal"
        )
        return {
            "requested_scene": requested,
            "resolved_scene": resolved,
            "reason": "intent-signals",
            "signals": formal_hits if resolved == "formal" else informal_hits,
        }

    compact_topic = re.sub(r"\s+", "", topic)
    resolved = "informal" if len(compact_topic) <= 12 else "formal"
    return {
        "requested_scene": requested,
        "resolved_scene": resolved,
        "reason": (
            "short-topic-discovery"
            if resolved == "informal"
            else "long-query-default"
        ),
        "signals": [],
    }
