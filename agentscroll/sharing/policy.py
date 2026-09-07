"""Content scoring and delivery decisions shared by the workflow and dispatcher."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentscroll.config import SharePolicySettings

HOTLIST_SCORES = (0, 3, 3.5, 4)


def hotlist_floor_score(title_count: int, threshold: int) -> float:
    if title_count >= threshold:
        return 4
    if title_count < 2:
        return 0
    gap = threshold - title_count
    return {1: 3.5, 2: 3}.get(gap, 0)


@dataclass(frozen=True)
class ShareDecision:
    score: float
    hotlist_score: float
    rules: tuple[str, ...]


def score_rules(
    general_score: float, interest_score: float, hotlist_score: float
) -> tuple[str, ...]:
    return tuple(
        rule
        for rule, score in (
            ("general_score", general_score),
            ("interest_score", interest_score),
            ("hotlist_title_count", hotlist_score),
        )
        if score >= 3
    )


def decide_share(
    *,
    status: str,
    relation: str,
    has_source: bool,
    hotlist_floor_score: float,
    general_score: float,
    interest_score: float,
) -> ShareDecision:
    if status != "complete" or not has_source:
        return ShareDecision(0, 0, ())
    hotlist_score = hotlist_floor_score if relation == "new" else 0
    return ShareDecision(
        max(general_score, interest_score, hotlist_score),
        hotlist_score,
        score_rules(general_score, interest_score, hotlist_score),
    )


@dataclass(frozen=True)
class DeliveryDecision:
    eligible: bool
    mode: str
    reasons: tuple[str, ...]


def decide_delivery(
    *,
    score: float,
    general_score: float,
    hotlist_score: float,
    policy: SharePolicySettings,
) -> DeliveryDecision:
    eligible = score >= policy.minimum_score
    threshold = policy.delivery.immediate_score
    reasons = ()
    if eligible and threshold is not None:
        reasons = tuple(
            rule
            for rule, value in (
                ("general_score", general_score),
                ("hotlist_title_count", hotlist_score),
            )
            if value >= threshold
        )
    return DeliveryDecision(
        eligible, "immediate" if reasons else "normal", reasons
    )


def legacy_share_trigger(rules: tuple[str, ...], general_score: float) -> str:
    """Keep the existing single-value review column alongside the full rules."""
    if "hotlist_title_count" in rules:
        return "hotlist_title_count"
    return "llm_major" if general_score == 4 else "normal"
