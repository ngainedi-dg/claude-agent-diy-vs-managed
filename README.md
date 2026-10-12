# Claude agent: DIY harness vs. Managed Agents

The same autonomous data-analyst agent, built twice: once on the Claude Messages API with a hand-written agent loop, sandbox and session store, and once on Anthropic Managed Agents. Both get the same model, task and dataset. A common evaluator scores what they produce.

**The question:** how much of the engineering around an autonomous agent does a managed runtime actually take off your plate?

This is a small experiment, not a benchmark. It does not ask which agent is smarter (both are Claude). It asks what you have to build, run and debug yourself in each case.

**How it was built:** by one person working with an AI coding assistant (Claude, in Claude Code). The assistant wrote the code, tests and docs. The person set the brief, approved every paid run and every push, and made the calls on scope and results. The [engineering log](docs/engineering-log.md) is written in the assistant's voice: "I" is the assistant, "the user" is the repository owner. Because of this, timings in the repo are not estimates of what a human team would need.

## What the agent does

It receives a CSV of 9,980 synthetic sales transactions and one instruction: explore the data, find trends and anomalies, investigate anything suspicious, chart it, and write a report. It decides for itself what to look at, writes and runs Python, reads the results and fixes its own errors. It must produce `report.md`, `analysis.py`, `charts/` and a schema-checked `summary.json`.

The dataset is generated from a fixed seed with 10 injected anomalies and data defects, 1 ambiguous pattern (negative quantities that could be returns), and 3 legitimate patterns that should not be flagged as problems. The answer key is kept in a separate directory and never given to an agent.

## The two implementations

| | DIY (`diy/`) | Managed (`managed/`) |
|---|---|---|
| Agent loop | hand-written against `client.messages.create` | runs on Anthropic's side |
| Code execution | Docker container: no network, non-root, read-only root, resource limits | Anthropic-hosted cloud sandbox |
| Sessions | SQLite, with crash recovery | server-side sessions |
| Tools | 5 custom tools | built-in toolset (bash, read, write, ...) |
| Lines of code | 625 in 5 files, plus a Dockerfile | 246 in 1 file, plus `agent.md` and `environment.yaml` |

Neither uses an agent framework or the Claude Agent SDK. See [docs/architecture.md](docs/architecture.md).

## Findings so far

Five runs per implementation on `claude-sonnet-5-5`, alternating order. Medians; n is small, so read differences as indicative.

| | DIY | Managed |
|---|---|---|
| Runs completed | 5 of 5 | 5 of 5 |
| Valid artifacts and `summary.json` | 5 of 5 | 4 of 5 |
| Injected-anomaly recall | 0.90 | 0.90 (4 scored runs) |
| Active time | 171 s | 271 s |
| Cost per run | $0.34 | $0.56 |

- **Eliminated by Managed:** the agent loop, tool dispatch, the sandbox, and session storage. We wrote all four for DIY.
- **Not eliminated:** streaming and reconnecting to events, moving files in and out (the Files API drops directory structure), cost tracking, and checking the agent's output. The managed client still came to 246 lines.
- **Evaluation was the largest code in the repo** (evaluator and experiment runners: 815 lines, against 171 for the DIY loop) and where most of our bugs were.
- Quality was indistinguishable at this sample size. Managed was slower and costlier in our runs; we did not isolate why (different tools, sandbox and system prompt).
- Across seven fault-injection scenarios (one does not apply to Managed, which has no iteration limit), every scenario ended as intended on both sides. What differed was who enforced the limit: our loop, or the platform.

Details, evidence and the limits of these comparisons are in [docs/comparison.md](docs/comparison.md) and [docs/findings.md](docs/findings.md).

## Limitations

One task, one dataset, one model, five runs per side. The two sides are not identical tests: different toolsets, sandbox sizes and iteration limits. **Development time per component was not tracked**; only wall-clock time between commits exists, and it includes waiting. Raw runs (agents' reports, findings, traces) are not committed; a metrics-only summary of every run is in [docs/results/](docs/results/), and the numbers above are reproducible by rerunning. Managed Agents is a beta service and may change.

## Run it

You need Docker, [uv](https://docs.astral.sh/uv/), and an Anthropic API key with Managed Agents access. Copy `.env.example` to `.env.local` and set `ANTHROPIC_API_KEY`.

```bash
uv sync
uv run python task/generate_data.py --seed 42        # dataset and answer key
uv run pytest                                        # 156 tests, no API calls

uv run python -m diy.main --task task/instructions.md --dry-run   # sandbox check, free
uv run python -m managed.main --task task/instructions.md --dry-run   # creates the agent and environment, runs nothing

uv run python -m diy.main --task task/instructions.md             # paid, about $0.35
uv run python -m managed.main --task task/instructions.md         # paid, about $0.55
uv run python -m evaluation.evaluate --results results/ --run-analysis

uv run python -m experiments.run_comparison --repetitions 5 --yes # paid, about $5
uv run python -m experiments.failure_scenarios --yes              # paid, about $1.50
uv run python -m experiments.run_comparison --report-only         # rebuild the report, free
uv run python -m experiments.export_results                       # metrics-only summary into docs/results/
```

The two experiment runners print an estimate and need `--yes`. The single-run commands start spending as soon as they run; each has a cost cap (default $3.00). On WSL with the repository on the Windows drive, set `UV_PROJECT_ENVIRONMENT` to a path on the Linux filesystem; it cut test time from about 90 seconds to about 4.

The managed run creates a real agent, environment and sessions in your Anthropic account. Nothing is archived or deleted automatically.

## Layout

| Path | Contents |
|---|---|
| `task/` | dataset generator, shared task and system prompts, output schema |
| `diy/` | loop, tools, Docker sandbox, SQLite sessions |
| `managed/` | agent and environment definitions, the client |
| `evaluation/` | answer-key matching, numeric-claim check, metrics, analysis re-run |
| `experiments/` | repeated-run and fault-injection runners, LOC counter |
| `docs/` | brief, architecture, comparison, findings, engineering log; `docs/results/` holds metrics-only run data |

The [engineering log](docs/engineering-log.md) was written as the work happened, including corrections to earlier entries. Read it for what went wrong, not only what was built.

## License

MIT. See [LICENSE](LICENSE).
