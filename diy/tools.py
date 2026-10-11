"""The five tools the agent can call, and the dispatcher that runs them in the sandbox.

Every tool returns (text, is_error). Errors the model can act on (bad path,
Python traceback, timeout) come back as tool results so it can retry or change
approach. Failures of the sandbox itself raise SandboxError and are handled by
the loop as infrastructure failures.
"""

import posixpath
import shlex

from jsonschema import Draft202012Validator

from diy.sandbox import DATA_MOUNT, WORKSPACE, Sandbox, truncate

DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 300
ALLOWED_COMMANDS = {"ls", "cat", "head", "tail", "wc", "grep", "find", "du", "sort", "uniq",
                    "mkdir", "cp", "mv", "rm", "file", "python"}

TOOLS = [
    {
        "name": "list_files",
        "description": f"List files in a directory of the sandbox. The input data is under {DATA_MOUNT} (read-only); "
                       f"your working directory is {WORKSPACE}.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": [],
                         "additionalProperties": False},
    },
    {
        "name": "read_file",
        "description": "Read a text file, or a bounded slice of it. Returns at most `limit` characters starting at `offset`.",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0, "description": "Character offset to start at (default 0)."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20000, "description": "Maximum characters (default 5000)."}},
            "required": ["path"], "additionalProperties": False},
    },
    {
        "name": "write_file",
        "description": f"Create or overwrite a text file under {WORKSPACE}. Parent directories are created.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                         "required": ["path", "content"], "additionalProperties": False},
    },
    {
        "name": "execute_python",
        "description": "Run Python 3 code in the sandbox (pandas, numpy, matplotlib and the standard library are installed; "
                       f"there is no network). The working directory is {WORKSPACE}. State does not persist between "
                       "calls; each call is a fresh interpreter. Returns stdout, stderr, exit code and duration.",
        "input_schema": {"type": "object", "properties": {
            "code": {"type": "string"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_S}},
            "required": ["code"], "additionalProperties": False},
    },
    {
        "name": "run_command",
        "description": "Run one command in the sandbox without a shell (no pipes or redirection). Allowed programs: "
                       + ", ".join(sorted(ALLOWED_COMMANDS)) + ".",
        "input_schema": {"type": "object", "properties": {
            "command": {"type": "string", "description": "For example: ls -la /workspace"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_S}},
            "required": ["command"], "additionalProperties": False},
    },
]


class ToolError(Exception):
    """A problem with how the model called a tool. Reported back to the model."""


def resolve_path(path: str, *, write: bool = False) -> str:
    """Normalise a model-supplied path and confine it to the workspace (or the data mount, read-only)."""
    if not path:
        raise ToolError("path is empty")
    full = posixpath.normpath(path if path.startswith("/") else posixpath.join(WORKSPACE, path))
    roots = [WORKSPACE] if write else [WORKSPACE, DATA_MOUNT]
    if not any(full == r or full.startswith(r + "/") for r in roots):
        where = WORKSPACE if write else f"{WORKSPACE} or {DATA_MOUNT}"
        raise ToolError(f"path {path!r} is outside the permitted area ({where})")
    return full


def format_exec(result) -> str:
    parts = [f"exit_code: {result.exit_code}", f"duration: {result.duration_s:.1f}s"]
    if result.stdout:
        parts.append("--- stdout ---\n" + result.stdout)
    if result.stderr:
        parts.append("--- stderr ---\n" + result.stderr)
    return "\n".join(parts)


class Dispatcher:
    def __init__(self, sandbox: Sandbox):
        self.sandbox = sandbox
        self.validators = {t["name"]: Draft202012Validator(t["input_schema"]) for t in TOOLS}
        self.python_calls = 0

    @property
    def definitions(self):
        return TOOLS

    def validate(self, name: str, args) -> None:
        if name not in self.validators:
            raise ToolError(f"unknown tool {name!r}; available: {', '.join(self.validators)}")
        errors = sorted(self.validators[name].iter_errors(args), key=lambda e: list(e.path))
        if errors:
            raise ToolError("invalid arguments: " + "; ".join(e.message for e in errors))

    def run(self, name: str, args: dict) -> tuple[str, bool]:
        try:
            self.validate(name, args)
            return getattr(self, f"_{name}")(**args)
        except ToolError as e:
            return str(e), True

    # --- tools ---------------------------------------------------------

    def _list_files(self, path: str = WORKSPACE):
        r = self.sandbox.exec(["ls", "-la", resolve_path(path)], timeout=30)
        return format_exec(r), r.exit_code != 0

    def _read_file(self, path: str, offset: int = 0, limit: int = 5000):
        script = ("import sys\np, off, lim = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])\n"
                  "text = open(p, encoding='utf-8', errors='replace').read()\n"
                  "sys.stdout.write(text[off:off + lim])\n"
                  "if off + lim < len(text): sys.stderr.write(f'[truncated: {len(text)} characters total]')\n")
        r = self.sandbox.exec(["python", "-c", script, resolve_path(path), str(offset), str(limit)], timeout=30)
        if r.exit_code != 0:
            return r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "could not read file", True
        return r.stdout + (("\n" + r.stderr) if r.stderr else ""), False

    def _write_file(self, path: str, content: str):
        full = resolve_path(path, write=True)
        r = self.sandbox.exec(["sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", full],
                              input=content, timeout=30)
        if r.exit_code != 0:
            return f"could not write {full}: {r.stderr.strip()}", True
        return f"wrote {len(content)} characters to {full}", False

    def _execute_python(self, code: str, timeout_seconds: int = DEFAULT_TIMEOUT_S):
        self.python_calls += 1
        script = f"{WORKSPACE}/_scratch/step_{self.python_calls:03d}.py"  # a real file, so tracebacks show source lines
        _, err = self._write_file(script, code)
        if err:
            return "could not stage code for execution", True
        r = self.sandbox.exec(["python", script], timeout=timeout_seconds)
        return format_exec(r), r.exit_code != 0

    def _run_command(self, command: str, timeout_seconds: int = DEFAULT_TIMEOUT_S):
        try:
            argv = shlex.split(command)
        except ValueError as e:
            raise ToolError(f"could not parse command: {e}")
        if not argv:
            raise ToolError("command is empty")
        if argv[0] not in ALLOWED_COMMANDS:
            raise ToolError(f"{argv[0]!r} is not allowed. Allowed: {', '.join(sorted(ALLOWED_COMMANDS))}")
        r = self.sandbox.exec(argv, timeout=timeout_seconds)
        return format_exec(r), r.exit_code != 0
