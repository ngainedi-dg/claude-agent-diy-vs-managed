"""Docker sandbox: one container per run, commands go in through `docker exec`.

Docker is process isolation on a shared kernel. It is not a production
sandbox for untrusted code. What it does give us here:

  - no network, no API key in the container's environment
  - non-root user, all capabilities dropped, no-new-privileges
  - read-only root filesystem; the only writable places are /workspace and /tmp
  - the dataset mounted read-only
  - CPU, memory and process-count limits
  - a hard timeout on every command, enforced inside the container
"""

import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

IMAGE = "diy-agent-sandbox"
DOCKERFILE_DIR = Path(__file__).resolve().parent
DATA_MOUNT = "/data"
WORKSPACE = "/workspace"
MAX_OUTPUT_CHARS = 10_000  # per stream; protects the model's context window


class SandboxError(Exception):
    """The sandbox itself failed (Docker, image, container), as opposed to the agent's code failing."""


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    timed_out: bool = False


def _docker(args, *, input=None, timeout=None):
    try:
        return subprocess.run(["docker", *args], input=input, capture_output=True, text=True,
                              errors="replace", timeout=timeout)
    except FileNotFoundError as e:
        raise SandboxError("docker CLI not found") from e


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} characters omitted] ...\n{text[-half:]}"


def ensure_image() -> None:
    if _docker(["image", "inspect", IMAGE]).returncode == 0:
        return
    r = _docker(["build", "-t", IMAGE, str(DOCKERFILE_DIR)])
    if r.returncode != 0:
        raise SandboxError(f"docker build failed:\n{r.stderr[-2000:]}")


class Sandbox:
    def __init__(self, data_file: Path, workspace: Path, *, memory="1g", cpus="1", pids=256):
        self.data_file = data_file.resolve()
        self.workspace = workspace.resolve()
        self.limits = dict(memory=memory, cpus=cpus, pids=pids)
        self.name = f"diy-agent-{uuid.uuid4().hex[:10]}"

    def start(self) -> None:
        ensure_image()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.workspace.chmod(0o777)  # the container user is not the host user on every setup
        r = _docker([
            "run", "-d", "--rm", "--name", self.name,
            "--network", "none",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only", "--tmpfs", "/tmp:rw,size=256m,mode=1777",
            "--memory", self.limits["memory"], "--cpus", self.limits["cpus"],
            "--pids-limit", str(self.limits["pids"]),
            "-e", "HOME=/tmp", "-e", "MPLCONFIGDIR=/tmp/mpl",
            "-v", f"{self.data_file}:{DATA_MOUNT}/{self.data_file.name}:ro",
            "-v", f"{self.workspace}:{WORKSPACE}:rw",
            IMAGE, "sleep", "infinity",
        ])
        if r.returncode != 0:
            raise SandboxError(f"could not start sandbox: {r.stderr.strip()}")

    def stop(self) -> None:
        _docker(["rm", "-f", self.name])

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def exec(self, argv: list[str], *, input: str | None = None, timeout: int = 120) -> ExecResult:
        """Run a command in the container. `timeout` is enforced by `timeout -s KILL` inside it."""
        cmd = ["exec", "-i", self.name, "timeout", "-s", "KILL", str(timeout), *argv]
        start = time.monotonic()
        try:
            r = _docker(cmd, input=input, timeout=timeout + 15)
        except subprocess.TimeoutExpired:
            raise SandboxError("docker exec did not return; the container may be wedged")
        duration = time.monotonic() - start
        if r.returncode == 137 and duration >= timeout - 1:  # SIGKILL from `timeout`
            return ExecResult(truncate(r.stdout), f"Killed: exceeded {timeout}s timeout.\n" + truncate(r.stderr),
                              137, duration, timed_out=True)
        if r.returncode == 125 or "No such container" in r.stderr:
            raise SandboxError(f"sandbox container unavailable: {r.stderr.strip()}")
        return ExecResult(truncate(r.stdout), truncate(r.stderr), r.returncode, duration)
