"""Mine validated FAIL_TO_PASS tasks from a repository's git history (Python + pytest)."""

from __future__ import annotations

import shutil
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .sandbox import EnvSpec, Sandbox, SandboxConfig, build_image, ensure_docker, image_exists
from .tasks import (
    Task,
    build_workspace,
    extract_commit,
    git,
    is_doc_path,
    is_test_path,
    repo_slug,
    resolve_repo,
    unified_diff,
)
from .testrun import run_tests

MAX_TEST_DIFF_CHARS = 12000


@dataclass
class Candidate:
    sha: str
    parent: str
    date: str
    test_paths: list[str]
    source_paths: list[str]
    source_lines: int


def find_candidates(
    repo: Path,
    rev: str = "HEAD",
    since: str | None = None,
    until: str | None = None,
    limit: int = 200,
    max_source_files: int = 4,
    max_source_lines: int = 200,
) -> tuple[list[Candidate], Counter]:
    """Non-merge commits that change both test files and .py source files."""
    args = ["log", "--no-merges", "--format=%H%x09%P%x09%cI", f"-n{limit}"]
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    args.append(rev)
    skipped: Counter = Counter()
    out: list[Candidate] = []
    for line in git(repo, *args).splitlines():
        sha, parents, date = line.split("\t")
        if not parents or " " in parents:
            skipped["root or merge commit"] += 1
            continue
        numstat = git(repo, "diff", "--numstat", parents, sha)
        tests, sources, lines = [], [], 0
        for row in numstat.splitlines():
            added, deleted, path = row.split("\t", 2)
            if is_test_path(path):
                tests.append(path)
            elif path.endswith(".py") and not is_doc_path(path):
                sources.append(path)
                if added != "-":
                    lines += int(added) + int(deleted)
        if not tests or not sources:
            skipped["does not touch both tests and source"] += 1
            continue
        if len(sources) > max_source_files or lines > max_source_lines:
            skipped["fix too large"] += 1
            continue
        out.append(Candidate(sha, parents, date, tests, sources, lines))
    return out, skipped


def _test_side_files(repo: Path, cand: Candidate) -> dict[str, str | None]:
    files: dict[str, str | None] = {}
    names = git(repo, "diff", "--name-status", "--no-renames", cand.parent, cand.sha)
    for row in names.splitlines():
        status, path = row.split("\t", 1)
        if not is_test_path(path):
            continue
        if status.startswith("D"):
            files[path] = None
        else:
            try:
                files[path] = git(repo, "show", f"{cand.sha}:{path}", text=False).decode()
            except UnicodeDecodeError:
                continue
    return files


def build_prompt(f2p: list[str], test_diff: str, messages: dict[str, str], test_paths: list[str]) -> str:
    """Task description from the failing tests only. The commit message is never used."""
    shown = f2p[:25]
    lines = [
        "The repository in /work has a bug or a missing behavior. These tests currently fail and must pass "
        "after your change:",
        "",
        *[f"- {t}" for t in shown],
    ]
    if len(f2p) > len(shown):
        lines.append(f"- ... and {len(f2p) - len(shown)} more")
    if len(test_diff) > MAX_TEST_DIFF_CHARS:
        test_diff = test_diff[:MAX_TEST_DIFF_CHARS] + "\n... [test diff truncated]\n"
    lines += [
        "",
        "The tests were written to describe the expected behavior:",
        "",
        "```diff",
        test_diff.rstrip(),
        "```",
    ]
    msgs = [f"{t}:\n{messages[t][:800]}" for t in shown[:3] if t in messages]
    if msgs:
        lines += ["", "Current failure output (truncated):", "", "```", "\n\n".join(msgs), "```"]
    py_tests = [p for p in test_paths if p.endswith(".py")]
    lines += [
        "",
        "Change the source code, not the tests: test files are restored before scoring. Do not break the "
        "rest of the test suite.",
    ]
    if py_tests:
        lines.append(f"Run the relevant tests with: python -m pytest -q {' '.join(py_tests)}")
    return "\n".join(lines)


