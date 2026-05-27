"""LLM client Protocol + production (Ollama / Qwen2.5) + test fake.

The :class:`LlmClient` Protocol is the same shape the embedder Protocol
took in step 10: an async method with a tightly-typed signature so the
production code, the fake, and any future implementation are all
interchangeable. The pattern is repeated for every Ollama-using stage
(score / arc-link / writer).

ADR-002 specifies the worker model: Qwen2.5 7B Instruct, quantised
Q4_K_M, served by local Ollama at ``http://localhost:11434``.
"""

from __future__ import annotations

import hashlib
from typing import Any, Protocol

import httpx

DEFAULT_OLLAMA_URL: str = "http://localhost:11434"
DEFAULT_WORKER_MODEL: str = "qwen2.5:7b-instruct-q4_K_M"
DEFAULT_TIMEOUT_SECONDS: float = 120.0
DEFAULT_TEMPERATURE: float = 0.1
DEFAULT_MAX_TOKENS: int = 512


# ── Protocol ───────────────────────────────────────────────────────────────


class LlmClient(Protocol):
    """Async LLM API. Returns the model's raw text completion."""

    async def generate(
        self,
        prompt: str,
        *,
        model: str = ...,
        temperature: float = ...,
        max_tokens: int = ...,
    ) -> str: ...

    async def generate_with_prefill(
        self,
        system: str,
        user: str,
        prefill: str,
        *,
        model: str = ...,
        temperature: float = ...,
        max_tokens: int = ...,
    ) -> str: ...


# ── Ollama implementation ──────────────────────────────────────────────────


class OllamaLlmClient:
    """Production client: POST to Ollama's ``/api/generate`` endpoint.

    Body shape::

        {
          "model": "qwen2.5:7b-instruct-q4_K_M",
          "prompt": "<full prompt>",
          "stream": false,
          "options": {"temperature": 0.1, "num_predict": 512}
        }

    Response shape::

        {"model": ..., "response": "<completion text>", "done": true, ...}

    Failures bubble up — the classifier translates them via its retry
    loop / final-fallback path.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._injected_client = client

    async def generate(
        self,
        prompt: str,
        *,
        model: str = DEFAULT_WORKER_MODEL,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        if self._injected_client is not None:
            return await self._call(
                self._injected_client, prompt, model, temperature, max_tokens
            )
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            return await self._call(
                client, prompt, model, temperature, max_tokens
            )

    async def generate_with_prefill(
        self,
        system: str,
        user: str,
        prefill: str,
        *,
        model: str = DEFAULT_WORKER_MODEL,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        if self._injected_client is not None:
            return await self._call_chat(
                self._injected_client, system, user, prefill,
                model, temperature, max_tokens,
            )
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            return await self._call_chat(
                client, system, user, prefill,
                model, temperature, max_tokens,
            )

    async def _call(
        self,
        client: httpx.AsyncClient,
        prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        response = await client.post(
            f"{self._base_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens,
                },
            },
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data.get("response", "")

    async def _call_chat(
        self,
        client: httpx.AsyncClient,
        system: str,
        user: str,
        prefill: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        response = await client.post(
            f"{self._base_url}/api/chat",
            json={
                "model": model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": prefill},
                ],
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens,
                },
            },
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data.get("message", {}).get("content", "")


# ── Fake (testing) implementation ──────────────────────────────────────────


class FakeLlmClient:
    """Deterministic LLM stand-in for tests.

    The ingester / embedder pattern: tests inject the fake via the
    classifier's constructor; the production Ollama client is never
    touched.

    Two modes of operation:

    * ``responses={prompt_substring: completion}`` — the first key whose
      substring appears in the prompt wins; useful for happy-path tests.
    * ``responder=callable`` — full control; called as
      ``responder(prompt, attempt_index)``. Useful for retry tests
      where the first attempt returns malformed JSON and the second
      attempt returns valid JSON.
    """

    def __init__(
        self,
        responses: dict[str, str] | None = None,
        responder: Any | None = None,
        default: str = "",
    ) -> None:
        self._responses = responses or {}
        self._responder = responder
        self._default = default
        self._call_log: list[str] = []
        self._prefill_log: list[str] = []
        self._attempts_per_prompt: dict[str, int] = {}

    async def generate(
        self,
        prompt: str,
        *,
        model: str = DEFAULT_WORKER_MODEL,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        self._call_log.append(prompt)
        key = _prompt_key(prompt)
        attempt = self._attempts_per_prompt.get(key, 0)
        self._attempts_per_prompt[key] = attempt + 1

        if self._responder is not None:
            return self._responder(prompt, attempt)
        for substring, response in self._responses.items():
            if substring in prompt:
                return response
        return self._default

    async def generate_with_prefill(
        self,
        system: str,
        user: str,
        prefill: str,
        *,
        model: str = DEFAULT_WORKER_MODEL,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        self._prefill_log.append(prefill)
        return await self.generate(
            user, model=model, temperature=temperature, max_tokens=max_tokens
        )

    @property
    def call_count(self) -> int:
        return len(self._call_log)

    @property
    def calls(self) -> list[str]:
        return list(self._call_log)

    @property
    def prefill_calls(self) -> list[str]:
        return list(self._prefill_log)


def _prompt_key(prompt: str) -> str:
    """Stable per-prompt identity for attempt-counting in FakeLlmClient."""
    return hashlib.md5(prompt.encode("utf-8"), usedforsecurity=False).hexdigest()


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_OLLAMA_URL",
    "DEFAULT_TEMPERATURE",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_WORKER_MODEL",
    "FakeLlmClient",
    "LlmClient",
    "OllamaLlmClient",
]
