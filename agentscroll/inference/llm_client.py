import json
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Literal, Protocol
from urllib.parse import urlparse

import httpx
import pyjson5
from openai import BadRequestError, OpenAI

from ._common import (
    API_TIMEOUT_SECONDS,
    OPENAI_API_MAX_RETRIES,
    calculate_retry_delay,
    logger,
    openrouter_proxy_url,
    project_root,
)
from .audit import LLMAuditCall, LLMAuditLogger

ProviderName = Literal["codex_exec", "api"]
Message = dict[str, str]
ParsedJson = dict[str, Any] | list[Any]
RUNTIME_ROOT = project_root()
CODEX_EXEC_RUN_DIR = RUNTIME_ROOT / "outputs" / "logs" / "codex_exec"


@dataclass
class LLMRequest:
    messages: list[Message]
    model: str | None = None
    provider: ProviderName | str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    timeout: int | None = None
    stream: bool = False
    json_mode: bool = False
    json_schema: dict[str, Any] | None = None
    extra_body: dict[str, Any] | None = None
    effort: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_prompt(cls, prompt: str, **kwargs: Any) -> "LLMRequest":
        return cls(messages=[{"role": "user", "content": prompt}], **kwargs)


@dataclass
class LLMResponse:
    ok: bool
    text: str | None = None
    error: str | None = None
    parsed_json: ParsedJson | None = None
    raw: Any = None
    provider: str = ""
    model: str = ""
    elapsed_s: float | None = None
    cost_usd: float | None = None
    finish_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


PromptLike = LLMRequest | str | list[Message]


class LLMClient(Protocol):
    provider: str

    def chat(self, prompt: PromptLike, **kwargs: Any) -> LLMResponse: ...

    def chat_batch(
        self, prompts: Iterable[PromptLike], **kwargs: Any
    ) -> list[LLMResponse]: ...

    def generate(self, request: LLMRequest) -> LLMResponse: ...

    def generate_batch(self, requests: Iterable[LLMRequest]) -> list[LLMResponse]: ...

    def close(self) -> None: ...


def messages_to_prompt(messages: list[Message]) -> str:
    if len(messages) == 1 and messages[0].get("role") == "user":
        return messages[0].get("content", "")

    rendered = []
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        rendered.append(f"{role.upper()}:\n{content}")
    return "\n\n".join(rendered)


def _response_audit_payload(response: LLMResponse) -> dict[str, Any]:
    return {
        "ok": response.ok,
        "text": response.text,
        "error": response.error,
        "parsed_json": response.parsed_json,
        "provider": response.provider,
        "model": response.model,
        "elapsed_s": response.elapsed_s,
        "cost_usd": response.cost_usd,
        "finish_reason": response.finish_reason,
        "metadata": response.metadata,
    }


def _finish_audit(call: LLMAuditCall, response: LLMResponse) -> LLMResponse:
    call.finish(_response_audit_payload(response))
    return response


def _provider_request_id(raw: Any) -> str | None:
    value = getattr(raw, "_request_id", None)
    return str(value) if value else None


def _api_response_metadata(raw: Any) -> dict[str, Any]:
    usage = getattr(raw, "usage", None)
    if usage is None:
        return {}
    model_dump = getattr(usage, "model_dump", None)
    if callable(model_dump):
        try:
            return {"usage": model_dump(mode="json")}
        except TypeError:
            return {"usage": model_dump()}
    return {"usage": usage}


def make_request(prompt: PromptLike, **kwargs: Any) -> LLMRequest:
    overrides = {key: value for key, value in kwargs.items() if value is not None}
    if isinstance(prompt, LLMRequest):
        return replace(prompt, **overrides) if overrides else prompt
    if isinstance(prompt, str):
        return LLMRequest.from_prompt(prompt, **overrides)
    return LLMRequest(messages=prompt, **overrides)


