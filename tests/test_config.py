from __future__ import annotations

import pytest

from agentscroll.config import HotlistSettings, InterestSettings, SharePolicySettings


def test_interest_keywords_are_trimmed_and_normalized_for_deduplication() -> None:
    settings = InterestSettings(
        keywords=["  MCP  ", "ｍｃｐ", "机器人"],
    )

    assert settings.keywords == ("MCP", "机器人")


def test_interest_keywords_reject_non_array_value() -> None:
    with pytest.raises(ValueError, match="字符串数组"):
        InterestSettings(keywords="MCP")


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
