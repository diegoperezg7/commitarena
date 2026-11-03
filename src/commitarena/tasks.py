"""Task model, task files and workspace construction from a git repository."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tarfile
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from .errors import TaskError
from .sandbox import EnvSpec

TASKS_FORMAT = 1

_TEST_DIRS = {"tests", "test", "testing"}
_DOC_RE = re.compile(
    r"(^|/)(docs?/|examples?/|benchmarks?/|CHANGE(S|LOG)|HISTORY|AUTHORS|NEWS)|\.(md|rst|txt)$", re.I
)
IGNORED_PARTS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def is_test_path(path: str) -> bool:
    """Files the agent may not change: anything under a tests dir, test_*.py, *_test.py, conftest.py."""
    p = PurePosixPath(path)
    if any(part in _TEST_DIRS for part in p.parts[:-1]):
        return True
    n = p.name
    return n == "conftest.py" or (n.endswith(".py") and (n.startswith("test_") or n.endswith("_test.py")))


def is_doc_path(path: str) -> bool:
    return bool(_DOC_RE.search(path))


def is_ignored(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return any(p in IGNORED_PARTS or p.endswith(".egg-info") for p in parts) or rel.endswith(".pyc")


@dataclass
class Task:
    id: str
    repo: str  # clone URL or local path
    commit: str
    parent: str
    date: str  # committer date, ISO 8601
    prompt: str
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    test_files: dict[str, str | None]  # test-side files at the fix commit (None = deleted)
    gold_patch: str  # human fix (non-test changes), unified diff
    image: str
    env: dict = field(default_factory=dict)  # EnvSpec fields
    source_files: list[str] = field(default_factory=list)
    title_hint: str = ""  # short neutral label (never the commit message)

    @property
    def env_spec(self) -> EnvSpec:
        return EnvSpec(**self.env) if self.env else EnvSpec()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Task:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def save_tasks(path: Path, tasks: list[Task], meta: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"format": TASKS_FORMAT, "meta": meta or {}, "tasks": [t.to_dict() for t in tasks]}
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False))


def load_tasks(path: str | os.PathLike, ids: list[str] | None = None) -> list[Task]:
    """Load mined tasks from a tasks.json written by ``commitarena mine``."""
    p = Path(path)
    if p.is_dir():
        p = p / "tasks.json"
    if not p.exists():
        raise TaskError(f"Task file not found: {p}. Create one with: commitarena mine <repo>")
    data = json.loads(p.read_text())
    if data.get("format") != TASKS_FORMAT:
        raise TaskError(f"{p} has unsupported format {data.get('format')!r} (expected {TASKS_FORMAT}).")
    tasks = [Task.from_dict(t) for t in data["tasks"]]
    if ids:
        wanted = set(ids)
        tasks = [t for t in tasks if t.id in wanted or any(t.id.endswith(i) for i in wanted)]
        if not tasks:
            raise TaskError(f"None of the task ids {sorted(wanted)} are in {p}.")
    return tasks


def load_tasks_meta(path: str | os.PathLike) -> dict:
    p = Path(path)
    if p.is_dir():
        p = p / "tasks.json"
    return json.loads(p.read_text()).get("meta", {})


# ---------------------------------------------------------------- git helpers


def git(repo: Path, *args: str, text: bool = True) -> str | bytes:
    cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if cp.returncode != 0:
        raise TaskError(f"git {' '.join(args)} failed: {cp.stderr.decode(errors='replace').strip()}")
    return cp.stdout.decode(errors="replace") if text else cp.stdout


def extract_commit(repo: Path, rev: str, dest: Path) -> None:
    data = git(repo, "archive", "--format=tar", rev, text=False)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")


def cache_dir() -> Path:
    base = os.environ.get("COMMITARENA_CACHE") or os.path.join(
        os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "commitarena"
    )
    return Path(base)


def repo_slug(repo: str) -> str:
    s = repo.rstrip("/").removesuffix(".git")
    s = s.split(":")[-1] if s.startswith("git@") else s
    parts = [p for p in re.split(r"[/\\]", s) if p][-2:]
    return re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-") or "repo"


def resolve_repo(repo: str) -> Path:
    """Local path as-is; URLs are cloned (once) into the cache."""
    local = Path(repo).expanduser()
    if local.exists():
        if not (local / ".git").exists() and not (local / "HEAD").exists():
            raise TaskError(f"{local} is not a git repository.")
        return local.resolve()
    dest = cache_dir() / "repos" / repo_slug(repo)
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        cp = subprocess.run(["git", "clone", "-q", repo, str(dest)], capture_output=True)
        if cp.returncode != 0:
            raise TaskError(f"git clone {repo} failed: {cp.stderr.decode(errors='replace').strip()}")
    return dest


def write_files(root: Path, files: dict[str, str | None]) -> None:
    for rel, content in files.items():
        target = root / rel
        if content is None:
            if target.exists():
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


def init_workspace_git(root: Path) -> None:
    """Fresh one-commit history so the agent can use git diff without seeing the real history."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "-A"],
        ["-c", "user.name=commitarena", "-c", "user.email=commitarena@localhost",
         "commit", "-q", "--no-gpg-sign", "-m", "task start"],
    ):  # fmt: skip
        subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True, env=env)


def build_workspace(task: Task, dest: Path, with_git: bool = True) -> Path:
    """Repo at the parent commit + the fix commit's test files. This is what the agent sees."""
    repo = resolve_repo(task.repo)
    extract_commit(repo, task.parent, dest)
    write_files(dest, task.test_files)
    if with_git:
        init_workspace_git(dest)
    return dest


def tree_files(root: Path) -> dict[str, Path]:
    out = {}
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            rel = p.relative_to(root).as_posix()
            if not is_ignored(rel):
                out[rel] = p
    return out


def changed_files(before: Path, after: Path) -> dict[str, bytes | None]:
    """Files that differ between two trees: rel path -> new bytes (None = deleted)."""
    a, b = tree_files(before), tree_files(after)
    out: dict[str, bytes | None] = {}
    for rel in sorted(set(a) | set(b)):
        if rel not in b:
            out[rel] = None
        elif rel not in a or a[rel].read_bytes() != b[rel].read_bytes():
            out[rel] = b[rel].read_bytes()
    return out


def unified_diff(before: Path, after: Path, only: list[str] | None = None) -> str:
    """git diff --no-index between two trees, restricted to `only` paths if given."""
    import difflib

    a, b = tree_files(before), tree_files(after)
    paths = sorted(set(a) | set(b)) if only is None else sorted(only)
    chunks: list[str] = []
    for rel in paths:
        try:
            old = a[rel].read_text().splitlines(keepends=True) if rel in a else []
            new = b[rel].read_text().splitlines(keepends=True) if rel in b else []
        except UnicodeDecodeError:
            chunks.append(f"Binary file {rel} differs\n")
            continue
        if old == new:
            continue
        diff = difflib.unified_diff(
            old, new, f"a/{rel}" if rel in a else "/dev/null", f"b/{rel}" if rel in b else "/dev/null", n=3
        )
        text = "".join(line if line.endswith("\n") else line + "\n" for line in diff)
        chunks.append(f"diff --git a/{rel} b/{rel}\n{text}")
    return "".join(chunks)
