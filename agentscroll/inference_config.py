from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import pyjson5
from pydantic import BaseModel, ConfigDict, Field

from .inference import CodexExecClient, LLMClient, LLMRequest, build_llm_client


INFERENCE_CONFIG_ENV = "AGENTSCROLL_INFERENCE_CONFIG"
DEFAULT_INFERENCE_CONFIG_PATH = Path(__file__).resolve().parents[1] / "settings.jsonc"
MODEL_PLACEHOLDER = "replace-with-model-name"


class APISettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    api_key_env: str = "AGENTSCROLL_LLM_API_KEY"


class CodexExecSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effort: str = "low"


class InferenceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["api", "codex_exec"]
    model: str
    max_workers: int = Field(default=10, gt=0)
    api: APISettings
    codex_exec: CodexExecSettings = Field(default_factory=CodexExecSettings)


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

    if settings.provider == "codex_exec":
        return CodexExecClient(
            model=model,
            effort=settings.codex_exec.effort,
            max_workers=settings.max_workers,
            enable_web_search=enable_web_search,
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
    "build_configured_client",
    "load_inference_settings",
    "make_configured_request",
    "resolve_inference_config_path",
]
