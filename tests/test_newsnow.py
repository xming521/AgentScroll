"""Offline tests for the NewsNow integration."""

from __future__ import annotations

import json

from agentscroll.collector import newsnow
from agentscroll.collector.sources import http


class _Response:
    status = 200

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(
            {
                "status": "success",
                "updatedTime": 1,
                "items": [
                    {
                        "id": "topic-1",
                        "title": "测试热点",
                        "url": "https://example.com/topic-1",
                    }
                ],
            }
        ).encode()


def test_newsnow_retries_a_transient_timeout(monkeypatch) -> None:
    calls = 0

    def urlopen(*_args: object, **_kwargs: object) -> _Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("timed out")
        return _Response()

    monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(http.time, "sleep", lambda _delay: None)

    result = newsnow.fetch_newsnow_hotlists("社媒", timeout=1, save=False)

    assert calls == 2
    assert result["total_items"] == 1
    assert result["sources"]["weibo"]["status"] == "success"


def test_newsnow_stops_after_bounded_timeout_retries(monkeypatch) -> None:
    calls = 0

    def urlopen(*_args: object, **_kwargs: object) -> _Response:
        nonlocal calls
        calls += 1
        raise TimeoutError("timed out")

    monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(http.time, "sleep", lambda _delay: None)

    result = newsnow.fetch_newsnow_hotlists("社媒", timeout=1, save=False)

    assert calls == 3
    assert result["total_items"] == 0
    assert result["sources"]["weibo"]["error"].endswith(
        "TimeoutError: timed out"
    )
