"""Run one task with one model, score it with the original tests, and run whole suites."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .agent import AgentConfig, run_agent
from .errors import BudgetExceeded, CommitArenaError
from .llm import Budget, ChatClient, ModelSpec
from .sandbox import Sandbox, SandboxConfig, build_image, ensure_docker, image_exists
from .tasks import (
    Task,
    changed_files,
    extract_commit,
    init_workspace_git,
    is_test_path,
    load_tasks,
    resolve_repo,
    save_tasks,
    unified_diff,
    write_files,
)
from .testrun import run_tests

CONTEXT_NAMES = ("AGENTS.md", "CLAUDE.md")


@dataclass
class ContextFile:
    """A context file (AGENTS.md, CLAUDE.md, ...) injected into the attempt.

    in_repo: write it at the repo root inside the sandbox.
    in_prompt: append its content to the system prompt.
    """

    content: str
    name: str = "AGENTS.md"
    in_repo: bool = True
    in_prompt: bool = True

    @classmethod
    def from_path(cls, path: str | os.PathLike, mode: str = "both", name: str | None = None) -> ContextFile:
        if mode not in ("both", "repo", "prompt"):
            raise CommitArenaError("--context-mode must be one of: both, repo, prompt")
        p = Path(path)
        return cls(p.read_text(), name or p.name, mode in ("both", "repo"), mode in ("both", "prompt"))


@dataclass
class RunResult:
    task_id: str
    model: str
    passed: bool
    fail_to_pass_passed: int = 0
    fail_to_pass_total: int = 0
    pass_to_pass_failed: int = 0
    pass_to_pass_total: int = 0
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    steps: int = 0
    tool_calls: int = 0
    wall_time_s: float = 0.0
    stop_reason: str = ""
    error: str = ""
    patch: str = ""  # model's diff on non-test files (what is scored)
    ignored_test_edits: list[str] = field(default_factory=list)
    task_date: str = ""
    context_file: str | None = None
    started_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> RunResult:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def ensure_task_image(task: Task) -> None:
    if image_exists(task.image):
        return
    repo = resolve_repo(task.repo)
    tmp = Path(tempfile.mkdtemp(prefix="commitarena-img-"))
    try:
        extract_commit(repo, task.commit, tmp / "snap")
        build_image(task.image, task.env_spec, tmp / "snap", tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def score_workspace(task: Task, pristine: Path, agent_tree: Path, sandbox: SandboxConfig, test_timeout: int):
    """Apply only the agent's non-test changes onto the pristine workspace and run the suite."""
    changes = changed_files(pristine, agent_tree)
    code = {p: c for p, c in changes.items() if not is_test_path(p)}
    ignored = sorted(p for p in changes if is_test_path(p))
    scored = pristine.parent / "scored"
    shutil.copytree(pristine, scored, ignore=shutil.ignore_patterns(".git"))
    for rel, data in code.items():
        target = scored / rel
        if data is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    patch = unified_diff(pristine, scored, only=sorted(code))
    with Sandbox(task.image, sandbox) as sb:
        sb.copy_in(scored)
        outcomes = run_tests(sb, test_timeout)
    return outcomes, patch, ignored


