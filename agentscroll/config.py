from __future__ import annotations

import os
import unicodedata
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import pyjson5
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .inference import (
    CodexExecClient,
    LLMAuditLogger,
    LLMClient,
    LLMRequest,
    build_llm_client,
)


CONFIG_ENV = "AGENTSCROLL_CONFIG"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "settings.jsonc"
MODEL_PLACEHOLDER = "replace-with-model-name"
INFERENCE_AUDIT_DIR = Path.cwd() / "outputs" / "logs" / "llm_audit"


class APISettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    api_key_env: str = "AGENTSCROLL_LLM_API_KEY"


class CodexExecSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effort: str = "low"


class ScheduleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    every: str = "4h"
    start_time: str = "08:00"
    end_time: str = "00:00"

    @field_validator("every")
    @classmethod
    def validate_every(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized[:-1].isdigit() or normalized[:1] == "0" or normalized[-1:] not in {
            "m",
            "h",
            "d",
        }:
            raise ValueError("schedule.every 必须是正整数加 m、h 或 d，例如 30m、4h、1d")
        unit_seconds = {"m": 60, "h": 60 * 60, "d": 24 * 60 * 60}
        if int(normalized[:-1]) * unit_seconds[normalized[-1]] > 24 * 60 * 60:
            raise ValueError("schedule.every 不能超过 1d")
        return normalized

    @field_validator("start_time", "end_time")
    @classmethod
    def validate_local_time(cls, value: str) -> str:
        parts = value.strip().split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError("定时时间必须使用 HH:MM 格式")
        hour, minute = (int(part) for part in parts)
        if hour > 23 or minute > 59:
            raise ValueError("定时时间必须使用 00:00 到 23:59")
        return f"{hour:02d}:{minute:02d}"


class StorageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_path: Path = Path("outputs/agentscroll.sqlite3")


class InterestSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keywords: tuple[str, ...] = ()

    @field_validator("keywords", mode="before")
    @classmethod
    def normalize_keywords(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError("interest.keywords 必须是字符串数组")
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_keyword in value:
            if not isinstance(raw_keyword, str):
                raise ValueError("interest.keywords 只能包含字符串")
            keyword = " ".join(raw_keyword.split())
            if not keyword:
                raise ValueError("interest.keywords 不能包含空字符串")
            key = unicodedata.normalize("NFKC", keyword).casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(keyword)
        return tuple(normalized)


class HotlistSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force_share_title_count: int = Field(default=3, ge=1)


class WindowSharePolicySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_minutes: int = Field(default=60, gt=0)
    max_messages_per_window: int = Field(default=2, gt=0)


class ShareDeliverySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_interval_minutes: int = Field(default=10, ge=0)
    immediate_score: float | None = Field(default=4.0, ge=3, le=4)
    immediate_interval_seconds: int = Field(default=3, ge=0)


class ScoreOnlySharePolicySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_score: float = Field(default=4.0, ge=3, le=4)


class SharePolicySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["window", "score_only"] = "window"
    delivery: ShareDeliverySettings = Field(default_factory=ShareDeliverySettings)
    window: WindowSharePolicySettings = Field(
        default_factory=WindowSharePolicySettings
    )
    score_only: ScoreOnlySharePolicySettings = Field(
        default_factory=ScoreOnlySharePolicySettings
    )


class ShareDestinationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transport: str
    target: str

    @field_validator("transport", "target")
    @classmethod
    def validate_nonempty(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("sharing destination 的 transport 和 target 不能为空")
        return normalized


class SharingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    policy: SharePolicySettings = Field(default_factory=SharePolicySettings)
    destinations: tuple[ShareDestinationSettings, ...] = ()

    @model_validator(mode="after")
    def validate_enabled_destinations(self) -> SharingSettings:
        identities = [
            (destination.transport, destination.target)
            for destination in self.destinations
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("sharing.destinations 不能包含重复目标")
        if self.enabled and not self.destinations:
            raise ValueError("启用即时分享时至少配置一个 destination")
        return self


class AstrBotIntegrationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "http://127.0.0.1:6185"
    api_key_env: str = "AGENTSCROLL_ASTRBOT_API_KEY"

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("integrations.astrbot.base_url 必须是 HTTP(S) 地址")
        return normalized

    @field_validator("api_key_env")
    @classmethod
    def validate_api_key_env(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("integrations.astrbot.api_key_env 不能为空")
        return normalized


class IntegrationsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    astrbot: AstrBotIntegrationSettings = Field(
        default_factory=AstrBotIntegrationSettings
    )


class AgentScrollSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["api", "codex_exec"]
    model: str
    max_workers: int = Field(default=10, gt=0)
    api: APISettings
    codex_exec: CodexExecSettings = Field(default_factory=CodexExecSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    interest: InterestSettings = Field(default_factory=InterestSettings)
    hotlist: HotlistSettings = Field(default_factory=HotlistSettings)
    sharing: SharingSettings = Field(default_factory=SharingSettings)
    integrations: IntegrationsSettings = Field(default_factory=IntegrationsSettings)


def resolve_config_path(config_path: str | Path | None = None) -> Path:
    configured = config_path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG_PATH
    return Path(configured).expanduser().resolve()


def load_settings(config_path: str | Path | None = None) -> AgentScrollSettings:
    path = resolve_config_path(config_path)
    if not path.is_file():
        raise FileNotFoundError(f"AgentScroll config not found: {path}")

    data = pyjson5.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"AgentScroll config must be a JSON object: {path}")
    return AgentScrollSettings.model_validate(data)


def _validated_model(settings: AgentScrollSettings) -> str:
    model = settings.model.strip()
    if not model or model == MODEL_PLACEHOLDER:
        raise ValueError("Set a real model name in settings.jsonc before running inference")
    return model


def build_configured_client(
    settings: AgentScrollSettings,
    *,
    enable_web_search: bool = False,
) -> LLMClient:
    model = _validated_model(settings)
    audit_logger = LLMAuditLogger(INFERENCE_AUDIT_DIR)

    if settings.provider == "codex_exec":
        return CodexExecClient(
            model=model,
            effort=settings.codex_exec.effort,
            max_workers=settings.max_workers,
            enable_web_search=enable_web_search,
            audit_logger=audit_logger,
        )

    if enable_web_search:
        raise ValueError("当前 API 推理后端尚未接入 web search，请使用 codex_exec")

    api_key = os.environ.get(settings.api.api_key_env, "").strip()
    if not api_key:
        raise ValueError(
            f"Inference API key environment variable is not set: {settings.api.api_key_env}"
        )
    return build_llm_client(
        "api",
        api_key=api_key,
        base_url=settings.api.base_url,
        model=model,
        max_workers=settings.max_workers,
        audit_logger=audit_logger,
    )


def make_configured_request(
    prompt: str,
    settings: AgentScrollSettings,
    **overrides: Any,
) -> LLMRequest:
    request_args: dict[str, Any] = {
        "model": _validated_model(settings),
        "provider": settings.provider,
    }
    if settings.provider == "codex_exec":
        request_args["effort"] = settings.codex_exec.effort
    request_args.update({key: value for key, value in overrides.items() if value is not None})
    return LLMRequest.from_prompt(prompt, **request_args)


__all__ = [
    "AstrBotIntegrationSettings",
    "AgentScrollSettings",
    "CONFIG_ENV",
    "DEFAULT_CONFIG_PATH",
    "IntegrationsSettings",
    "InterestSettings",
    "ScheduleSettings",
    "ScoreOnlySharePolicySettings",
    "ShareDeliverySettings",
    "ShareDestinationSettings",
    "SharePolicySettings",
    "SharingSettings",
    "StorageSettings",
    "WindowSharePolicySettings",
    "build_configured_client",
    "load_settings",
    "make_configured_request",
    "resolve_config_path",
]
