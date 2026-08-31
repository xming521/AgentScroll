"""External service integrations."""

from __future__ import annotations

from collections.abc import Iterable

from agentscroll.config import IntegrationsSettings
from agentscroll.sharing import ShareTransport

from .astrbot import AstrBotTransport


def build_share_transports(
    names: Iterable[str],
    settings: IntegrationsSettings,
) -> dict[str, ShareTransport]:
    requested = set(names)
    unsupported = requested - {"astrbot"}
    if unsupported:
        raise ValueError(
            f"Unsupported sharing transports: {', '.join(sorted(unsupported))}"
        )
    transports: dict[str, ShareTransport] = {}
    if "astrbot" in requested:
        transports["astrbot"] = AstrBotTransport(settings.astrbot)
    return transports


__all__ = ["AstrBotTransport", "build_share_transports"]
