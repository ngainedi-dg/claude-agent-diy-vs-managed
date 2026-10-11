# DIY vs. Managed Agents: comparison

Evidence for every number is listed at the end. Raw run results are not committed; they are reproducible with `uv run python -m experiments.run_comparison` (see the README). Sample sizes are small (5 runs per side, 1 per fault scenario), so differences are indicative, not statistically established.

## 1. Infrastructure responsibilities

Classification from the brief: **Eliminated** (no application code needed), **Simplified** (still needs integration or configuration, but much less), **Still required** (remains the developer's job).

| Capability | Verdict | What DIY needed | What Managed needed | Source |
|---|---|---|---|---|
| Agent loop | Eliminated | `agent_loop.py`, 171 lines (stop reasons, tool-result batching, thinking-block replay, truncation, retries) | nothing; the session is started with the task | `diy/agent_loop.py`, `managed/main.py` |
| Tool dispatch and validation | Eliminated for built-in tools | `tools.py`, 126 lines: schemas, path confinement, command allowlist | `agent_toolset_20260401` in `agent.md` | `diy/tools.py`, `managed/agent.md` |
| Code-execution sandbox | Eliminated; one policy decision remains | `sandbox.py` (80 lines) and a Dockerfile (10 lines), plus a Docker daemon | `environment.yaml` (6 lines); networking must be set explicitly because the API default is unrestricted | `diy/sandbox.py`, `managed/environment.yaml` |
| Session state | Eliminated storage; client reconnection remains | `sessions.py`, 144 lines: SQLite, recovery, redaction | server-side; we wrote `watch()` to reconnect and de-duplicate events | `diy/sessions.py`, `managed/main.py` |
| Context management | Eliminated | prompt caching enabled by hand; tool output truncated; no compaction needed (peak prompt 54,818 tokens) | built in; 0 compaction events in 5 runs | `run.json`, traces |
| Error handling | Simplified | API backoff, three error classes, an "outcome unknown" rule for interrupted calls | 0 `session.error` events in 568 events; our own client code still had bugs | `diy/agent_loop.py`, traces |
| Observability | Simplified | tracer, JSONL, SQLite, human-readable summary | event stream provided; we still wrote the trace writer and metrics | `diy/sessions.py`, `managed/main.py` |
| Execution limits | Simplified | iteration, wall-clock and estimated-cost limits in our loop | platform dollar budget and a platform tool timeout; no iteration limit; our wall-clock timeout | `diy/agent_loop.py`, scenarios |
| Artifact handling | Still required, and arguably harder on Managed | bind-mounted workspace | Files API: upload, list, download; directories flattened; the input shows up in the output listing | `managed/main.py` |
| Output validation | Still required on both | schema and rubric checks in `evaluation/` | same | `evaluation/` |
| Task design, prompts, permissions | Still required on both | | | |

## 2. Engineering effort

| Measure | DIY | Managed |
|---|---|---|
| Lines of code | 625 (5 files) | 246 (1 file) |
| Configuration files | Dockerfile, 10 lines | `agent.md` 17, `environment.yaml` 6 |
| Test functions | 47 | 19 |
| Third-party imports | anthropic, jsonschema, dotenv | anthropic, pyyaml, dotenv |
| System prerequisites | Docker daemon and image build | API key and beta access |

Lines are counted as non-blank, non-comment, non-docstring Python (`experiments/loc.py`). They are not a verdict: DIY includes crash recovery Managed does not need, and Managed includes reconnect, download and trace code.

**Development time was not tracked per component.** The "time spent" estimates in early log entries were written without a timer and have been withdrawn. The only measured time is wall-clock between commits (23:14 to 00:45 IST from brief to experiment tooling), which includes waiting for approvals and paid runs. First complete run: DIY about 20 minutes after the previous phase commit, Managed about 14, but Managed built on the dataset, evaluator and lessons from DIY. Not a fair head-to-head.

## 3. Code and operational complexity

**DIY.** The loop is small; the surrounding work is not. We hit and fixed: a container wedged by unreaped zombie processes (`--init`); Docker's own errors arriving as ordinary tool output; the scratch-script numbering restarting after a resume; and a narrow unclosed gap where a crash after a `max_tokens`-truncated reply would run a half-written tool call on resume. Operationally it needs a Docker daemon, and killing the client with SIGKILL left an orphaned container (no reaper was built).

**Managed.** Less to run, more to learn about its model: event types, the no-replay stream, a Files API that flattens directories. Our bugs were in the client: a crash after the session had finished (the input file is listed but not downloadable), and a trace that duplicated events on re-attach. Operationally, remote resources accumulate (agent, environment, sessions, files) and nothing cleans them up automatically.

## 4. Runtime behaviour (experiment, 5 runs per side)

| Measure (median) | DIY | Managed |
|---|---|---|
| Active time | 171 s | 271 s |
| Model calls / tool calls | 18 / 18 | 22 / 25 |
| Output tokens | 20,791 | 33,485 |
| Tool mix (all runs) | execute_python 77 of 89 | bash 91 of 120 |
| Peak prompt | up to 54,818 tokens | not reported |

Conditions differ: built-in bash/read/write against five custom tools, a sandbox up to 8 GB against 1 GB and 1 CPU, an iteration limit on one side, and explicit against automatic prompt caching.

## 5. Cost

| Median per run | DIY | Managed |
|---|---|---|
| Token cost (our estimate, one price table) | $0.34 | $0.56 |
| Platform runtime charge | not applicable | $0.006 |
| Total | $0.34 (range 0.33-0.45) | $0.56 (range 0.42-0.73) |

The platform's own reported list cost for Managed agrees (median $0.56). DIY local compute cost was not measured. Output tokens were about 60% of cost on both sides, cache writes about 30%, cache reads 10-13%. Experiment spend: $4.79. The cause of the 1.7x gap was not isolated.

## 6. Reliability

All 10 experiment runs completed. Valid artifacts: DIY 5 of 5, Managed 4 of 5 (one summary had a boolean where the schema wants a number or string). `analysis.py` reran in a clean container for all 10.

Fault scenarios (one run per side; faults enter through the task, so the tests are not identical):

| Scenario | DIY | Managed | Handled by |
|---|---|---|---|
| Syntax error | recovered | recovered | model |
| Missing input file | `MISSING` | `MISSING` (63 s; the agent ran `find /`) | model |
| Tool error | correct | correct | model |
| Tool timeout | `TIMEOUT`; our allowlist rejected the command, so the agent wrote its own timeout and our kill path was not exercised | `TIMEOUT`; platform stopped the command at 295 s | DIY: model; Managed: runtime |
| Budget cap $0.05 | stopped at $0.0503 | paused at $0.06 list cost | our loop / the runtime |
| Iteration limit | `max_iterations` | not applicable | our loop |
| Client killed (SIGKILL) | resumed; in-flight call not re-run | re-attached; completed | our recovery code / persistent session |

Both interrupted runs completed with recall 0.9. We did not measure whether the Managed session kept working while the client was down.

## 7. Limitations of this comparison

- One task, dataset and model; 5 runs per side for quality and cost; 1 per fault scenario.
- Anomaly recall comes from rule-based matching against an answer key. We fixed one false negative after seeing results and re-scored every run by the same rule. Most reported numbers (42 of 51 per DIY run, 65 of 72 per Managed run) could not be checked.
- Managed rep 1 has no recall score because its summary failed the schema.
- Run-to-run variance was large: one anomaly was found by both pilot runs but by only 1 of 5 per side in the experiment.
- Observed Managed limitations are listed above. Production concerns (vendor dependence, data residency, scale, pricing changes) were not tested.

## Sources

| Claim | Where |
|---|---|
| LOC, files | `uv run python -m experiments.loc` |
| Test counts | `tests/`, `pytest --collect-only` (153 total) |
| Experiment medians, ranges, completion, reruns | `results/experiment-1/report.json` (local) |
| Tool mix, peak prompt, compaction and error counts | per-run `trace.jsonl`, `session.sqlite`, `run.json` (local) |
| Fault scenarios | `results/failures/failure-report.md` and traces (local) |
| Bugs, corrections, timeline | [engineering-log.md](engineering-log.md) |
