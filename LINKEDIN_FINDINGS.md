# DIY agent harness vs. Anthropic Managed Agents: research notes

**Status: first draft, 2026-10-12.** The 10-run experiment is complete. The reliability scenarios (`results/failures/`) were still running, so anything depending on them is marked **pending**. This is research material, not the post.

## 1. Executive summary

We built the same autonomous sales-data analyst twice on `claude-sonnet-5-5`: once on the Messages API with our own loop, tools, Docker sandbox and crash-recoverable sessions (625 lines of Python), once on Managed Agents (246 lines of client code, no loop, no local sandbox). Each ran the identical task 5 times against a seeded dataset with 14 known anomalies and look-alikes.

Managed Agents eliminated the loop, tool dispatch, sandbox and session storage. It did not eliminate integration work (event streaming, file transfer, cost tracking) or evaluation work, which was the largest code in the repo. Analytical quality was indistinguishable at n=5 (median recall 0.9 on both). In our sample Managed was slower (median 271 s vs 171 s) and costlier (median $0.56 vs $0.34); we did not isolate why.

## 2. Engineering effort

| Measure | DIY | Managed | Source |
|---|---|---|---|
| Lines of code (non-blank, non-comment, non-docstring) | **625** | **246** | `experiments/loc.py` |
| Python files | 5: `agent_loop` 171, `sessions` 144, `tools` 126, `main` 104, `sandbox` 80 | 1: `main` 246 | same |
| Config files | Dockerfile (10 lines) | `agent.md` 17, `environment.yaml` 6 | `wc -l` |
| Test functions written for this side | 47 | 18 | `tests/` |
| Third-party imports | anthropic, jsonschema, dotenv | anthropic, pyyaml, dotenv | AST scan |
| System prerequisites | Docker daemon, image build | API key and beta access | log, Phases 0 and 4 |

LOC is not like for like: DIY includes crash recovery (`sessions.py`) that Managed does not need; Managed includes reconnect, download, trace and redaction code. Fewer lines is not evidence of better design.

**Development time was not tracked.** The brief asked for per-component effort; we have none and cannot reconstruct it. Early "time spent" estimates in the log were written without a timer, contradict commit timestamps, and were formally withdrawn (`docs/engineering-log.md`, "Correction"). What exists is wall-clock time between commits, which includes waiting for approvals and paid runs: brief 23:14, dataset/evaluator 23:36, DIY loop and sandbox 23:52, sessions/recovery 00:09, Managed implementation 00:22, experiment tooling 00:45 (IST).

**Time to first successful full run (approximate, from file and session timestamps):** DIY about 20 minutes after the previous phase commit (including a Docker sandbox pulled forward for safety); Managed about 14. Managed benefited from the dataset, evaluator and lessons from building DIY first, so this is not a fair head-to-head.

**Setup.** DIY needed Python (absent on the Windows host, so `uv` in WSL), a running Docker daemon and an image build. Managed needed an API key and beta access; a free dry run (create agent, environment, file, idle session; delete) validated every API shape before spending anything. The cloud sandbox already has pandas, numpy and matplotlib.

## 3. Infrastructure responsibility matrix

| Capability | Verdict | DIY | Managed | Still ours |
|---|---|---|---|---|
| Agent loop | **Eliminated** | `run_agent`, `agent_loop.py:92` | none; session started via `initial_events` | model, prompt, task |
| Tool orchestration | **Eliminated** (built-ins) | 5 custom tools, schema validation, path confinement, command allowlist (`tools.py`) | `agent_toolset_20260401` in `agent.md`; agents used bash 91, read 21, write 8 | tool and permission policy |
| Sandbox | **Eliminated**, one decision left | `sandbox.py` + Dockerfile: no network, non-root, read-only root, limits, in-container kill timeout, `--init` | `environment.yaml`, 6 lines | networking mode; API default is **unrestricted** |
| Session state | **Eliminated** storage | `sessions.py`: SQLite, `started`/`done` call records, resume | server-side | `watch()` reconnect and de-dup (`managed/main.py:106`) |
| Context management | **Eliminated** | prompt caching enabled by hand; tool output truncated at 10,000 chars; peak prompt 54,818 tokens | built in; 0 compaction events in 5 runs | nothing at this task size |
| Error handling | **Simplified** | 5/20/60 s API backoff, API/sandbox/tool error classes, "outcome unknown" rule (`agent_loop.py:28,116,132`) | 0 `session.error` in 568 events | our own client bugs; validating agent output |
| Observability | **Simplified** | `Tracer`, JSONL, SQLite, `summary.md` | event stream provided | trace writer, redaction, metrics (`managed/main.py:163,209`) |
| Execution limits | **Simplified** | iteration, time and estimated-cost limits in our loop | platform-enforced dollar `budget`; no iteration limit | wall-clock timeout; limit behaviour **pending** |
| Artifact handling | **Left to us, arguably harder** | bind-mounted workspace | Files API upload/list/download (`managed/main.py:179`) | see discovery 3 |

