"""Unit tests for the AstrBot outbound transport."""

from __future__ import annotations

import httpx
import pytest

import agentscroll.integrations.astrbot as astrbot_module
from agentscroll.config import AstrBotIntegrationSettings
from agentscroll.integrations.astrbot import AstrBotTransport
from agentscroll.sharing import SendResult


TARGET = "qq:GroupMessage:123456789"


def test_astrbot_transport_validates_target(monkeypatch) -> None:
    monkeypatch.setenv("AGENTSCROLL_ASTRBOT_API_KEY", "test-key")
    transport = AstrBotTransport(AstrBotIntegrationSettings())

    transport.validate_target(TARGET)
    with pytest.raises(ValueError, match="platform:message_type:session_id"):
        transport.validate_target("not-an-umo")


def test_astrbot_transport_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("AGENTSCROLL_ASTRBOT_API_KEY", raising=False)

    with pytest.raises(ValueError, match="API key environment variable is not set"):
        AstrBotTransport(AstrBotIntegrationSettings())


def test_astrbot_transport_retries_connect_failure_and_posts_expected_payload(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AGENTSCROLL_ASTRBOT_API_KEY", "test-key")
    transport = AstrBotTransport(AstrBotIntegrationSettings())
    request = httpx.Request(
        "POST",
        "http://127.0.0.1:6185/api/v1/im/message",
    )
    responses = iter(
        [
            httpx.ConnectError("connect", request=request),
            httpx.Response(200, json={"status": "ok"}),
        ]
    )
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(astrbot_module.httpx, "post", post)
    monkeypatch.setattr(astrbot_module.time, "sleep", lambda _seconds: None)

    result = transport.send(TARGET, "hello")

    assert result == SendResult("sent", "ok", 2)
    assert len(calls) == 2
    assert calls[-1][0] == "http://127.0.0.1:6185/api/v1/im/message"
    assert calls[-1][1]["json"] == {"umo": TARGET, "message": "hello"}
    assert calls[-1][1]["headers"] == {"Authorization": "Bearer test-key"}


def test_astrbot_transport_does_not_retry_read_timeout(monkeypatch) -> None:
    monkeypatch.setenv("AGENTSCROLL_ASTRBOT_API_KEY", "test-key")
    transport = AstrBotTransport(AstrBotIntegrationSettings())
    request = httpx.Request(
        "POST",
        "http://127.0.0.1:6185/api/v1/im/message",
    )
    calls = 0

    def post(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("read", request=request)

    monkeypatch.setattr(astrbot_module.httpx, "post", post)

    result = transport.send(TARGET, "hello")

    assert result == SendResult("unknown", "ReadTimeout", 1)
    assert calls == 1
