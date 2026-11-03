"""User-facing errors. The CLI prints these without a traceback."""

from __future__ import annotations


class CommitArenaError(Exception):
    """Base class. `str(err)` is a complete, actionable message."""


class MissingAPIKey(CommitArenaError):
    def __init__(self, provider: str, env_var: str):
        super().__init__(
            f"Missing API key for provider '{provider}'. Set the {env_var} environment variable "
            f"(for example in your shell or a .env file you source), or use a local model such as "
            f"'ollama/qwen3:4b'."
        )
        self.provider = provider
        self.env_var = env_var


class DockerUnavailable(CommitArenaError):
    pass


class ModelUnavailable(CommitArenaError):
    """Model not found, or the provider (e.g. Ollama) cannot be reached."""


class BudgetExceeded(CommitArenaError):
    def __init__(self, spent: float, max_cost: float):
        super().__init__(
            f"Budget exceeded: spent ${spent:.4f} of --max-cost ${max_cost:.2f}. Stopped before the next "
            f"model call. Partial results were kept; raise --max-cost to continue."
        )
        self.spent = spent
        self.max_cost = max_cost


class TaskError(CommitArenaError):
    pass