def parse_json_from_text(text: str | None) -> ParsedJson:
    if text is None:
        raise ValueError("empty response")
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    start_obj = stripped.find("{")
    start_arr = stripped.find("[")
    starts = [pos for pos in (start_obj, start_arr) if pos >= 0]
    if not starts:
        raise ValueError(f"no JSON object or array in response: {stripped[:200]!r}")
    start = min(starts)
    opening = stripped[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(stripped)):
        char = stripped[idx]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return json.loads(stripped[start : idx + 1])
    raise ValueError(f"unbalanced JSON in response: {stripped[:200]!r}")


def _maybe_parse_response_json(
    request: LLMRequest, text: str | None
) -> ParsedJson | None:
    if request.json_mode or request.json_schema:
        return parse_json_from_text(text)
    return None


def _classify_error(text: str) -> str:
    lowered = (text or "").lower()
    if any(
        key in lowered
        for key in ("overload", "rate limit", "rate_limit", "429", "too many requests")
    ):
        return "overload/rate_limit"
    if any(
        key in lowered
        for key in ("usage limit", "quota", "exceeded", "out of credit", "insufficient")
    ):
        return "quota"
    if any(key in lowered for key in ("auth", "unauthorized", "401", "login")):
        return "auth"
    return "other"


def _response_format_type_unavailable(exc: BadRequestError) -> bool:
    return "response_format type is unavailable" in str(exc).lower()


def _is_deepseek_api(base_url: str | None) -> bool:
    return (urlparse(base_url or "").hostname or "").lower() == "api.deepseek.com"