## 4. Experimental results

10 runs (5 per side), same task text and model, alternating order, 00:45-01:25 IST on 2026-10-12. Source: `results/experiment-1/report.json` (not committed; `results/` is gitignored).

| | DIY | Managed |
|---|---|---|
| Completed | 5 of 5 | 5 of 5 |
| Required artifacts and valid `summary.json` | 5 of 5 | 4 of 5 |
| `analysis.py` reruns on the CSV in a clean container | 5 of 5 | 5 of 5 |
| Median injected-anomaly recall (10 entries) | 0.90 (n=5) | 0.90 (n=4 scored) |
| Median unmatched findings | 1 | 0.5 |
| Median active time | 171 s | 271 s |
| Median model / tool calls | 18 / 18 | 22 / 25 |
| Median output tokens | 20,791 | 33,485 |
| Median cost (range) | $0.34 ($0.33-0.45) | $0.56 ($0.42-0.73) |

**Conditions differ:** built-in bash/read/write vs 5 custom tools; sandbox up to 8 GB vs 1 GB/1 CPU; DIY has a 40-iteration limit, Managed none; DIY enabled caching explicitly; task text differs only in file paths. DIY cost is our estimate from token counts; Managed adds $0.005-0.007 runtime, and the platform's reported list cost (median $0.56) agrees. DIY local compute cost was **not measured**. Experiment spend $4.79. Managed rep 1 wrote a `summary.json` with a boolean evidence value the schema rejects, so its recall is unavailable, not zero.

**Reliability scenarios: pending.** So far, syntax error and missing input file were handled by the model on both sides (expected answer written). Tool timeout, tool error, budget and iteration limits, and kill-and-resume are still running.

## 5. Five most interesting discoveries

**1. `docker run` was not yet a sandbox: PID 1 never reaped zombies.** A test forking 2,000 children left our container unusable (`OCI runtime exec failed`): `sleep infinity` never reaps defunct processes, which filled the 256-process limit. Docker's error also came back as ordinary output with exit code 128. Fix: `--init` (`sandbox.py:75`) and classifying it as a sandbox failure. Evidence: log (Phase 3), `tests/test_diy_sandbox.py`. Why it matters: sandboxing is a list of failure modes found one test at a time. We did not run the same fork loop against Managed's sandbox, so we cannot say how it fares.

**2. A wrong price table was caught only because the platform reports its own cost.** Our estimate for the first Managed run was $0.68; the platform said $0.60. The gap was Sonnet 5.5's cache-read price ($0.10 per million tokens, not $0.20), which also overstated every earlier DIY cost. Evidence: log (Phase 4), `tests/test_claims_metrics.py`. Why it matters: output tokens were about 60% of cost on both sides, cache writes about 30%, cache reads 10-13% (arithmetic on mean token counts); cost models are easy to get wrong.

**3. The agent finished; our client crashed afterwards.** The Files API listing includes the uploaded CSV, marked `downloadable: false`, and downloading it returns a 400. A file written to `outputs/charts/x.png` comes back as `x.png`, so `managed/main.py` rebuilds `charts/` by extension. Because the session lives server-side, we re-attached (`--attach`) at no cost. Evidence: log, `tests/test_managed.py`. Why it matters: Managed moves the failure boundary: client crashes stop costing the run, but artifact plumbing becomes your code against someone else's file model.

**4. Evaluation was the biggest code and produced the most bugs.** Evaluator plus runners are 815 lines against 171 for the DIY loop. Found by running real output: ground-truth IDs recorded after a re-sort (wrong rows); a prompt example that leaked an answer; a correct "burst" finding scored as a miss; a numeric-claims check labelling correct claims "incorrect" (redesigned to supported/unsupported/unverifiable). Even now most numbers cannot be checked: on average 42 of 51 evidence values (DIY) and 65 of 72 (Managed) per run are unverifiable. Why it matters: neither platform removes this work, and it is where an agent comparison earns credibility.

