"""AstrBot transport for outbound share messages."""

from __future__ import annotations

import os
import time
from urllib.parse import urlparse

import httpx

from agentscroll.config import AstrBotIntegrationSettings
from agentscroll.sharing.dispatcher import SendResult


ASTRBOT_BASE_URL_ENV = "AGENTSCROLL_ASTRBOT_BASE_URL"


class AstrBotTransport:
    """Send already-rendered messages through AstrBot's IM OpenAPI."""

    def __init__(self, settings: AstrBotIntegrationSettings) -> None:
        api_key = os.environ.get(settings.api_key_env, "").strip()
        if not api_key:
            raise ValueError(
                f"AstrBot API key environment variable is not set: {settings.api_key_env}"
            )
        configured_base_url = os.environ.get(ASTRBOT_BASE_URL_ENV, settings.base_url)
        self._base_url = self._normalize_base_url(configured_base_url)
        self._api_key = api_key

    @staticmethod
    def _normalize_base_url(value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("AGENTSCROLL_ASTRBOT_BASE_URL 必须是 HTTP(S) 地址")
        return normalized

    def validate_target(self, target: str) -> None:
        parts = target.split(":", 2)
        if len(parts) != 3 or not all(part.strip() for part in parts):
            raise ValueError("AstrBot target 必须使用 platform:message_type:session_id")

    def send(self, target: str, message: str) -> SendResult:
        delays = (0, 1, 3)
        for attempt, delay in enumerate(delays, start=1):
            if delay:
                time.sleep(delay)
            try:
                response = httpx.post(
                    f"{self._base_url}/api/v1/im/message",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"umo": target, "message": message},
                    timeout=httpx.Timeout(10, connect=3),
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt < len(delays):
                    continue
                return SendResult("failed", type(exc).__name__, attempt)
            except httpx.RequestError as exc:
                return SendResult("unknown", type(exc).__name__, attempt)

            if not response.is_success:
                return SendResult("failed", f"HTTP {response.status_code}", attempt)
            try:
                payload = response.json()
            except ValueError:
                return SendResult("failed", "invalid_json_response", attempt)
            if not isinstance(payload, dict) or payload.get("status") != "ok":
                return SendResult("failed", "astrbot_error_response", attempt)
            return SendResult("sent", "ok", attempt)
        raise AssertionError("unreachable")


__all__ = ["ASTRBOT_BASE_URL_ENV", "AstrBotTransport"]
