"""Static HTML report and leaderboard aggregation."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from . import __version__
from .tasks import load_tasks

# Upper bounds used when no --cutoff is given: the model's public release date.
# Anything committed after it cannot be in its training data. Override per run with --cutoff.
DEFAULT_CUTOFFS: dict[str, str] = {
    "ollama/qwen3:4b": "2025-04-29",  # Qwen3 release (Qwen blog, 2025-04-29)
    "ollama/qwen2.5-coder:7b": "2024-09-19",  # Qwen2.5-Coder release
    "mistral/codestral-latest": "2025-07-31",  # codestral-2508, "released end of July 2025" (Mistral docs)
    "mistral/codestral-2508": "2025-07-31",
    "mistral/mistral-small-latest": "2026-03-01",  # mistral-small-2603 (v26.03)
    "mistral/mistral-small-2603": "2026-03-01",
    "mistral/mistral-large-4": "2026-10-01",  # Mistral Large 4, v26.10 (Mistral docs)
    "mistral/mistral-large-4-0": "2026-10-01",
    "ollama/qwen3:4b-16k": "2025-04-29",  # same weights as qwen3:4b, larger context window
}


def _split():
    return {"passed": 0, "total": 0}


def leaderboard(results: list[dict], cutoffs: dict[str, str]) -> list[dict]:
    """Aggregate per model. 'before' = tasks dated before the model's cutoff (may be memorized)."""
    rows: dict[str, dict] = {}
    for r in results:
        m = rows.setdefault(
            r["model"],
            {"model": r["model"], "passed": 0, "total": 0, "cost": 0.0, "tokens": 0, "time": 0.0, "steps": 0,
             "before": _split(), "after": _split(), "cutoff": cutoffs.get(r["model"])},
        )  # fmt: skip
        m["total"] += 1
        m["passed"] += int(bool(r["passed"]))
        m["cost"] += r.get("cost_usd", 0.0)
        m["tokens"] += r.get("tokens_in", 0) + r.get("tokens_out", 0)
        m["time"] += r.get("wall_time_s", 0.0)
        m["steps"] += r.get("steps", 0)
        cut = cutoffs.get(r["model"])
        if cut and r.get("task_date"):
            side = "before" if r["task_date"][:10] < cut else "after"
            m[side]["total"] += 1
            m[side]["passed"] += int(bool(r["passed"]))
    out = []
    for m in rows.values():
        n = m["total"] or 1
        m["rate"] = m["passed"] / n
        m["avg_time"] = m["time"] / n
        m["avg_steps"] = m["steps"] / n
        out.append(m)
    out.sort(key=lambda m: (-m["rate"], m["cost"]))
    return out


def load_run(run_dir: Path, cutoff_overrides: dict[str, str] | None = None) -> dict:
    meta = json.loads((run_dir / "run.json").read_text()) if (run_dir / "run.json").exists() else {}
    results = [
        json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines() if line.strip()
    ]
    tasks = load_tasks(run_dir / "tasks.json") if (run_dir / "tasks.json").exists() else []
    cutoffs = {**DEFAULT_CUTOFFS, **meta.get("cutoffs", {}), **(cutoff_overrides or {})}
    return {"meta": meta, "results": results, "tasks": tasks, "cutoffs": cutoffs}


def _commit_url(repo: str, sha: str) -> str | None:
    r = repo.removesuffix(".git").rstrip("/")
    if r.startswith("https://github.com/") or r.startswith("https://gitlab.com/"):
        return f"{r}/commit/{sha}"
    return None


def build_payload(run_dir: Path, cutoff_overrides: dict[str, str] | None = None) -> dict:
    data = load_run(run_dir, cutoff_overrides)
    used = {r["task_id"] for r in data["results"]}
    tasks = [
        {
            "id": t.id,
            "date": t.date,
            "files": t.source_files,
            "f2p": len(t.fail_to_pass),
            "p2p": len(t.pass_to_pass),
            "gold": t.gold_patch,
            "url": _commit_url(t.repo, t.commit),
            "commit": t.commit[:10],
        }
        for t in sorted(data["tasks"], key=lambda t: t.date)
        if t.id in used
    ]
    models = sorted({r["model"] for r in data["results"]})
    return {
        "meta": {**data["meta"], "report_version": __version__},
        "tasks": tasks,
        "models": models,
        "cutoffs": {m: data["cutoffs"].get(m) for m in models},
        "board": leaderboard(data["results"], data["cutoffs"]),
        "results": [
            {k: r.get(k) for k in ("task_id", "model", "passed", "fail_to_pass_passed", "fail_to_pass_total",
                                   "pass_to_pass_failed", "cost_usd", "tokens_in", "tokens_out", "steps",
                                   "wall_time_s", "stop_reason", "patch", "ignored_test_edits")}
            for r in data["results"]
        ],
    }  # fmt: skip


def render(payload: dict) -> str:
    template = resources.files("commitarena").joinpath("report_template.html").read_text()
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return template.replace("/*__DATA__*/{}", blob)


def write_report(
    run_dir: Path, out: Path | None = None, cutoff_overrides: dict[str, str] | None = None
) -> Path:
    out = out or run_dir / "report.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(build_payload(run_dir, cutoff_overrides)))
    return out


def demo_dir() -> Path:
    return Path(str(resources.files("commitarena").joinpath("demo")))
