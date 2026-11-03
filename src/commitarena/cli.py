"""Command line interface: mine, run, report, demo."""

from __future__ import annotations

import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .errors import CommitArenaError

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Your git history is the benchmark. Replay real bug fixes against any model, in a sandbox with "
    "no network.",
)
console = Console()
err = Console(stderr=True)


def _version(value: bool) -> None:
    if value:
        console.print(f"commitarena {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show the version.")
    ] = False,
) -> None:
    pass


def _parse_pairs(values: list[str] | None, what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in values or []:
        if "=" not in v:
            raise CommitArenaError(f"{what} must look like MODEL=VALUE, got '{v}'.")
        k, val = v.rsplit("=", 1)
        out[k.strip()] = val.strip()
    return out


def _csv(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


@app.command()
def mine(
    repo: Annotated[str, typer.Argument(help="Git URL or local path of a Python repo that uses pytest.")],
    out: Annotated[Path | None, typer.Option(help="Where to write tasks.json.")] = None,
    rev: Annotated[str, typer.Option(help="Branch or commit to walk back from.")] = "HEAD",
    since: Annotated[str | None, typer.Option(help="Only commits after this date (git --since).")] = None,
    until: Annotated[str | None, typer.Option(help="Only commits before this date (git --until).")] = None,
    scan: Annotated[int, typer.Option(help="How many recent commits to scan.")] = 200,
    max_tasks: Annotated[int, typer.Option(help="Stop after this many validated tasks.")] = 10,
    python: Annotated[str, typer.Option(help="Python version of the test image.")] = "3.12",
    setup: Annotated[
        str, typer.Option(help="Install command run once at image build time (with network).")
    ] = "pip install --no-cache-dir -e .",
    test_timeout: Annotated[int, typer.Option(help="Seconds allowed for one test-suite run.")] = 600,
) -> None:
    """Find commits that fix a bug with a test, and keep those whose new tests fail without the fix."""
    from .mine import mine as do_mine
    from .sandbox import EnvSpec
    from .tasks import repo_slug

    out = out or Path("tasks") / repo_slug(repo) / "tasks.json"

    def on_event(kind: str, d: dict) -> None:
        if kind == "build":
            console.print(f"[dim]building test image {d['image']} (network on, only for this step)[/dim]")
        elif kind == "candidates":
            console.print(f"{d['count']} candidate commits touch both source and tests")
        elif kind == "task":
            console.print(f"  [green]valid[/green]  {d['id']}  {d['f2p']} fail->pass, {d['p2p']} pass->pass")
        elif kind == "reject":
            console.print(f"  [dim]skip   {d['sha']}  {d['reason']}[/dim]")

    tasks, meta = do_mine(
        repo, out=out, rev=rev, since=since, until=until, scan=scan, max_tasks=max_tasks,
        env=EnvSpec(python=python, setup=setup), test_timeout=test_timeout, on_event=on_event,
    )  # fmt: skip
    console.print(f"\n[bold]{len(tasks)} validated tasks[/bold] written to {out}")
    if tasks:
        console.print(f"Next: commitarena run --tasks {out} --models ollama/qwen3:4b")


def results_table(results, cutoffs: dict[str, str] | None = None) -> Table:
    from .report import leaderboard

    rows = leaderboard([r.to_dict() if hasattr(r, "to_dict") else r for r in results], cutoffs or {})
    t = Table(title="Leaderboard", title_justify="left")
    for col, just in [
        ("model", "left"), ("pass", "right"), ("rate", "right"), ("before cutoff", "right"),
        ("after cutoff", "right"), ("cost $", "right"), ("tokens", "right"), ("avg time s", "right"),
        ("avg steps", "right"),
    ]:  # fmt: skip
        t.add_column(col, justify=just)

    def split(s):
        return f"{s['passed']}/{s['total']}" if s["total"] else "-"

    for r in rows:
        t.add_row(
            r["model"], f"{r['passed']}/{r['total']}", f"{r['rate'] * 100:.0f}%", split(r["before"]),
            split(r["after"]), f"{r['cost']:.4f}", f"{r['tokens']:,}", f"{r['avg_time']:.0f}",
            f"{r['avg_steps']:.1f}",
        )  # fmt: skip
    return t


@app.command()
def run(
    tasks: Annotated[Path, typer.Option(help="tasks.json from `commitarena mine`.")],
    models: Annotated[str, typer.Option(help="Comma-separated provider/model, e.g. ollama/qwen3:4b.")],
    out: Annotated[Path | None, typer.Option(help="Run directory (resumable).")] = None,
    max_cost: Annotated[float, typer.Option(help="Stop before any call once this many USD are spent.")] = 1.0,
    task_ids: Annotated[str | None, typer.Option(help="Comma-separated task ids to run.")] = None,
    limit: Annotated[int | None, typer.Option(help="Run only the first N tasks.")] = None,
    max_steps: Annotated[int, typer.Option(help="Model calls per attempt.")] = 30,
    max_time: Annotated[int, typer.Option(help="Seconds per attempt.")] = 1200,
    context_file: Annotated[
        Path | None, typer.Option(help="AGENTS.md/CLAUDE.md to inject into the attempt.")
    ] = None,
    context_mode: Annotated[str, typer.Option(help="both | repo | prompt")] = "both",
    strip_context_files: Annotated[
        bool, typer.Option(help="Remove the repo's own AGENTS.md / CLAUDE.md first.")
    ] = False,
    cutoff: Annotated[
        list[str] | None, typer.Option(help="Training cutoff, MODEL=YYYY-MM-DD (repeatable).")
    ] = None,
    price: Annotated[
        list[str] | None, typer.Option(help="Price override, MODEL=IN,OUT in USD per 1M tokens (repeatable).")
    ] = None,
    cpus: Annotated[float, typer.Option(help="CPU limit of the sandbox.")] = 2.0,
    memory: Annotated[str, typer.Option(help="Memory limit of the sandbox.")] = "2g",
    command_timeout: Annotated[int, typer.Option(help="Seconds per agent command.")] = 120,
) -> None:
    """Run every task against every model with the same minimal agent, then write report.html."""
    from .agent import AgentConfig
    from .llm import Budget, ChatClient, ModelSpec
    from .report import write_report
    from .runner import ContextFile, run_suite
    from .sandbox import SandboxConfig, ensure_docker
    from .tasks import load_tasks

    prices = {}
    for k, v in _parse_pairs(price, "--price").items():
        try:
            pin, pout = (float(x) for x in v.split(","))
        except ValueError as e:
            raise CommitArenaError(f"--price for {k} must be IN,OUT (numbers), got '{v}'.") from e
        prices[k] = (pin, pout)
    specs = [ModelSpec.parse(m, prices.get(m)) for m in _csv(models)]
    if not specs:
        raise CommitArenaError("--models is empty.")
    for s in specs:  # validate keys and prices before any work
        ChatClient(s)._sdk()
        if s.is_local:
            _warn_ollama_context(s.name)
    task_list = load_tasks(tasks, _csv(task_ids) or None)
    if limit:
        task_list = task_list[:limit]
    ensure_docker()
    out = out or Path("runs") / datetime.now().strftime("%Y%m%d-%H%M%S")
    ctx = ContextFile.from_path(context_file, context_mode) if context_file else None
    budget = Budget(max_cost=max_cost)
    cutoffs = _parse_pairs(cutoff, "--cutoff")
    console.print(
        f"{len(task_list)} tasks x {len(specs)} models, sandbox --network none, {cpus} CPU, {memory}, "
        f"budget ${max_cost:.2f}. Results in {out}"
    )

    def on_start(task, model):
        console.print(f"[bold]{model.id}[/bold]  {task.id}  [dim]({task.date[:10]})[/dim]")

    def on_step(step, command):
        first = command.strip().splitlines()[0] if command.strip() else ""
        console.print(f"  [dim]{step:>2} $ {first[:100]}[/dim]")

    def on_result(r):
        mark = "[green]PASS[/green]" if r.passed else "[red]FAIL[/red]"
        console.print(
            f"  {mark}  {r.fail_to_pass_passed}/{r.fail_to_pass_total} fail->pass, "
            f"{r.pass_to_pass_failed} broken, {r.steps} steps, {r.wall_time_s:.0f}s, "
            f"${r.cost_usd:.4f} ({r.stop_reason})  total ${budget.spent:.4f}"
        )

    results, abort = run_suite(
        task_list, specs, out, budget=budget, context=ctx, strip_context_files=strip_context_files,
        agent_config=AgentConfig(max_steps=max_steps, max_wall_time=max_time),
        sandbox_config=SandboxConfig(cpus=cpus, memory=memory, command_timeout=command_timeout),
        cutoffs=cutoffs, on_result=on_result, on_start=on_start, on_step=on_step,
    )  # fmt: skip
    report = write_report(out)
    console.print(results_table(results, _load_cutoffs(out)))
    console.print(f"Spent ${budget.spent:.4f}. Report: {report}")
    if abort:
        err.print(f"[red]{abort}[/red]")
        raise typer.Exit(2)


def _warn_ollama_context(name: str) -> None:
    """Ollama's OpenAI endpoint ignores num_ctx; a small default window silently drops the task text."""
    import json
    import os
    import urllib.request

    from .llm import _ollama_base

    if os.environ.get("OLLAMA_CONTEXT_LENGTH"):
        return
    try:
        req = urllib.request.Request(
            _ollama_base().removesuffix("/v1") + "/api/show", data=json.dumps({"model": name}).encode()
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            info = json.load(r)
    except Exception:  # noqa: BLE001 - the real call reports connection problems
        return
    if "num_ctx" not in (info.get("parameters") or ""):
        err.print(
            f"[yellow]warning:[/yellow] ollama/{name} has no num_ctx set, so Ollama may serve it with a small "
            f"default context (often 4096 tokens) and drop the task text. Create a variant:\n"
            f"  printf 'FROM {name}\\nPARAMETER num_ctx 16384\\n' > Modelfile && "
            f"ollama create {name.split(':')[0]}:16k -f Modelfile"
        )


def _load_cutoffs(run_dir: Path) -> dict[str, str]:
    import json

    from .report import DEFAULT_CUTOFFS

    meta = json.loads((run_dir / "run.json").read_text()) if (run_dir / "run.json").exists() else {}
    return {**DEFAULT_CUTOFFS, **meta.get("cutoffs", {})}


@app.command()
def report(
    run_dir: Annotated[Path, typer.Argument(help="Run directory written by `commitarena run`.")],
    out: Annotated[Path | None, typer.Option(help="Output HTML (default: <run_dir>/report.html).")] = None,
    cutoff: Annotated[
        list[str] | None, typer.Option(help="Override a training cutoff, MODEL=YYYY-MM-DD.")
    ] = None,
    open_browser: Annotated[bool, typer.Option("--open", help="Open the report in a browser.")] = False,
) -> None:
    """Render the static HTML report: leaderboard, task x model matrix, cutoff split, diffs."""
    from .report import load_run, write_report

    if not (run_dir / "results.jsonl").exists():
        raise CommitArenaError(f"No results.jsonl in {run_dir}. Run `commitarena run` first.")
    path = write_report(run_dir, out, _parse_pairs(cutoff, "--cutoff"))
    data = load_run(run_dir)
    console.print(
        results_table(data["results"], {**_load_cutoffs(run_dir), **_parse_pairs(cutoff, "--cutoff")})
    )
    console.print(f"Report: {path}")
    if open_browser:
        webbrowser.open(path.resolve().as_uri())


@app.command()
def demo(
    out: Annotated[Path, typer.Option(help="Where to write the demo report.")] = Path(
        "commitarena-demo.html"
    ),
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open it in a browser.")] = True,
) -> None:
    """Show a RECORDED real run (no keys, no Docker): leaderboard in the terminal plus the HTML report."""
    from .report import demo_dir, load_run, write_report

    d = demo_dir()
    data = load_run(d)
    m = data["meta"]
    console.print(
        f"[bold]Recorded result[/bold] (not a live run): {m.get('updated_at', '')[:10]}, commitarena "
        f"{m.get('version')}, models {', '.join(m.get('models', []))}, on {m.get('hardware', '?')}."
    )
    console.print(f"Repo: {m.get('repo', '')} ({m.get('repo_license', '')})")
    console.print(results_table(data["results"], data["cutoffs"]))
    path = write_report(d, out)
    console.print(f"Report: {path.resolve()}")
    console.print("Run it yourself: commitarena mine <repo> && commitarena run --tasks ... --models ...")
    if open_browser:
        webbrowser.open(path.resolve().as_uri())


def _sigterm(signum, frame):  # noqa: ARG001
    raise KeyboardInterrupt


def main() -> None:
    import signal

    signal.signal(signal.SIGTERM, _sigterm)  # so sandboxes are removed on kill
    try:
        app()
    except CommitArenaError as e:
        err.print(f"[red]error:[/red] {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        err.print("[yellow]interrupted[/yellow]")
        sys.exit(130)
