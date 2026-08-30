from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import pyjson5
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .inference import (
    CodexExecClient,
    LLMAuditLogger,
    LLMClient,
    LLMRequest,
    build_llm_client,
)


INFERENCE_CONFIG_ENV = "AGENTSCROLL_INFERENCE_CONFIG"
DEFAULT_INFERENCE_CONFIG_PATH = Path(__file__).resolve().parents[1] / "settings.jsonc"
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


class InferenceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["api", "codex_exec"]
    model: str
    max_workers: int = Field(default=10, gt=0)
    api: APISettings
    codex_exec: CodexExecSettings = Field(default_factory=CodexExecSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)


def resolve_inference_config_path(config_path: str | Path | None = None) -> Path:
    configured = config_path or os.environ.get(INFERENCE_CONFIG_ENV) or DEFAULT_INFERENCE_CONFIG_PATH
    return Path(configured).expanduser().resolve()


def load_inference_settings(config_path: str | Path | None = None) -> InferenceSettings:
    path = resolve_inference_config_path(config_path)
    if not path.is_file():
        raise FileNotFoundError(f"Inference config not found: {path}")

    data = pyjson5.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Inference config must be a JSON object: {path}")
    return InferenceSettings.model_validate(data)


def _validated_model(settings: InferenceSettings) -> str:
    model = settings.model.strip()
    if not model or model == MODEL_PLACEHOLDER:
        raise ValueError("Set a real model name in settings.jsonc before running inference")
    return model


def build_configured_client(
    settings: InferenceSettings,
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
    settings: InferenceSettings,
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
    "DEFAULT_INFERENCE_CONFIG_PATH",
    "INFERENCE_CONFIG_ENV",
    "InferenceSettings",
    "ScheduleSettings",
    "build_configured_client",
    "load_inference_settings",
    "make_configured_request",
    "resolve_inference_config_path",
]
