"""Count application lines of code, the same way for every implementation.

    uv run python -m experiments.loc

Method: physical lines of Python that are not blank, not comment-only and not part of a
docstring. Counted per implementation directory (diy/, managed/, evaluation/, task/), with
tests/ reported separately. Excluded: generated data, lockfiles, Markdown and other
documentation, Dockerfiles and YAML (listed as configuration files instead).
A smaller number is not evidence of a better design; it is one input to the comparison.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GROUPS = ["diy", "managed", "evaluation", "task", "experiments", "tests"]
CONFIG_SUFFIXES = {".yaml", ".yml", ".toml", ".json"}
CONFIG_NAMES = {"Dockerfile", "agent.md"}  # agent.md is configuration (an agent definition), not documentation


def count_lines(path: Path) -> int:
    source = path.read_text(encoding="utf-8")
    doc_lines = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                doc_lines.update(range(body[0].lineno, body[0].end_lineno + 1))
    return sum(1 for n, line in enumerate(source.splitlines(), 1)
               if line.strip() and not line.strip().startswith("#") and n not in doc_lines)


def measure(group: str) -> dict:
    base = ROOT / group
    files = sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts and "data" not in p.parts)
    config = sorted(p for p in base.rglob("*") if p.is_file() and "data" not in p.parts and "__pycache__" not in p.parts
                    and not p.name.startswith(".") and (p.suffix in CONFIG_SUFFIXES or p.name in CONFIG_NAMES) and "schema" not in p.name)
    return {"files": {str(p.relative_to(ROOT)): count_lines(p) for p in files},
            "config_files": [str(p.relative_to(ROOT)) for p in config]}


def main():
    print(f"{'group':<14}{'python files':>13}{'lines of code':>15}   configuration files")
    for g in GROUPS:
        if not (ROOT / g).is_dir():
            continue
        m = measure(g)
        print(f"{g:<14}{len(m['files']):>13}{sum(m['files'].values()):>15}   {', '.join(m['config_files']) or '-'}")
        if g in ("diy", "managed"):
            for name, n in m["files"].items():
                print(f"    {name:<34}{n:>6}")


if __name__ == "__main__":
    main()
