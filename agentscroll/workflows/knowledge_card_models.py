"""Typed model contracts and contextual validation for knowledge cards."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    SkipValidation,
    WithJsonSchema,
    field_validator,
    model_validator,
)

CardStatus = Literal["complete", "needs_research", "rejected"]
CARD_STATUSES = ("complete", "needs_research", "rejected")

_LABEL_ALIASES = {
    "news": "news",
    "breaking": "news",
    "major": "news",
    "fun": "fun",
    "meme": "fun",
}
REMOVED_LABELS = frozenset({"conversation", "discussion"})
_SHARE_LABELS = frozenset({"news", "fun"})
_SHARE_SCORE_THRESHOLD = 3


def _limited_text(max_length: int) -> Any:
    return Annotated[
        str,
        WithJsonSchema({"type": "string", "maxLength": max_length}),
    ]


Text20 = _limited_text(20)
Text40 = _limited_text(40)
Text60 = _limited_text(60)
Text80 = _limited_text(80)
Text120 = _limited_text(120)
Text180 = _limited_text(180)
Text260 = _limited_text(260)
ShareScore = Annotated[
    SkipValidation[Any],
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "number", "const": 0},
                {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 4,
                    "multipleOf": 0.1,
                },
            ]
        }
    ),
]
InterestShareScore = Annotated[
    SkipValidation[Any],
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "number", "const": 0},
                {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 3.9,
                    "multipleOf": 0.1,
                },
            ]
        }
    ),
]
class ShareDraft(BaseModel):
    """Share fields returned directly by the model."""

    model_config = ConfigDict(extra="forbid")

    text: Text180
    source_id: Text40
    comment_id: Text60
    generated_comment: Text80

    @field_validator(
        "text",
        "source_id",
        "comment_id",
        "generated_comment",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return str(value or "").strip()


class KnowledgeCardDraft(BaseModel):
    """One untrusted knowledge card returned by the model."""

    model_config = ConfigDict(extra="forbid")

    status: CardStatus
    rejection_reason: Text120
    knowledge: Text260
    chat_context: Text180
    latest_update: Text260 | None
    general_share_score: ShareScore
    interest_share_score: InterestShareScore
    share: SkipValidation[ShareDraft | None]

    @field_validator("rejection_reason", "knowledge", "chat_context", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("latest_update", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: Any) -> str | None:
        return str(value).strip() if value is not None else None


class ResearchKnowledgeCardDraft(KnowledgeCardDraft):
    """Knowledge-card response that also selects research sources."""

    research_sources: Annotated[
        SkipValidation[list[Text20]],
        WithJsonSchema(
            {
                "type": "array",
                "maxItems": 3,
                "items": {"type": "string", "maxLength": 20},
            }
        ),
    ]


class KnowledgeCardResponse(BaseModel):
    """Guided-decoding response for one ordinary topic."""

    model_config = ConfigDict(extra="forbid")

    cards: list[KnowledgeCardDraft]


class ResearchKnowledgeCardResponse(BaseModel):
    """Guided-decoding response for one supplemented topic."""

    model_config = ConfigDict(extra="forbid")

    cards: list[ResearchKnowledgeCardDraft]


class KnowledgeShare(BaseModel):
    """Evidence-backed share selected from a validated card."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    source_id: str
    url: str
    comment_id: str
    comment_type: Literal["platform", "generated"]
    comment: str