def mine(
    repo: str,
    *,
    out: Path,
    rev: str = "HEAD",
    since: str | None = None,
    until: str | None = None,
    scan: int = 200,
    max_tasks: int = 10,
    env: EnvSpec | None = None,
    sandbox: SandboxConfig | None = None,
    test_timeout: int = 600,
    on_event: Callable[[str, dict], None] | None = None,
) -> tuple[list[Task], dict]:
    """Find candidate commits and keep those whose new tests fail before the fix and pass after it."""
    from .tasks import save_tasks

    emit = on_event or (lambda kind, data: None)
    ensure_docker()
    env = env or EnvSpec()
    sandbox = sandbox or SandboxConfig(command_timeout=test_timeout)
    path = resolve_repo(repo)
    slug = repo_slug(repo)
    tag = env.image_tag(slug)
    work = Path(tempfile.mkdtemp(prefix="commitarena-mine-"))
    try:
        if not image_exists(tag):
            emit("build", {"image": tag})
            snap = work / "head"
            extract_commit(path, rev, snap)
            build_image(tag, env, snap, work)
        cands, skipped = find_candidates(path, rev, since, until, scan)
        emit("candidates", {"count": len(cands), "skipped": dict(skipped)})
        tasks: list[Task] = []
        rejected: Counter = Counter()
        for cand in cands:
            if len(tasks) >= max_tasks:
                break
            emit("validate", {"sha": cand.sha[:10], "date": cand.date})
            test_files = _test_side_files(path, cand)
            stub = Task(
                id="", repo=repo, commit=cand.sha, parent=cand.parent, date=cand.date, prompt="",
                fail_to_pass=[], pass_to_pass=[], test_files=test_files, gold_patch="", image=tag,
            )  # fmt: skip
            before = build_workspace(stub, work / f"{cand.sha[:10]}-before", with_git=False)
            after = work / f"{cand.sha[:10]}-after"
            extract_commit(path, cand.sha, after)
            with Sandbox(tag, sandbox) as sb:
                sb.copy_in(before)
                res_before = run_tests(sb, test_timeout)
            with Sandbox(tag, sandbox) as sb:
                sb.copy_in(after)
                res_after = run_tests(sb, test_timeout)
            reason = None
            if not res_after.ok or not res_before.ok:
                reason = "test run crashed or timed out"
            f2p = sorted(res_after.passed() - res_before.passed())
            p2p = sorted(res_after.passed() & res_before.passed())
            broken = res_before.passed() - res_after.passed()
            if reason is None and not f2p:
                reason = "no test goes from failing to passing"
            if reason is None and broken:
                reason = "fix breaks previously passing tests"
            if reason:
                rejected[reason] += 1
                emit("reject", {"sha": cand.sha[:10], "reason": reason})
            else:
                parent_tree = work / f"{cand.sha[:10]}-parent"
                extract_commit(path, cand.parent, parent_tree)
                test_diff = unified_diff(parent_tree, after, only=sorted(test_files))
                gold_paths = [
                    p for p in set(_all_changed(path, cand)) if not is_test_path(p) and not is_doc_path(p)
                ]
                gold = unified_diff(before, after, only=gold_paths)
                task = Task(
                    id=f"{slug}-{cand.sha[:8]}",
                    repo=repo,
                    commit=cand.sha,
                    parent=cand.parent,
                    date=cand.date,
                    prompt=build_prompt(f2p, test_diff, res_before.messages, sorted(test_files)),
                    fail_to_pass=f2p,
                    pass_to_pass=p2p,
                    test_files=test_files,
                    gold_patch=gold,
                    image=tag,
                    env=env.__dict__.copy(),
                    source_files=cand.source_paths,
                    title_hint=", ".join(cand.source_paths),
                )
                tasks.append(task)
                emit("task", {"id": task.id, "f2p": len(f2p), "p2p": len(p2p)})
                shutil.rmtree(parent_tree, ignore_errors=True)
            shutil.rmtree(before, ignore_errors=True)
            shutil.rmtree(after, ignore_errors=True)
        meta = {
            "repo": repo,
            "rev": rev,
            "image": tag,
            "env": env.__dict__.copy(),
            "candidates": len(cands),
            "skipped": dict(skipped),
            "rejected": dict(rejected),
            "validated": len(tasks),
        }
        save_tasks(out, tasks, meta)
        return tasks, meta
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _all_changed(repo: Path, cand: Candidate) -> list[str]:
    return [
        p for p in git(repo, "diff", "--name-only", "--no-renames", cand.parent, cand.sha).splitlines() if p
    ]
