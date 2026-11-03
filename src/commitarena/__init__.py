"""commitarena: replay real bug fixes from git history against any model, in a sandbox with no network."""

__version__ = "0.1.0"

from .agent import SYSTEM_PROMPT, AgentConfig  # noqa: E402
from .errors import (  # noqa: E402
    BudgetExceeded,
    CommitArenaError,
    DockerUnavailable,
    MissingAPIKey,
    ModelUnavailable,
    TaskError,
)
from .llm import Budget, ModelSpec  # noqa: E402
from .runner import ContextFile, RunResult, run_suite, run_task  # noqa: E402
from .sandbox import EnvSpec, SandboxConfig, ensure_docker  # noqa: E402
from .tasks import Task, load_tasks  # noqa: E402

__all__ = [
    "SYSTEM_PROMPT",
    "AgentConfig",
    "Budget",
    "BudgetExceeded",
    "CommitArenaError",
    "ContextFile",
    "DockerUnavailable",
    "EnvSpec",
    "MissingAPIKey",
    "ModelSpec",
    "ModelUnavailable",
    "RunResult",
    "SandboxConfig",
    "Task",
    "TaskError",
    "__version__",
    "ensure_docker",
    "load_tasks",
    "run_suite",
    "run_task",
]
