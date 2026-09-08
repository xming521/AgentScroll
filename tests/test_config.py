from __future__ import annotations

import pytest

from agentscroll.config import HotlistSettings, InterestSettings, SharePolicySettings


@pytest.mark.parametrize("field", ["keywords", "blocked_keywords", "soft_blocked_keywords"])
def test_interest_keywords_are_trimmed_and_normalized_for_deduplication(field: str) -> None:
    settings = InterestSettings.model_validate({field: ["  MCP  ", "ｍｃｐ", "机器人"]})

    assert getattr(settings, field) == ("MCP", "机器人")


@pytest.mark.parametrize("field", ["keywords", "blocked_keywords", "soft_blocked_keywords"])
@pytest.mark.parametrize("value", ["MCP", [1], [" "]])
def test_interest_keywords_reject_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=f"interest.{field}"):
        InterestSettings.model_validate({field: value})


def test_blocked_keywords_default_to_empty_without_changing_interests() -> None:
    settings = InterestSettings(keywords=["AI"])
    assert settings.blocked_keywords == ()
    assert settings.soft_blocked_keywords == ()
    assert settings.keywords == ("AI",)


def test_force_share_title_count_must_be_positive() -> None:
    with pytest.raises(ValueError):
        HotlistSettings(force_share_title_count=0)


@pytest.mark.parametrize("mode", ["window", "score_only"])
def test_immediate_threshold_cannot_be_below_active_minimum(mode: str) -> None:
    with pytest.raises(ValueError, match=f"policy.{mode}.min_score"):
        SharePolicySettings.model_validate({
            "mode": mode,
            mode: {"min_score": 3.5},
            "delivery": {"immediate_score": 3.4},
        })


@pytest.mark.parametrize("threshold", [None, 3.3, 4])
@pytest.mark.parametrize("mode", ["window", "score_only"])
def test_immediate_validation_uses_only_active_policy(mode: str, threshold: float | None) -> None:
    inactive = "score_only" if mode == "window" else "window"
    policy = SharePolicySettings.model_validate({
        "mode": mode,
        mode: {"min_score": 3.3},
        inactive: {"min_score": 4},
        "delivery": {"immediate_score": threshold},
    })
    assert policy.minimum_score == 3.3