**5. Run-to-run variance exceeded the gap between implementations.** The Apparel dip (A09) was found by both pilot runs but by only 1 of 5 runs per side in the experiment; cause unknown. Median recall was identical; Managed cost varied $0.42-0.73. Evidence: `report.md`, log. Why it matters: a single-run demo of either approach would tell a different story depending on the run.

## 6. Managed Agents limitations

**Observed:**
- Output directories are flattened; the input file appears in the output listing as non-downloadable.
- In our sample, 1.6x the active time, 1.7x the median cost, 1.4x the tool calls and 1.6x the output tokens of DIY; cause not isolated (different tools, system prompt, sandbox).
- No iteration limit; budget is dollars only.
- API-created environments default to unrestricted networking (documented; we set `limited` with no hosts).
- 1 of 5 Managed runs wrote a schema-invalid `summary.json` (0 of 5 DIY); weak evidence, and it is the agent's output.
- Tool choice is the platform's: 91 of 120 calls were bash.
- Beta; the recommended `ant` CLI was not installed, so we used the SDK (about 40 lines `ant apply` would replace).

**Pending:** budget-limit and timeout behaviour, re-attach after a killed client.

**Hypothetical, not tested:** vendor dependence, data residency, scale, price changes, sessions long enough to trigger compaction.

## 7. Best LinkedIn angles

1. **"A sandbox is a list of failure modes."** *"I thought `docker run --network none` was a sandbox until a fork loop left our container permanently unusable."* Most people picture flags; this shows the unglamorous part and what a managed runtime might spare you.
2. **"The loop was the small part."** *"Our agent loop was 171 lines; the code to find out whether the agent was right was nearly five times that."* Leaders budget for the agent and forget the eval, and the bugs we hit (a leaked answer, mis-keyed ground truth) would have silently corrupted any conclusion.
3. **"Managed removes infrastructure, not integration."** *"The managed version had no loop and no sandbox, and its client still came to 246 lines."* It is an honest trade: smaller blast radius, survives client crashes, but you still write streaming, files and cost tracking, and our sample showed higher cost and time we could not yet explain.

## 8. Repository information

- https://github.com/ngainedi-dg/claude-agent-diy-vs-managed. **Phases 0-4 are pushed; Phase 5 tooling, the log correction and this file were local-only when written.**
- Inspect: `docs/engineering-log.md`, `diy/agent_loop.py`, `diy/sandbox.py`, `diy/sessions.py`, `managed/main.py`, `managed/agent.md`, `evaluation/claims.py`, `experiments/run_comparison.py`.
- Reproduce (Docker, `uv`, API key with Managed Agents access in `.env.local`; on WSL set `UV_PROJECT_ENVIRONMENT` to a Linux path): `uv sync`; `uv run python task/generate_data.py --seed 42`; `uv run pytest` (152 tests, no API calls); `uv run python -m experiments.run_comparison --repetitions 5 --yes` (about $5); `uv run python -m experiments.failure_scenarios --yes` (about $1.50-2); `--report-only` rebuilds reports free. Managed runs create a real agent, environment and sessions in your account.

## 9. Evidence and confidence

| Claim | Source |
|---|---|
| LOC, file counts | `experiments/loc.py` |
| 47 / 18 test functions; 152 total | `tests/`; `pytest --collect-only` |
| Medians, ranges, completion, rerun counts | `results/experiment-1/report.json` |
| Tool mix (DIY: execute_python 77, write_file 7, run_command 4, list_files 1) | managed `trace.jsonl`, DIY `session.sqlite` |
| Peak prompt 54,818; 0 compactions; 0 errors in 568 events | DIY `run.json`; managed traces |
| $0.60 reported vs $0.68 estimated | log, Phase 4 |
| Cost shares | arithmetic on `report.md` means |
| Timeline | git commit times, session timestamps |

**Missing or weak:** per-component effort, human hours, DIY compute cost, Managed peak context, reliability results (running). **Small samples:** n=5 per side, one task, dataset and model; no significance testing. **Not equivalent:** tools, sandbox size, iteration limits, caching control. **Evaluator limits:** rule-based matching (one false negative fixed after seeing results, all runs re-scored by the same rule); most numeric claims unverifiable. **Process note:** raw results are not committed, so these numbers are reproducible by rerunning, not by reading data.