class BaseBatchMixin:
    max_workers: int

    def chat(self, prompt: PromptLike, **kwargs: Any) -> LLMResponse:
        return self.generate(make_request(prompt, **kwargs))

    def chat_batch(
        self, prompts: Iterable[PromptLike], **kwargs: Any
    ) -> list[LLMResponse]:
        return self.generate_batch(make_request(prompt, **kwargs) for prompt in prompts)

    def generate(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError

    def generate_batch(self, requests: Iterable[LLMRequest]) -> list[LLMResponse]:
        request_list = list(requests)
        if not request_list:
            return []

        results: list[LLMResponse | None] = [None] * len(request_list)
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_idx = {
                executor.submit(self.generate, request): idx
                for idx, request in enumerate(request_list)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except Exception as exc:
                    request = request_list[idx]
                    results[idx] = LLMResponse(
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                        provider=request.provider or getattr(self, "provider", ""),
                        model=request.model or getattr(self, "model", "") or "",
                    )
        return [result for result in results if result is not None]


class OpenAICompatibleClient(BaseBatchMixin):
    provider = "api"

    def __init__(
        self,
        api_key: str,
        base_url: str | None,
        model: str | None = None,
        max_workers: int = 10,
        timeout: int = API_TIMEOUT_SECONDS,
        audit_logger: LLMAuditLogger | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.max_workers = max_workers
        self.timeout = timeout
        self.audit_logger = audit_logger or LLMAuditLogger()
        self.is_deepseek_api = _is_deepseek_api(base_url)
        proxy_url = openrouter_proxy_url(base_url or "")
        self.http_client = (
            httpx.Client(proxy=proxy_url, timeout=timeout) if proxy_url else None
        )
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,
            timeout=timeout,
            http_client=self.http_client,
        )

    def generate(self, request: LLMRequest) -> LLMResponse:
        model = self.model or ""
        with self.audit_logger.start_call(
            request=request,
            provider=self.provider,
            model=model,
            backend={"base_url": self.base_url},
        ) as audit_call:
            if not model:
                raise ValueError("model is required for api backend")

            params: dict[str, Any] = {
                "model": model,
                "messages": request.messages,
                "stream": request.stream,
            }
            if request.max_tokens is not None:
                params["max_tokens"] = request.max_tokens
            if request.temperature is not None:
                params["temperature"] = request.temperature
            if request.top_p is not None:
                params["top_p"] = request.top_p
            extra_body = dict(request.extra_body or {})
            if extra_body:
                params["extra_body"] = extra_body
            if request.timeout is not None:
                params["timeout"] = request.timeout
            if request.json_schema:
                if self.is_deepseek_api:
                    params["response_format"] = {"type": "json_object"}
                else:
                    params["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "llm_response",
                            "schema": request.json_schema,
                            "strict": True,
                        },
                    }
            elif request.json_mode:
                params["response_format"] = {"type": "json_object"}

            t0 = time.time()
            empty_json_retries = 0
            for attempt_index in range(OPENAI_API_MAX_RETRIES + 1):
                attempt = attempt_index + 1
                audit_call.start_attempt(
                    attempt,
                    parameters={
                        key: value for key, value in params.items() if key != "messages"
                    },
                )
                try:
                    raw = self.client.chat.completions.create(**params)
                except BadRequestError as exc:
                    response_format = params.get("response_format")
                    will_retry = bool(
                        request.json_schema
                        and isinstance(response_format, dict)
                        and response_format.get("type") == "json_schema"
                        and _response_format_type_unavailable(exc)
                    )
                    audit_call.finish_attempt(
                        attempt,
                        status="failed",
                        error=exc,
                        provider_request_id=getattr(exc, "request_id", None),
                        will_retry=will_retry,
                        retry_reason="json_schema_unsupported" if will_retry else None,
                    )
                    if will_retry:
                        params["response_format"] = {"type": "json_object"}
                        logger.warning(
                            "OpenAI-compatible API does not support response_format "
                            "type json_schema; falling back to json_object"
                        )
                        continue
                    elapsed = round(time.time() - t0, 3)
                    return _finish_audit(
                        audit_call,
                        LLMResponse(
                            ok=False,
                            error=f"{type(exc).__name__}: {exc}",
                            provider=self.provider,
                            model=model,
                            elapsed_s=elapsed,
                        ),
                    )
                except Exception as exc:
                    will_retry = attempt_index < OPENAI_API_MAX_RETRIES
                    delay = calculate_retry_delay(attempt_index) if will_retry else None
                    audit_call.finish_attempt(
                        attempt,
                        status="failed",
                        error=exc,
                        provider_request_id=getattr(exc, "request_id", None),
                        will_retry=will_retry,
                        retry_reason="provider_error" if will_retry else None,
                        retry_delay_s=delay,
                    )
                    if will_retry and delay is not None:
                        logger.warning(
                            f"OpenAI-compatible API failed: {type(exc).__name__}: {exc}; "
                            f"retry {attempt}/{OPENAI_API_MAX_RETRIES + 1} after {delay:.2f}s"
                        )
                        time.sleep(delay)
                        continue
                    elapsed = round(time.time() - t0, 3)
                    return _finish_audit(
                        audit_call,
                        LLMResponse(
                            ok=False,
                            error=f"{type(exc).__name__}: {exc}",
                            provider=self.provider,
                            model=model,
                            elapsed_s=elapsed,
                        ),
                    )

                elapsed = round(time.time() - t0, 3)
                provider_request_id = _provider_request_id(raw)
                try:
                    choice = raw.choices[0]
                    text = choice.message.content
                except Exception as exc:
                    will_retry = attempt_index < OPENAI_API_MAX_RETRIES
                    delay = calculate_retry_delay(attempt_index) if will_retry else None
                    audit_call.finish_attempt(
                        attempt,
                        status="invalid_response",
                        response=raw,
                        error=exc,
                        provider_request_id=provider_request_id,
                        will_retry=will_retry,
                        retry_reason="invalid_response" if will_retry else None,
                        retry_delay_s=delay,
                    )
                    if will_retry and delay is not None:
                        logger.warning(
                            f"OpenAI-compatible API returned an invalid response: "
                            f"{type(exc).__name__}: {exc}; retry {attempt}/"
                            f"{OPENAI_API_MAX_RETRIES + 1} after {delay:.2f}s"
                        )
                        time.sleep(delay)
                        continue
                    return _finish_audit(
                        audit_call,
                        LLMResponse(
                            ok=False,
                            error=f"invalid response: {type(exc).__name__}: {exc}",
                            raw=raw,
                            provider=self.provider,
                            model=model,
                            elapsed_s=elapsed,
                            metadata=_api_response_metadata(raw),
                        ),
                    )

                if not (text or "").strip():
                    will_retry = bool(
                        (request.json_schema or request.json_mode)
                        and empty_json_retries < 1
                    )
                    audit_call.finish_attempt(
                        attempt,
                        status="empty_response",
                        response=raw,
                        provider_request_id=provider_request_id,
                        will_retry=will_retry,
                        retry_reason="empty_json" if will_retry else None,
                    )
                    if will_retry:
                        empty_json_retries += 1
                        logger.warning(
                            "OpenAI-compatible API returned empty JSON content; "
                            "retrying once"
                        )
                        continue
                    return _finish_audit(
                        audit_call,
                        LLMResponse(
                            ok=False,
                            text=text,
                            error="empty response",
                            raw=raw,
                            provider=self.provider,
                            model=model,
                            elapsed_s=elapsed,
                            finish_reason=choice.finish_reason,
                            metadata=_api_response_metadata(raw),
                        ),
                    )

                audit_call.finish_attempt(
                    attempt,
                    status="succeeded",
                    response=raw,
                    provider_request_id=provider_request_id,
                )
                try:
                    parsed_json = _maybe_parse_response_json(request, text)
                except Exception as exc:
                    return _finish_audit(
                        audit_call,
                        LLMResponse(
                            ok=False,
                            text=text,
                            error=f"json parse fail: {type(exc).__name__}: {exc}",
                            raw=raw,
                            provider=self.provider,
                            model=model,
                            elapsed_s=elapsed,
                            finish_reason=choice.finish_reason,
                            metadata=_api_response_metadata(raw),
                        ),
                    )
                return _finish_audit(
                    audit_call,
                    LLMResponse(
                        ok=True,
                        text=text.strip(),
                        parsed_json=parsed_json,
                        raw=raw,
                        provider=self.provider,
                        model=model,
                        elapsed_s=elapsed,
                        finish_reason=choice.finish_reason,
                        metadata=_api_response_metadata(raw),
                    ),
                )

    def close(self) -> None:
        self.client.close()
        if self.http_client is not None:
            self.http_client.close()

    def __enter__(self) -> "OpenAICompatibleClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


class CodexExecClient(BaseBatchMixin):
    provider = "codex_exec"

    def __init__(
        self,
        model: str | None = None,
        effort: str = "low",
        max_workers: int = 10,
        timeout: int = 120,
        command: str = "codex",
        sandbox: str = "read-only",
        cwd: str | Path | None = None,
        extra_args: Iterable[str] | None = None,
        enable_web_search: bool = False,
        audit_logger: LLMAuditLogger | None = None,
    ):
        self.model = model
        self.effort = effort
        self.max_workers = max_workers
        self.timeout = timeout
        self.command = command
        self.sandbox = sandbox
        self.cwd = Path(cwd).resolve() if cwd else RUNTIME_ROOT
        self.requests_dir = CODEX_EXEC_RUN_DIR / "requests"
        self.extra_args = list(extra_args or [])
        self.enable_web_search = enable_web_search
        self.audit_logger = audit_logger or LLMAuditLogger()

    def _build_command(
        self,
        request: LLMRequest,
        *,
        output_path: Path,
        schema_path: Path | None,
        cwd: Path,
    ) -> list[str]:
        model = request.model or self.model
        if not model:
            raise ValueError("model is required for codex exec backend")

        cmd = [self.command]
        if self.enable_web_search:
            cmd.append("--search")
        cmd += [
            "exec",
            "--json",
            "--color",
            "never",
            "--ephemeral",
            "--skip-git-repo-check",
            "--cd",
            str(cwd),
            "--sandbox",
            self.sandbox,
            "--output-last-message",
            str(output_path),
            "--model",
            model,
        ]
        effort = request.effort or self.effort
        if effort:
            cmd += ["-c", f"model_reasoning_effort={json.dumps(effort)}"]
        if schema_path is not None:
            cmd += ["--output-schema", str(schema_path)]
        cmd += self.extra_args
        cmd.append("-")
        return cmd

    def generate(self, request: LLMRequest) -> LLMResponse:
        model = request.model or self.model or ""
        timeout = request.timeout or self.timeout
        with self.audit_logger.start_call(
            request=request,
            provider=self.provider,
            model=model,
            backend={
                "command": self.command,
                "cwd": self.cwd,
                "sandbox": self.sandbox,
                "enable_web_search": self.enable_web_search,
                "extra_args": self.extra_args,
            },
        ) as audit_call:
            prompt = messages_to_prompt(request.messages)
            t0 = time.time()
            attempt = 1

            self.requests_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix="request-",
                dir=self.requests_dir,
                ignore_cleanup_errors=True,
            ) as tmp_dir:
                tmp_path = Path(tmp_dir)
                output_path = tmp_path / "last_message.txt"
                schema_path = None
                if request.json_schema:
                    schema_path = tmp_path / "output_schema.json"
                    schema_path.write_text(
                        json.dumps(request.json_schema, ensure_ascii=False),
                        encoding="utf-8",
                    )
                cmd = self._build_command(
                    request,
                    output_path=output_path,
                    schema_path=schema_path,
                    cwd=self.cwd,
                )

                audit_call.start_attempt(
                    attempt,
                    parameters={
                        "model": model,
                        "effort": request.effort or self.effort,
                        "timeout": timeout,
                        "sandbox": self.sandbox,
                        "cwd": self.cwd,
                        "json_schema": request.json_schema,
                        "enable_web_search": self.enable_web_search,
                        "extra_args": self.extra_args,
                    },
                )
                try:
                    proc = subprocess.run(
                        cmd,
                        input=prompt,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    audit_call.finish_attempt(
                        attempt,
                        status="timeout",
                        error=exc,
                    )
                    return _finish_audit(
                        audit_call,
                        LLMResponse(
                            ok=False,
                            error=f"timeout>{timeout}s",
                            provider=self.provider,
                            model=model,
                            elapsed_s=round(time.time() - t0, 3),
                        ),
                    )
                except Exception as exc:
                    audit_call.finish_attempt(
                        attempt,
                        status="failed",
                        error=exc,
                    )
                    raise

                output_text = ""
                try:
                    if output_path.exists():
                        output_text = output_path.read_text(encoding="utf-8").strip()
                except Exception as exc:
                    audit_call.finish_attempt(
                        attempt,
                        status="failed",
                        response={
                            "returncode": proc.returncode,
                            "stdout": proc.stdout,
                            "stderr": proc.stderr,
                        },
                        error=exc,
                    )
                    raise

            elapsed = round(time.time() - t0, 3)
            stdout = (proc.stdout or "").strip()
            stderr = (proc.stderr or "").strip()
            raw_response = {
                "returncode": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "output": output_text,
            }
            audit_call.finish_attempt(
                attempt,
                status="succeeded" if proc.returncode == 0 else "failed",
                response=raw_response,
            )

            usage: dict[str, int] = {}
            web_search_calls = 0
            for line in stdout.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                item = event.get("item")
                if (
                    event.get("type") == "item.completed"
                    and isinstance(item, dict)
                    and item.get("type") == "web_search"
                ):
                    web_search_calls += 1
                if event.get("type") != "turn.completed":
                    continue
                raw_usage = event.get("usage")
                if not isinstance(raw_usage, dict):
                    continue
                usage = {
                    str(key): value
                    for key, value in raw_usage.items()
                    if isinstance(value, int) and not isinstance(value, bool)
                }
            metadata: dict[str, Any] = {}
            if usage:
                metadata["usage"] = usage
            if self.enable_web_search:
                metadata["web_search_calls"] = web_search_calls

            if proc.returncode != 0:
                detail = output_text or stdout[:400]
                blob = f"{stderr} || {detail}"
                return _finish_audit(
                    audit_call,
                    LLMResponse(
                        ok=False,
                        error=(
                            f"returncode={proc.returncode}[{_classify_error(blob)}]: "
                            f"{blob[:400]}"
                        ),
                        raw=raw_response,
                        provider=self.provider,
                        model=model,
                        elapsed_s=elapsed,
                        metadata=metadata,
                    ),
                )

            text = output_text or stdout
            if not text:
                return _finish_audit(
                    audit_call,
                    LLMResponse(
                        ok=False,
                        error="empty response",
                        raw=raw_response,
                        provider=self.provider,
                        model=model,
                        elapsed_s=elapsed,
                        metadata=metadata,
                    ),
                )
            try:
                parsed_json = _maybe_parse_response_json(request, text)
            except Exception as exc:
                return _finish_audit(
                    audit_call,
                    LLMResponse(
                        ok=False,
                        text=text,
                        error=f"json parse fail: {type(exc).__name__}: {exc}",
                        raw=raw_response,
                        provider=self.provider,
                        model=model,
                        elapsed_s=elapsed,
                        metadata=metadata,
                    ),
                )

            return _finish_audit(
                audit_call,
                LLMResponse(
                    ok=True,
                    text=text.strip(),
                    parsed_json=parsed_json,
                    raw=raw_response,
                    provider=self.provider,
                    model=model,
                    elapsed_s=elapsed,
                    metadata=metadata,
                ),
            )

    def close(self) -> None:
        return None

    def __enter__(self) -> "CodexExecClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


def normalize_provider(provider: str) -> str:
    provider = provider.lower().strip().replace("-", "_")
    if provider in {"codex", "codex_exec"}:
        return "codex_exec"
    if provider in {"openai", "deepseek", "openrouter", "openai_compatible", "api"}:
        return "api"
    raise ValueError(f"unknown llm provider: {provider}")


def load_api_config(config_path: str | Path) -> dict[str, str]:
    config_file = Path(config_path)
    config_data = pyjson5.loads(config_file.read_text(encoding="utf-8"))
    make_dataset_args = config_data.get("make_dataset_args", {})
    api_key = make_dataset_args.get("llm_api_key")
    base_url = make_dataset_args.get("base_url")
    model = make_dataset_args.get("model_name")
    missing = [
        name
        for name, value in (
            ("make_dataset_args.llm_api_key", api_key),
            ("make_dataset_args.base_url", base_url),
            ("make_dataset_args.model_name", model),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"Missing API config fields in {config_file}: {missing}")
    return {
        "api_key": str(api_key).strip(),
        "base_url": str(base_url).strip(),
        "model": str(model).strip(),
    }


def build_llm_client(
    provider: ProviderName | str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    model_name: str | None = None,
    config_path: str | Path | None = None,
    max_workers: int = 10,
    timeout: int | None = None,
    effort: str = "low",
    command: str = "codex",
    sandbox: str = "read-only",
    audit_logger: LLMAuditLogger | None = None,
) -> LLMClient:
    normalized_provider = normalize_provider(provider)
    resolved_model = model or model_name

    if normalized_provider == "codex_exec":
        return CodexExecClient(
            model=resolved_model,
            effort=effort,
            max_workers=max_workers,
            timeout=timeout or 120,
            command=command,
            sandbox=sandbox,
            audit_logger=audit_logger,
        )

    if config_path is not None:
        api_config = load_api_config(config_path)
        api_key = api_config["api_key"]
        base_url = api_config["base_url"]
        resolved_model = api_config["model"]

    missing = [
        name
        for name, value in (
            ("api_key", api_key),
            ("base_url", base_url),
            ("model", resolved_model),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"Missing API client arguments: {missing}")

    return OpenAICompatibleClient(
        api_key=str(api_key),
        base_url=str(base_url),
        model=str(resolved_model),
        max_workers=max_workers,
        timeout=timeout or API_TIMEOUT_SECONDS,
        audit_logger=audit_logger,
    )


__all__ = [
    "CodexExecClient",
    "LLMClient",
    "LLMAuditLogger",
    "LLMRequest",
    "LLMResponse",
    "OpenAICompatibleClient",
    "build_llm_client",
    "load_api_config",
    "make_request",
    "messages_to_prompt",
    "normalize_provider",
    "parse_json_from_text",
]
