"""Docker sandbox: every command runs in a container started with ``--network none``."""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .errors import DockerUnavailable

WORKDIR = "/work"


def _docker(
    *args: str, input: bytes | None = None, timeout: float | None = None
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["docker", *args], input=input, capture_output=True, timeout=timeout)
    except FileNotFoundError as e:
        raise DockerUnavailable(
            "Docker is not installed (no 'docker' command on PATH). Install Docker Desktop or Docker Engine."
        ) from e


def ensure_docker() -> None:
    """Raise DockerUnavailable with an actionable message if the daemon is not reachable."""
    try:
        cp = _docker("info", "--format", "{{.ServerVersion}}", timeout=20)
    except subprocess.TimeoutExpired as e:
        raise DockerUnavailable("Docker did not answer within 20 s. Is Docker Desktop starting up?") from e
    if cp.returncode != 0:
        raise DockerUnavailable(
            "Docker is not running (docker info failed). Start Docker Desktop or the docker service and retry."
        )


@dataclass(frozen=True)
class SandboxConfig:
    cpus: float = 2.0
    memory: str = "2g"
    pids_limit: int = 512
    command_timeout: int = 120  # seconds per command
    network: str = "none"


@dataclass(frozen=True)
class EnvSpec:
    """How to build the per-repo test image (built once, with network; tasks run without)."""

    python: str = "3.12"
    setup: str = "pip install --no-cache-dir -e ."
    extra_packages: str = "pytest==9.2.0"

    def dockerfile(self) -> str:
        return (
            f"FROM python:{self.python}-slim\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends git "
            "&& rm -rf /var/lib/apt/lists/*\n"
            f"COPY snapshot/ {WORKDIR}/\n"
            f"WORKDIR {WORKDIR}\n"
            f"RUN pip install --no-cache-dir {self.extra_packages} && {self.setup}\n"
            # Keep installed deps (and the editable .pth pointing at /work); drop the snapshot.
            f"RUN find {WORKDIR} -mindepth 1 -delete\n"
            f"ENV PYTHONPATH={WORKDIR}:{WORKDIR}/src PYTHONDONTWRITEBYTECODE=1 PIP_NO_INDEX=1\n"
        )

    def image_tag(self, slug: str) -> str:
        h = hashlib.sha256(self.dockerfile().encode()).hexdigest()[:10]
        return f"commitarena-env-{slug}:{h}"


def image_exists(tag: str) -> bool:
    return _docker("image", "inspect", tag).returncode == 0


def build_image(tag: str, env: EnvSpec, snapshot_dir: Path, build_root: Path) -> None:
    """Build the env image from a snapshot of the repo (network is used only here)."""
    ensure_docker()
    ctx = build_root / f"ctx-{uuid.uuid4().hex[:8]}"
    shutil.copytree(snapshot_dir, ctx / "snapshot", ignore=shutil.ignore_patterns(".git"))
    (ctx / "Dockerfile").write_text(env.dockerfile())
    try:
        cp = _docker("build", "-q", "-t", tag, str(ctx), timeout=1800)
    finally:
        shutil.rmtree(ctx, ignore_errors=True)
    if cp.returncode != 0:
        tail = cp.stderr.decode(errors="replace")[-2000:]
        raise DockerUnavailable(f"Building image {tag} failed:\n{tail}")


@dataclass
class ExecResult:
    exit_code: int
    output: str
    timed_out: bool = False


def _tar_dir(src: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for p in sorted(src.rglob("*")):
            tar.add(p, arcname=str(p.relative_to(src)), recursive=False)
    return buf.getvalue()


class Sandbox:
    """A long-lived container with no network. Use as a context manager."""

    def __init__(self, image: str, config: SandboxConfig | None = None):
        self.image = image
        self.config = config or SandboxConfig()
        self.name = f"commitarena-{uuid.uuid4().hex[:12]}"
        self.started = False

    def __enter__(self) -> Sandbox:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        c = self.config
        cp = _docker(
            "run", "-d", "--rm", "--name", self.name, "--label", "commitarena=sandbox",
            "--network", c.network,
            "--cpus", str(c.cpus), "--memory", c.memory, "--memory-swap", c.memory,
            "--pids-limit", str(c.pids_limit),
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-w", WORKDIR, self.image, "sleep", "infinity",
            timeout=120,
        )  # fmt: skip
        if cp.returncode != 0:
            raise DockerUnavailable(
                f"Could not start sandbox container: {cp.stderr.decode(errors='replace')}"
            )
        self.started = True

    def stop(self) -> None:
        if self.started:
            _docker("rm", "-f", self.name, timeout=60)
            self.started = False

    def copy_in(self, src: Path) -> None:
        """Copy a host directory into /work. Files end up owned by root (no CAP_DAC_OVERRIDE needed)."""
        cp = _docker(
            "exec", "-i", self.name, "tar", "-xf", "-", "-C", WORKDIR, "--no-same-owner",
            input=_tar_dir(src), timeout=300,
        )  # fmt: skip
        if cp.returncode != 0:
            raise DockerUnavailable(f"Copy into sandbox failed: {cp.stderr.decode(errors='replace')}")

    def copy_out(self, dest: Path) -> None:
        cp = _docker("exec", self.name, "tar", "-cf", "-", "-C", WORKDIR, ".", timeout=300)
        if cp.returncode != 0:
            raise DockerUnavailable(f"Copy out of sandbox failed: {cp.stderr.decode(errors='replace')}")
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(cp.stdout)) as tar:
            tar.extractall(dest, filter="data")

    def exec(self, command: str, timeout: int | None = None) -> ExecResult:
        t = int(timeout or self.config.command_timeout)
        if "\x00" in command:
            return ExecResult(126, "error: the command contains a null byte; remove it and try again")
        try:
            cp = _docker(
                "exec", self.name, "timeout", "-s", "KILL", str(t), "bash", "-c", "exec 2>&1\n" + command,
                timeout=t + 30,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            return ExecResult(124, f"[command timed out after {t}s]", True)
        out = cp.stdout.decode(errors="replace") + cp.stderr.decode(errors="replace")
        if cp.returncode in (124, 137) and t:
            return ExecResult(cp.returncode, out + f"\n[command killed: time limit {t}s]", True)
        return ExecResult(cp.returncode, out)
