"""Estimate the cost of one DIY run before spending anything.

    uv run python -m experiments.estimate_cost

Measures the fixed prompt (system + tools + task) with the free count_tokens
endpoint, then projects cost for a few run shapes. The projection is a model of
how an agent run grows, not a measurement; the real run reports actual usage.
"""

import argparse
import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from diy.agent_loop import PRICES
from diy.main import DEFAULT_MODEL, ROOT, build_task
from diy.tools import TOOLS

# (name, turns, new context tokens added per turn, output tokens per turn including thinking)
SCENARIOS = [("short run", 12, 2500, 1200), ("typical run", 25, 3500, 1800), ("run that hits the 40-turn limit", 40, 5000, 2500)]


def project(model, base, turns, added, out):
    inp, outp, write, read = PRICES[model]
    total = 0.0
    for i in range(turns):
        context_before = base + i * added
        # previous context is read from cache; the newly added tokens are written to it
        total += (context_before * read + (added if i else base) * write + out * outp) / 1_000_000
    uncached = sum((base + i * added) * inp + out * outp for i in range(turns)) / 1_000_000
    return total, uncached


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default=DEFAULT_MODEL, choices=sorted(PRICES))
    a = ap.parse_args()
    load_dotenv(ROOT / ".env.local")
    load_dotenv(ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set")

    task = build_task(ROOT / "task" / "instructions.md", "sales_transactions.csv")
    system = (ROOT / "task" / "system.md").read_text(encoding="utf-8").strip()
    count = anthropic.Anthropic().messages.count_tokens(
        model=a.model, system=system, tools=TOOLS, messages=[{"role": "user", "content": task}])
    base = count.input_tokens
    inp, outp, write, read = PRICES[a.model]
    print(f"model {a.model}: ${inp}/MTok input, ${outp}/MTok output, ${write} cache write, ${read} cache read")
    print(f"fixed prompt (system + 5 tools + task): {base} tokens\n")
    print(f"{'scenario':<34}{'with caching':>14}{'without caching':>17}")
    for name, turns, added, out in SCENARIOS:
        cached, uncached = project(a.model, base, turns, added, out)
        print(f"{name + f' ({turns} turns)':<34}{f'${cached:.2f}':>14}{f'${uncached:.2f}':>17}")


if __name__ == "__main__":
    main()
