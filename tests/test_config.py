from __future__ import annotations

import pytest

from agentscroll.config import HotlistSettings, InterestSettings


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