class ResearchSource(BaseModel):
    """Source retained as provenance for a supplemented card."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    url: str


class KnowledgeCard(BaseModel):
    """Normalized knowledge card used inside the workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: CardStatus
    rejection_reason: str
    knowledge: str
    chat_context: str
    latest_update: str | None
    share_score: int | float
    general_share_score: int | float
    interest_share_score: int | float = 0
    candidate_interest_keywords: tuple[str, ...] = ()
    share: KnowledgeShare | None
    topic_id: int
    research_sources: tuple[ResearchSource, ...] | None = None

    @model_validator(mode="before")
    @classmethod
    def default_score_components(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        normalized = dict(value)
        normalized.setdefault(
            "general_share_score", normalized.get("share_score", 0)
        )
        return normalized

    @model_validator(mode="after")
    def validate_final_share_score(self) -> KnowledgeCard:
        expected = max(self.general_share_score, self.interest_share_score)
        if self.share_score != expected:
            raise ValueError("share_score 必须等于大众分与兴趣分的较高值")
        return self

    @classmethod
    def needs_research(
        cls,
        topic_id: int,
        *,
        research: bool = False,
        candidate_interest_keywords: tuple[str, ...] = (),
    ) -> KnowledgeCard:
        return cls(
            status="needs_research",
            rejection_reason="",
            knowledge="",
            chat_context="",
            latest_update=None,
            share_score=0,
            general_share_score=0,
            interest_share_score=0,
            candidate_interest_keywords=candidate_interest_keywords,
            share=None,
            topic_id=topic_id,
            research_sources=() if research else None,
        )

    def to_dict(self) -> dict[str, Any]:
        exclude = set()
        if self.research_sources is None:
            exclude.add("research_sources")
        if not self.candidate_interest_keywords:
            exclude.add("candidate_interest_keywords")
        return self.model_dump(mode="json", exclude=exclude or None)


def http_url(value: Any) -> str:
    url = str(value or "").strip()
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return url
    return ""


def normalize_label(value: Any) -> str:
    raw_label = str(value or "")
    label = _LABEL_ALIASES.get(raw_label)
    if label is None:
        raise ValueError(f"无效的 label：{raw_label!r}")
    return label


def _inline_schema_refs(value: Any, definitions: Mapping[str, Any]) -> Any:
    if isinstance(value, list):
        return [_inline_schema_refs(item, definitions) for item in value]
    if not isinstance(value, dict):
        return value

    reference = value.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        name = reference.removeprefix("#/$defs/")
        resolved = _inline_schema_refs(deepcopy(definitions[name]), definitions)
        siblings = {
            key: _inline_schema_refs(item, definitions)
            for key, item in value.items()
            if key != "$ref" and key not in {"title", "description"}
        }
        return {**resolved, **siblings}

    return {
        key: _inline_schema_refs(item, definitions)
        for key, item in value.items()
        if key not in {"$defs", "description"}
        and not (key == "title" and isinstance(item, str))
    }


def card_response_schema(*, research: bool) -> dict[str, Any]:
    model = ResearchKnowledgeCardResponse if research else KnowledgeCardResponse
    schema = model.model_json_schema()
    definitions = schema.get("$defs") or {}
    normalized = _inline_schema_refs(schema, definitions)
    cards_schema = normalized["properties"]["cards"]
    cards_schema["minItems"] = 1
    cards_schema["maxItems"] = 1
    return normalized


def _share_draft(raw_share: Any) -> ShareDraft | None:
    if not isinstance(raw_share, Mapping):
        return None
    generated_comment = str(raw_share.get("generated_comment") or "").strip()
    if not generated_comment and raw_share.get("comment_type") == "generated":
        generated_comment = str(raw_share.get("comment") or "").strip()
    return ShareDraft.model_validate(
        {
            "text": raw_share.get("text"),
            "source_id": raw_share.get("source_id"),
            "comment_id": raw_share.get("comment_id"),
            "generated_comment": generated_comment,
        }
    )


def _validate_share(
    raw_share: Any,
    *,
    score: Any,
    status: str,
    topic: Mapping[str, Any],
) -> KnowledgeShare | None:
    topic_id = topic["topic_id"]
    valid_range = isinstance(score, (int, float)) and (
        score == 0 or 1 <= score <= 4
    )
    has_at_most_one_decimal = valid_range and abs(
        score * 10 - round(score * 10)
    ) < 1e-9
    if isinstance(score, bool) or not has_at_most_one_decimal:
        raise ValueError(
            f"话题 {topic_id} 的 share_score 必须为 0 或 1 至 4 "
            "且最多保留一位小数"
        )
    ready = score >= _SHARE_SCORE_THRESHOLD

    sources = [
        *list(topic.get("evidence") or []),
        *list(topic.get("research_evidence") or []),
    ]
    has_share_source = any(
        isinstance(item, Mapping)
        and item.get("source_id")
        and http_url(item.get("url"))
        for item in sources
    )
    if status != "complete" and score != 0:
        raise ValueError(f"非完整话题 {topic_id} 的 share_score 必须为 0")
    if not has_share_source and score != 0:
        raise ValueError(f"话题 {topic_id} 没有可用分享来源，share_score 必须为 0")

    if not ready:
        if raw_share is not None:
            raise ValueError(
                f"话题 {topic_id} 的 share_score 低于 "
                f"{_SHARE_SCORE_THRESHOLD} 时 share 必须为 null"
            )
        return None

    if not isinstance(raw_share, Mapping):
        raise ValueError(f"话题 {topic_id} 的高分 share 缺少有效对象")
    share = _share_draft(raw_share)
    assert share is not None

    if status != "complete":
        raise ValueError(f"待补搜话题 {topic_id} 不能进入即时分享队列")
    if topic.get("label") not in _SHARE_LABELS:
        raise ValueError(f"话题 {topic_id} 的 label 不允许即时分享")
    if not share.text or not share.source_id:
        raise ValueError(f"可分享话题 {topic_id} 缺少分享内容或来源")
    if bool(share.comment_id) == bool(share.generated_comment):
        raise ValueError(f"可分享话题 {topic_id} 必须且只能选择真实评论或自拟评论之一")

    source = next(
        (
            item
            for item in sources
            if isinstance(item, Mapping)
            and item.get("source_id") == share.source_id
        ),
        None,
    )
    if source is None:
        raise ValueError(
            f"话题 {topic_id} 返回了未知 source_id：{share.source_id!r}"
        )
    url = http_url(source.get("url"))
    if not url:
        raise ValueError(f"话题 {topic_id} 选择的分享来源没有有效 URL")

    if share.comment_id:
        comments = {
            str(comment.get("comment_id") or ""): str(comment.get("text") or "").strip()
            for item in sources
            if isinstance(item, Mapping)
            for comment in item.get("comments") or []
            if isinstance(comment, Mapping) and comment.get("comment_id")
        }
        comment = comments.get(share.comment_id, "")
        if not comment:
            raise ValueError(
                f"话题 {topic_id} 返回了未知 comment_id：{share.comment_id!r}"
            )
        comment_type = "platform"
    else:
        comment = share.generated_comment
        comment_type = "generated"

    return KnowledgeShare(
        text=share.text,
        source_id=share.source_id,
        url=url,
        comment_id=share.comment_id,
        comment_type=comment_type,
        comment=comment,
    )


def _validated_score(
    value: Any,
    *,
    topic_id: int,
    field: str,
    maximum: float,
) -> int | float:
    valid_range = isinstance(value, (int, float)) and (
        value == 0 or 1 <= value <= maximum
    )
    has_at_most_one_decimal = valid_range and abs(
        value * 10 - round(value * 10)
    ) < 1e-9
    if isinstance(value, bool) or not has_at_most_one_decimal:
        raise ValueError(
            f"话题 {topic_id} 的 {field} 必须为 0 或 1 至 {maximum:g} "
            "且最多保留一位小数"
        )
    return value


def _topic_interest_keywords(
    topic: Mapping[str, Any], *, topic_id: int
) -> tuple[str, ...]:
    value = topic.get("candidate_interest_keywords") or []
    if not isinstance(value, list) or any(
        not isinstance(keyword, str) for keyword in value
    ):
        raise ValueError(
            f"话题 {topic_id} 的 candidate_interest_keywords 必须是字符串数组"
        )
    normalized = tuple(keyword.strip() for keyword in value)
    if any(not keyword for keyword in normalized) or len(normalized) != len(
        set(normalized)
    ):
        raise ValueError(
            f"话题 {topic_id} 的 candidate_interest_keywords 包含空值或重复值"
        )
    return normalized


def _draft_from_mapping(
    raw_card: Mapping[str, Any],
    *,
    topic_id: int,
    research: bool,
) -> KnowledgeCardDraft:
    uses_score_components = any(
        field in raw_card
        for field in (
            "general_share_score",
            "interest_share_score",
        )
    )
    payload = {
        "status": raw_card.get("status"),
        "rejection_reason": raw_card.get("rejection_reason"),
        "knowledge": raw_card.get("knowledge"),
        "chat_context": raw_card.get("chat_context"),
        "latest_update": raw_card.get("latest_update"),
        "general_share_score": (
            raw_card.get("general_share_score")
            if uses_score_components
            else raw_card.get("share_score")
        ),
        "interest_share_score": (
            raw_card.get("interest_share_score") if uses_score_components else 0
        ),
        "share": raw_card.get("share"),
    }
    if research:
        raw_sources = raw_card.get("research_sources")
        if not isinstance(raw_sources, list):
            raise TypeError(f"补搜知识卡 {topic_id} 缺少 research_sources 数组")
        payload["research_sources"] = raw_sources
        return ResearchKnowledgeCardDraft.model_validate(payload)
    return KnowledgeCardDraft.model_validate(payload)


def _validate_draft(
    draft: KnowledgeCardDraft,
    *,
    topic_id: int,
    topic: Mapping[str, Any],
    research: bool,
) -> KnowledgeCard:
    status = draft.status
    relation = str(topic.get("event_relation") or "new")
    general_share_score = _validated_score(
        draft.general_share_score,
        topic_id=topic_id,
        field="general_share_score",
        maximum=4,
    )
    interest_share_score = _validated_score(
        draft.interest_share_score,
        topic_id=topic_id,
        field="interest_share_score",
        maximum=3.9,
    )
    candidate_interest_keywords = _topic_interest_keywords(
        topic, topic_id=topic_id
    )
    if interest_share_score > 0 and not candidate_interest_keywords:
        raise ValueError(
            f"话题 {topic_id} 的 interest_share_score 大于 0 时必须有候选兴趣关键词"
        )
    share_score = max(general_share_score, interest_share_score)
    if status == "complete":
        if not draft.knowledge or not draft.chat_context or draft.rejection_reason:
            raise ValueError(f"完整知识卡 {topic_id} 的字段状态不一致")
        if relation == "update" and not draft.latest_update:
            raise ValueError(f"更新知识卡 {topic_id} 缺少 latest_update")
        if relation == "new" and draft.latest_update is not None:
            raise ValueError(f"新知识卡 {topic_id} 的 latest_update 必须为 null")
    elif status == "needs_research":
        if (
            draft.rejection_reason
            or draft.knowledge
            or draft.chat_context
            or draft.latest_update is not None
        ):
            raise ValueError(f"待补搜知识卡 {topic_id} 的字段状态不一致")
    elif (
        not draft.rejection_reason
        or draft.knowledge
        or draft.chat_context
        or draft.latest_update is not None
    ):
        raise ValueError(f"已淘汰知识卡 {topic_id} 的字段状态不一致")

    share = _validate_share(
        draft.share,
        score=share_score,
        status=status,
        topic=topic,
    )
    research_sources: tuple[ResearchSource, ...] | None = None
    if research:
        assert isinstance(draft, ResearchKnowledgeCardDraft)
        available_sources = {
            str(source.get("source_id") or ""): source
            for source in [
                *list(topic.get("evidence") or []),
                *list(topic.get("research_evidence") or []),
            ]
            if isinstance(source, Mapping) and source.get("source_id")
        }
        normalized_sources = []
        for source_id in draft.research_sources:
            source = available_sources.get(str(source_id))
            if source is None:
                raise ValueError(
                    f"补搜知识卡 {topic_id} 返回了未知来源：{source_id!r}"
                )
            url = http_url(source.get("url"))
            if not url:
                raise ValueError(
                    f"补搜知识卡 {topic_id} 选择的来源没有有效 URL"
                )
            title = str(
                source.get("source_title")
                or source.get("title")
                or source.get("platform")
                or source_id
            ).strip()
            normalized_sources.append(ResearchSource(title=title, url=url))
        research_sources = tuple(normalized_sources)

    return KnowledgeCard(
        status=status,
        rejection_reason=draft.rejection_reason,
        knowledge=draft.knowledge,
        chat_context=draft.chat_context,
        latest_update=draft.latest_update,
        share_score=share_score,
        general_share_score=general_share_score,
        interest_share_score=interest_share_score,
        candidate_interest_keywords=candidate_interest_keywords,
        share=share,
        topic_id=topic_id,
        research_sources=research_sources,
    )


def _validate_cards(
    raw_cards: Any,
    topics: list[dict[str, Any]],
    *,
    research: bool,
) -> list[KnowledgeCard]:
    if not isinstance(raw_cards, list):
        raise ValueError("模型返回值缺少 cards 数组")
    topic_by_id = {topic["topic_id"]: topic for topic in topics}
    card_by_id: dict[int, KnowledgeCard] = {}
    for raw_card in raw_cards:
        if isinstance(raw_card, KnowledgeCard):
            topic_id = raw_card.topic_id
            if topic_id not in topic_by_id or topic_id in card_by_id:
                raise ValueError(f"模型返回了无效或重复 topic_id：{topic_id!r}")
            card_by_id[topic_id] = raw_card
            continue
        if not isinstance(raw_card, Mapping):
            raise ValueError("模型返回了无效知识卡")
        topic_id = raw_card.get("topic_id")
        if topic_id is None and len(topics) == 1:
            topic_id = topics[0]["topic_id"]
        if topic_id not in topic_by_id or topic_id in card_by_id:
            raise ValueError(f"模型返回了无效或重复 topic_id：{topic_id!r}")
        status = raw_card.get("status")
        if status not in CARD_STATUSES:
            raise ValueError(f"模型返回了无效 status：{status!r}")
        draft = _draft_from_mapping(
            raw_card,
            topic_id=topic_id,
            research=research,
        )
        card_by_id[topic_id] = _validate_draft(
            draft,
            topic_id=topic_id,
            topic=topic_by_id[topic_id],
            research=research,
        )
    missing_ids = set(topic_by_id) - set(card_by_id)
    if missing_ids:
        raise ValueError(f"模型漏掉 topic_id：{sorted(missing_ids)}")
    return [card_by_id[topic["topic_id"]] for topic in topics]


def validate_cards(
    raw_cards: Any,
    topics: list[dict[str, Any]],
) -> list[KnowledgeCard]:
    return _validate_cards(raw_cards, topics, research=False)


def validate_research_cards(
    raw_cards: Any,
    topics: list[dict[str, Any]],
) -> list[KnowledgeCard]:
    return _validate_cards(raw_cards, topics, research=True)


def cards_to_dicts(cards: list[KnowledgeCard]) -> list[dict[str, Any]]:
    return [card.to_dict() for card in cards]


__all__ = [
    "CARD_STATUSES",
    "KnowledgeCard",
    "REMOVED_LABELS",
    "card_response_schema",
    "cards_to_dicts",
    "http_url",
    "normalize_label",
    "validate_cards",
    "validate_research_cards",
]
