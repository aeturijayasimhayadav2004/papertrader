"""Groq as the brain's model provider (free tier), behind the same small surface the brain
uses from the Anthropic SDK: client.beta.messages.create(**kw) -> response with
.content (text blocks), .stop_reason, .usage and .model.

Groq serves open models (e.g. openai/gpt-oss-120b) through an OpenAI-compatible
chat-completions endpoint. Standard library only. The key is read from GROQ_API_KEY
and never logged. JSON mode makes the model return one JSON object; the brain still
validates it field by field, exactly as for Claude.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b"


class GroqError(RuntimeError):
    pass


@dataclass
class _Block:
    text: str
    type: str = "text"


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class GroqResponse:
    content: list[_Block] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: _Usage = field(default_factory=_Usage)
    model: str = ""


def groq_key() -> str | None:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        try:  # a local run may keep the key only in .env
            from dotenv import load_dotenv

            from papertrader.config import PROJECT_ROOT
            load_dotenv(PROJECT_ROOT / ".env", override=False)
        except Exception:
            return None
        key = os.environ.get("GROQ_API_KEY")
    return key or None


def build_body(model: str, max_tokens: int, system: Any, messages: list[dict[str, Any]],
               output_config: dict[str, Any] | None) -> dict[str, Any]:
    """Anthropic-style request (system blocks, user turn, optional JSON schema) -> Groq body."""
    sys_text = system if isinstance(system, str) else "".join(b.get("text", "") for b in system)
    fmt = (output_config or {}).get("format")
    if fmt:
        sys_text += ("\n\nReply with ONE JSON object only, no other text, matching this JSON schema:\n"
                     + json.dumps(fmt.get("schema"), sort_keys=True))
    body: dict[str, Any] = {
        "model": model,
        "max_completion_tokens": max_tokens,
        "messages": [{"role": "system", "content": sys_text}]
        + [{"role": m["role"], "content": m["content"]} for m in messages],
    }
    if fmt:
        body["response_format"] = {"type": "json_object"}
    if model.startswith("openai/gpt-oss"):
        body["reasoning_effort"] = "low"
    return body


class _Messages:
    def __init__(self, outer: "GroqClient"):
        self.outer = outer

    def create(self, *, model: str, max_tokens: int, system: Any, messages: list[dict[str, Any]],
               output_config: dict[str, Any] | None = None, **_ignored: Any) -> GroqResponse:
        data = self.outer._post(build_body(model, max_tokens, system, messages, output_config))
        choice = (data.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content") or "").strip()
        usage = data.get("usage") or {}
        return GroqResponse(
            content=[_Block(text)],
            stop_reason="max_tokens" if choice.get("finish_reason") == "length" else "end_turn",
            usage=_Usage(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)),
            model=str(data.get("model") or model),
        )


class _Beta:
    def __init__(self, outer: "GroqClient"):
        self.messages = _Messages(outer)


class GroqClient:
    def __init__(self, key: str | None = None, timeout: float = 60.0, retries: int = 2):
        self.key = key or groq_key()
        if not self.key:
            raise GroqError("no GROQ_API_KEY in the environment or .env")
        self.timeout = timeout
        self.retries = retries
        self.beta = _Beta(self)

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST", headers={
            "Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
            "User-Agent": "papertrader"})
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.load(r)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                if exc.code in (429, 500, 502, 503) and attempt < self.retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                if exc.code == 401:
                    raise GroqError("Groq API key rejected (401): check GROQ_API_KEY") from None
                if exc.code == 429:
                    raise GroqError("Groq free-tier rate limit reached (429)") from None
                raise GroqError(f"Groq API error ({exc.code}): {detail}") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < self.retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise GroqError(f"network error reaching Groq: {exc}") from None
        raise GroqError("Groq request failed")
