"""Does an agent's analysis.py run on the original CSV, on its own?

The script is agent-written, so it only ever runs inside a container: no network, non-root, read-only
root filesystem, resource limits. Each implementation's script hardcodes the path it was given, so the
CSV is mounted at both known locations. The script runs in a scratch copy; nothing in results/ changes.
"""

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

IMAGE = "diy-agent-sandbox"  # built from diy/Dockerfile: Python 3.12, pandas, numpy, matplotlib
IMAGE_CONTEXT = Path(__file__).resolve().parent.parent / "diy"
CSV_NAME = "sales_transactions.csv"
DATA_PATHS = [f"/data/{CSV_NAME}", f"/mnt/session/uploads/{CSV_NAME}"]
OUTPUT_PATHS = ["/workspace", "/mnt/session/outputs"]  # all point at the same scratch directory


def docker_ready() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def ensure_image() -> None:
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode != 0:
        subprocess.run(["docker", "build", "-t", IMAGE, str(IMAGE_CONTEXT)], check=True, capture_output=True)


def run_analysis(outputs: Path, csv: Path, timeout: int = 300) -> dict:
    """Run <outputs>/analysis.py in a throwaway container. Returns exit code, duration and what it produced."""
    script = outputs / "analysis.py"
    if not script.is_file():
        return {"ran": False, "reason": "analysis.py missing"}
    if not docker_ready():
        return {"ran": False, "reason": "docker unavailable"}
    ensure_image()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "work"
        work.mkdir()
        work.chmod(0o777)
        shutil.copy(script, work / "analysis.py")
        cmd = ["docker", "run", "--rm", "--init", "--network", "none", "--user", "1000:1000", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp:rw,size=256m,mode=1777",
               "--memory", "2g", "--cpus", "1", "--pids-limit", "256", "-e", "HOME=/tmp", "-e", "MPLCONFIGDIR=/tmp/mpl",
               "-w", "/workspace"]
        cmd += [a for p in DATA_PATHS for a in ("-v", f"{csv.resolve()}:{p}:ro")]
        cmd += [a for p in OUTPUT_PATHS for a in ("-v", f"{work}:{p}:rw")]
        cmd += [IMAGE, "timeout", "-s", "KILL", str(timeout), "python", "analysis.py"]
        start = time.monotonic()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout + 60)
        except subprocess.TimeoutExpired:
            return {"ran": True, "ok": False, "timed_out": True, "duration_s": round(time.monotonic() - start, 1)}
        produced = sorted(str(p.relative_to(work)) for p in work.rglob("*") if p.is_file() and p.name != "analysis.py")
        return {"ran": True, "ok": r.returncode == 0, "exit_code": r.returncode, "timed_out": r.returncode == 137,
                "duration_s": round(time.monotonic() - start, 1), "files_produced": len(produced),
                "stderr_tail": r.stderr[-400:] if r.returncode else ""}