def run_task(
    task: Task,
    model: str | ModelSpec,
    *,
    budget: Budget | None = None,
    context: ContextFile | None = None,
    strip_context_files: bool = False,
    agent_config: AgentConfig | None = None,
    sandbox_config: SandboxConfig | None = None,
    test_timeout: int = 600,
    on_step: Callable[[int, str], None] | None = None,
    keep_trajectory: list | None = None,
) -> RunResult:
    """Run one attempt of `task` with `model` and score it. Raises CommitArenaError on fatal setup errors."""
    spec = model if isinstance(model, ModelSpec) else ModelSpec.parse(model)
    budget = budget or Budget()
    sb_cfg = sandbox_config or SandboxConfig()
    client = ChatClient(spec, budget)
    ensure_docker()
    ensure_task_image(task)
    started = datetime.now(UTC).isoformat(timespec="seconds")
    t0 = time.monotonic()
    tmp = Path(tempfile.mkdtemp(prefix="commitarena-run-"))
    try:
        pristine = tmp / "pristine"
        extract_commit(resolve_repo(task.repo), task.parent, pristine)
        write_files(pristine, task.test_files)
        if strip_context_files:
            for n in CONTEXT_NAMES:
                (pristine / n).unlink(missing_ok=True)
        system_extra = ""
        if context:
            if context.in_repo:
                (pristine / context.name).write_text(context.content)
            if context.in_prompt:
                system_extra = f"# Project instructions ({context.name})\n\n{context.content}"
        workspace = tmp / "workspace"
        shutil.copytree(pristine, workspace)
        init_workspace_git(workspace)
        agent_tree = tmp / "agent"
        with Sandbox(task.image, sb_cfg) as sb:
            sb.copy_in(workspace)
            outcome = run_agent(client, sb, task.prompt, agent_config, system_extra, on_step)
            sb.copy_out(agent_tree)
        if keep_trajectory is not None:
            keep_trajectory.extend(outcome.trajectory)
        outcomes, patch, ignored = score_workspace(task, pristine, agent_tree, sb_cfg, test_timeout)
        passed_set = outcomes.passed()
        f2p_ok = sum(1 for t in task.fail_to_pass if t in passed_set)
        p2p_bad = sum(1 for t in task.pass_to_pass if t not in passed_set)
        return RunResult(
            task_id=task.id,
            model=spec.id,
            passed=outcomes.ok and f2p_ok == len(task.fail_to_pass) and p2p_bad == 0,
            fail_to_pass_passed=f2p_ok,
            fail_to_pass_total=len(task.fail_to_pass),
            pass_to_pass_failed=p2p_bad,
            pass_to_pass_total=len(task.pass_to_pass),
            cost_usd=round(outcome.usage.cost_usd, 6),
            tokens_in=outcome.usage.tokens_in,
            tokens_out=outcome.usage.tokens_out,
            steps=outcome.steps,
            tool_calls=outcome.tool_calls,
            wall_time_s=round(time.monotonic() - t0, 1),
            stop_reason=outcome.stop_reason,
            error=outcome.error,
            patch=patch,
            ignored_test_edits=ignored,
            task_date=task.date,
            context_file=context.name if context else None,
            started_at=started,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def hardware() -> str:
    mem = ""
    try:
        if platform.system() == "Darwin":
            b = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout)
            chip = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True
            ).stdout.strip()
            mem = f"{chip}, {b // 2**30} GB RAM"
        elif Path("/proc/meminfo").exists():
            kb = int(Path("/proc/meminfo").read_text().split()[1])
            mem = f"{platform.processor() or platform.machine()}, {kb // 2**20} GB RAM"
    except Exception:  # noqa: BLE001 - best effort only
        pass
    return f"{platform.system()} {platform.machine()}" + (f" ({mem})" if mem else "")


def run_suite(
    tasks: list[Task],
    models: list[ModelSpec],
    out_dir: Path,
    *,
    budget: Budget,
    context: ContextFile | None = None,
    strip_context_files: bool = False,
    agent_config: AgentConfig | None = None,
    sandbox_config: SandboxConfig | None = None,
    cutoffs: dict[str, str] | None = None,
    on_result: Callable[[RunResult], None] | None = None,
    on_start: Callable[[Task, ModelSpec], None] | None = None,
    on_step: Callable[[int, str], None] | None = None,
) -> tuple[list[RunResult], str | None]:
    """Run every (task, model) pair sequentially. Resumable: pairs already in results.jsonl are skipped.

    Returns (results, abort_message). abort_message is set when the budget stopped the run.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "trajectories").mkdir(exist_ok=True)
    results_path = out_dir / "results.jsonl"
    done: dict[tuple[str, str], RunResult] = {}
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            if line.strip():
                r = RunResult.from_dict(json.loads(line))
                done[(r.task_id, r.model)] = r
    meta_path = out_dir / "run.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta.update(
        {
            "tool": "commitarena",
            "version": __version__,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "hardware": hardware(),
            "models": sorted({*meta.get("models", []), *(m.id for m in models)}),
            "cutoffs": {**meta.get("cutoffs", {}), **(cutoffs or {})},
            "agent": {k: v for k, v in asdict(agent_config or AgentConfig()).items() if k != "system_prompt"},
            "sandbox": asdict(sandbox_config or SandboxConfig()),
            "context_file": context.name if context else None,
            "strip_context_files": strip_context_files,
        }
    )
    meta.setdefault("created_at", meta["updated_at"])
    meta_path.write_text(json.dumps(meta, indent=1))
    tasks_by_id = {t.id: t for t in tasks}
    existing_tasks = out_dir / "tasks.json"
    if existing_tasks.exists():
        for t in load_tasks(existing_tasks):
            tasks_by_id.setdefault(t.id, t)
    save_tasks(existing_tasks, list(tasks_by_id.values()))
    results = list(done.values())
    abort = None
    for model in models:
        for task in tasks:
            if (task.id, model.id) in done:
                continue
            try:
                budget.check()
            except BudgetExceeded as e:
                return results, str(e)
            if on_start:
                on_start(task, model)
            traj: list = []
            res = run_task(
                task, model, budget=budget, context=context, strip_context_files=strip_context_files,
                agent_config=agent_config,
                sandbox_config=sandbox_config, on_step=on_step, keep_trajectory=traj,
            )  # fmt: skip
            results.append(res)
            with results_path.open("a") as f:
                f.write(json.dumps(res.to_dict(), ensure_ascii=False) + "\n")
            safe = model.id.replace("/", "_").replace(":", "_")
            (out_dir / "trajectories" / f"{task.id}__{safe}.json").write_text(
                json.dumps(traj, indent=1, ensure_ascii=False)
            )
            if on_result:
                on_result(res)
            if res.stop_reason == "budget_exceeded":
                return results, res.error
    return results, abort
