"""Model providers. One small interface, so the agent runs on Meta's API, an open-weights deployment, or a test script.

Every hosted provider used here speaks the OpenAI-compatible chat completions protocol, so a single HTTP client
covers them; only the base URL, key and model name change. Configuration comes from environment variables and
keys are never logged.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

Message = dict[str, Any]  # {"role": "system" | "user" | "assistant", "content": str | list of parts}


class Provider(Protocol):
    name: str
    usage: "Usage"

    def complete(self, messages: list[Message], *, temperature: float = 0.0, max_tokens: int = 4000) -> str: ...


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0

    def add(self, prompt: int, completion: int, seconds: float) -> None:
        self.calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.seconds += seconds


class ProviderError(RuntimeError):
    pass


@dataclass
class OpenAICompatible:
    """POST {base_url}/chat/completions with a bearer key."""
    base_url: str
    api_key: str
    model: str
    name: str = "openai-compatible"
    timeout: float = 120.0
    usage: Usage = field(default_factory=Usage)

    def complete(self, messages: list[Message], *, temperature: float = 0.0, max_tokens: int = 4000) -> str:
        data = self._post(messages, temperature, max_tokens)
        choice = (data.get("choices") or [{}])[0]
        if not (choice.get("message") or {}).get("content") and choice.get("finish_reason") == "length":
            # Reasoning models think in hidden tokens first; if the thinking used the whole budget, the visible answer
            # is empty. Ask once more with more room rather than return nothing.
            data = self._post(messages, temperature, min(max_tokens * 3, 16000))
            choice = (data.get("choices") or [{}])[0]
        try:
            return choice["message"]["content"] or ""
        except (KeyError, TypeError):
            raise ProviderError(f"{self.name} returned an unexpected response: {str(data)[:300]}") from None

    def _post(self, messages: list[Message], temperature: float, max_tokens: int) -> dict[str, Any]:
        body = json.dumps({"model": self.model, "messages": messages, "temperature": temperature,
                           "max_tokens": max_tokens}).encode()
        req = urllib.request.Request(self.base_url.rstrip("/") + "/chat/completions", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        started = time.monotonic()
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                detail = e.read()[:500].decode("utf-8", "replace")
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise ProviderError(f"{self.name} returned HTTP {e.code}: {detail}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise ProviderError(f"{self.name} unreachable: {e}") from None
        u = data.get("usage") or {}
        self.usage.add(u.get("prompt_tokens", 0), u.get("completion_tokens", 0), time.monotonic() - started)
        return data


@dataclass
class ScriptedProvider:
    """A provider for tests and offline demos: replies come from a list or a function of the messages."""
    replies: list[str] | Callable[[list[Message]], str]
    name: str = "scripted"
    usage: Usage = field(default_factory=Usage)
    seen: list[list[Message]] = field(default_factory=list)

    def complete(self, messages: list[Message], *, temperature: float = 0.0, max_tokens: int = 4000) -> str:
        self.seen.append(messages)
        self.usage.add(0, 0, 0.0)
        if callable(self.replies):
            return self.replies(messages)
        if not self.replies:
            raise ProviderError("scripted provider ran out of replies")
        return self.replies.pop(0)


# Known endpoints. The base URL and model can be overridden by environment variable; the key always comes from one.
PRESETS = {
    # Meta Model API (dev.meta.ai/docs): OpenAI-compatible. Standard mode, whose inputs are not used to train Meta's
    # models; the "-contributor" model ids are cheaper but may be trained on, so they are not the default.
    "meta": {"base_url_env": "META_API_BASE_URL", "key_env": "META_API_KEY", "model_env": "META_MODEL",
             "base_url": "https://api.meta.ai/v1", "model": "muse-spark-1.3"},
    "baseten": {"base_url_env": "BASETEN_BASE_URL", "key_env": "BASETEN_API_KEY", "model_env": "BASETEN_MODEL",
                "base_url": "https://inference.baseten.co/v1", "model": "meta-models/muse-spark-open-1026"},
    "custom": {"base_url_env": "VEA_BASE_URL", "key_env": "VEA_API_KEY", "model_env": "VEA_MODEL", "base_url": "", "model": ""},
}


def from_env(preset: str) -> OpenAICompatible:
    if preset not in PRESETS:
        raise ProviderError(f"unknown provider {preset}; choose one of {', '.join(PRESETS)}")
    p = PRESETS[preset]
    base_url = os.environ.get(p["base_url_env"]) or p["base_url"]
    model = os.environ.get(p["model_env"]) or p["model"]
    key = os.environ.get(p["key_env"], "")
    missing = [n for n, v in ((p["base_url_env"], base_url), (p["model_env"], model), (p["key_env"], key)) if not v]
    if missing:
        raise ProviderError(f"provider {preset} needs these environment variables: {', '.join(missing)}")
    return OpenAICompatible(base_url=base_url, api_key=key, model=model, name=preset)
