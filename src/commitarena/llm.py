"""One OpenAI-compatible client with provider presets, pricing and a spend cap."""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from typing import Any

from .errors import BudgetExceeded, CommitArenaError, MissingAPIKey, ModelUnavailable


def _ollama_base() -> str:
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").strip().rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    if host.startswith("http://0.0.0.0"):
        host = host.replace("0.0.0.0", "localhost", 1)
    return host + "/v1"


# provider -> (base_url, env var with the key or None for local)
PROVIDERS: dict[str, tuple[str, str | None]] = {
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "ollama": ("", None),  # resolved at call time from OLLAMA_HOST
}

# USD per million tokens (input, output). List prices from docs.mistral.ai/inference/pricing,
# checked 2026-11-03. Override with --price or ModelSpec(price=...).
PRICES: dict[str, tuple[float, float]] = {
    "mistral/codestral-latest": (0.30, 0.90),
    "mistral/codestral-2508": (0.30, 0.90),
    "mistral/mistral-small-latest": (0.15, 0.60),
    "mistral/mistral-small-2603": (0.15, 0.60),
    "mistral/mistral-large-4": (0.70, 2.10),
    "mistral/mistral-large-4-0": (0.70, 2.10),
}


@dataclass(frozen=True)
class ModelSpec:
    """A model id of the form ``provider/name``, e.g. ``ollama/qwen3:4b``."""

    provider: str
    name: str
    price: tuple[float, float] | None = None  # USD per 1M (input, output)

    @classmethod
    def parse(cls, spec: str, price: tuple[float, float] | None = None) -> ModelSpec:
        if "/" not in spec:
            raise CommitArenaError(
                f"Model '{spec}' must look like provider/name, e.g. ollama/qwen3:4b or "
                f"mistral/codestral-latest. Providers: {', '.join(PROVIDERS)}."
            )
        provider, name = spec.split("/", 1)
        if provider not in PROVIDERS:
            raise CommitArenaError(f"Unknown provider '{provider}'. Use one of: {', '.join(PROVIDERS)}.")
        return cls(provider, name, price)

    @property
    def id(self) -> str:
        return f"{self.provider}/{self.name}"

    @property
    def is_local(self) -> bool:
        return self.provider == "ollama"

    def unit_price(self) -> tuple[float, float] | None:
        if self.price is not None:
            return self.price
        if self.is_local:
            return (0.0, 0.0)
        return PRICES.get(self.id)


@dataclass
class Budget:
    """Shared spend cap in USD. Checked before every model call; local models cost 0."""

    max_cost: float = 1.0
    spent: float = 0.0

    def check(self) -> None:
        if self.spent >= self.max_cost:
            raise BudgetExceeded(self.spent, self.max_cost)

    def add(self, usd: float) -> None:
        self.spent += usd


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    calls: int = 0

    def add(self, other: Usage) -> None:
        self.tokens_in += other.tokens_in
        self.tokens_out += other.tokens_out
        self.cost_usd += other.cost_usd
        self.calls += other.calls


@dataclass
class ChatClient:
    """Thin wrapper over the `openai` SDK. Retries 429/5xx with exponential backoff."""

    model: ModelSpec
    budget: Budget = field(default_factory=Budget)
    max_retries: int = 12
    timeout: float = 600.0
    _client: Any = None
    _connected: bool = False  # a local server that answered once gets retries on later drops

    def __post_init__(self) -> None:
        if self.model.unit_price() is None and self.model.provider != "openrouter":
            raise CommitArenaError(
                f"No price known for {self.model.id}, so the budget cannot be enforced. Pass "
                f"--price '{self.model.id}=IN,OUT' (USD per million tokens)."
            )

    def _sdk(self):
        if self._client is None:
            from openai import OpenAI

            base, env = PROVIDERS[self.model.provider]
            if self.model.provider == "ollama":
                base, key = _ollama_base(), "ollama"
                self.timeout = max(self.timeout, 1800.0)  # a busy local server can be slow
            else:
                key = os.environ.get(env or "", "")
                if not key:
                    raise MissingAPIKey(self.model.provider, env or "")
            self._client = OpenAI(base_url=base, api_key=key, timeout=self.timeout, max_retries=0)
        return self._client

    def chat(self, messages: list[dict], tools: list[dict] | None = None, **kw) -> tuple[Any, Usage]:
        import openai

        self.budget.check()
        client = self._sdk()
        extra: dict[str, Any] = {}
        if self.model.provider == "openrouter":
            extra["extra_body"] = {"usage": {"include": True}}
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            try:
                resp = client.chat.completions.create(
                    model=self.model.name, messages=messages, tools=tools, **extra, **kw
                )
                self._connected = True
                break
            except openai.AuthenticationError as e:
                _, env = PROVIDERS[self.model.provider]
                raise CommitArenaError(
                    f"{self.model.provider} rejected the API key in {env} (401). Check the key."
                ) from e
            except openai.NotFoundError as e:
                hint = (
                    f" Pull it first: ollama pull {self.model.name}"
                    if self.model.is_local
                    else " Check the model name with the provider."
                )
                raise ModelUnavailable(f"Model '{self.model.id}' was not found.{hint}") from e
            except openai.APIConnectionError as e:
                if self.model.is_local and (attempt >= 3 or not self._connected):
                    raise ModelUnavailable(
                        f"Cannot reach Ollama at {_ollama_base()}. Start it with 'ollama serve' "
                        f"or set OLLAMA_HOST."
                    ) from e
                if attempt == self.max_retries:
                    raise ModelUnavailable(f"Cannot reach {self.model.provider}: {e}") from e
            except (openai.RateLimitError, openai.InternalServerError, openai.APITimeoutError) as e:
                if attempt == self.max_retries:
                    raise ModelUnavailable(
                        f"{self.model.provider} kept failing after {attempt + 1} attempts "
                        f"(last error: {type(e).__name__}). Try later or use a local model."
                    ) from e
            except openai.APIStatusError as e:
                if e.status_code == 402:
                    raise CommitArenaError(f"{self.model.provider} reports no credit left (402).") from e
                raise CommitArenaError(f"{self.model.provider} error {e.status_code}: {e.message}") from e
            time.sleep(min(delay, 90.0) * (0.75 + random.random() / 2))
            delay *= 2
        usage = self._usage(resp)
        self.budget.add(usage.cost_usd)
        return resp, usage

    def _usage(self, resp: Any) -> Usage:
        u = getattr(resp, "usage", None)
        tin = int(getattr(u, "prompt_tokens", 0) or 0)
        tout = int(getattr(u, "completion_tokens", 0) or 0)
        cost = None
        if u is not None and self.model.provider == "openrouter":
            extra = getattr(u, "model_extra", None) or {}
            cost = extra.get("cost")
        if cost is None:
            pin, pout = self.model.unit_price() or (0.0, 0.0)
            cost = (tin * pin + tout * pout) / 1_000_000
        return Usage(tin, tout, float(cost), 1)
