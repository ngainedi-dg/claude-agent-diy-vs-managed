# DIY agent harness vs. Anthropic Managed Agents: research notes

**Status: complete draft, 2026-10-12.** The 10-run experiment and all seven reliability scenarios have run. This is research material, not the post.

## 1. Executive summary

We built the same autonomous sales-data analyst twice on `claude-sonnet-5-5`: once on the Messages API with our own loop, tools, Docker sandbox and crash-recoverable sessions (625 lines of Python), once on Managed Agents (246 lines of client code, no loop, no local sandbox). Each ran the identical task 5 times against a seeded dataset with 14 known anomalies and look-alikes.

Managed Agents eliminated the loop, tool dispatch, sandbox and session storage. It did not eliminate integration work (event streaming, file transfer, cost tracking) or evaluation work, the largest code in the repo. Quality was indistinguishable at n=5 (median recall 0.9 both). Managed was slower (271 s vs 171 s median) and costlier ($0.56 vs $0.34) in our sample, for reasons we did not isolate. Every reliability scenario that applied ended as intended on both sides; what differed was who enforced the limit.

## 2. Engineering effort

| Measure | DIY | Managed | Source |
|---|---|---|---|
| Lines of code (non-blank, non-comment, non-docstring) | **625** | **246** | `experiments/loc.py` |
| Python files | 5: `agent_loop` 171, `sessions` 144, `tools` 126, `main` 104, `sandbox` 80 | 1: `main` 246 | same |
| Config files | Dockerfile (10 lines) | `agent.md` 17, `environment.yaml` 6 | `wc -l` |
| Test functions written for this side | 47 | 19 | `tests/` |
| Third-party imports | anthropic, jsonschema, dotenv | anthropic, pyyaml, dotenv | AST scan |
| System prerequisites | Docker daemon, image build | API key and beta access | log, Phases 0 and 4 |

LOC is not like for like: DIY includes crash recovery (`sessions.py`) Managed does not need; Managed includes reconnect, download, trace and redaction code. Fewer lines is not evidence of better design.

**Development time was not tracked.** The brief asked for per-component effort; we have none and cannot reconstruct it. Early "time spent" estimates in the log were written without a timer, contradict commit timestamps, and were formally withdrawn (`docs/engineering-log.md`, "Correction"). What exists is wall-clock time between commits, which includes waiting for approvals and paid runs: brief 23:14, dataset/evaluator 23:36, DIY loop and sandbox 23:52, sessions/recovery 00:09, Managed implementation 00:22, experiment tooling 00:45 (IST).

**Time to first successful full run (approximate, file and session timestamps):** DIY about 20 minutes after the previous phase commit (including a Docker sandbox pulled forward for safety); Managed about 14. Managed benefited from the dataset, evaluator and lessons from building DIY first, so this is not a fair head-to-head.

**Setup.** DIY needed Python (absent on the Windows host, so `uv` in WSL), a running Docker daemon and an image build. Managed needed an API key and beta access; a free dry run (create agent, environment, file, idle session; delete) validated every API shape before spending anything. The cloud sandbox already has pandas, numpy and matplotlib.

## 3. Infrastructure responsibility matrix

| Capability | Verdict | DIY | Managed | Still ours |
|---|---|---|---|---|
| Agent loop | **Eliminated** | `run_agent`, `agent_loop.py:92` | none; session started via `initial_events` | model, prompt, task |
| Tool orchestration | **Eliminated** (built-ins) | 5 custom tools, schema validation, path confinement, command allowlist (`tools.py`) | `agent_toolset_20260401` in `agent.md`; agents used bash 91, read 21, write 8 | tool and permission policy |
| Sandbox | **Eliminated**, one decision left | `sandbox.py` + Dockerfile: no network, non-root, read-only root, limits, in-container kill timeout, `--init` | `environment.yaml`, 6 lines | networking mode; API default is **unrestricted** |
| Session state | **Eliminated** storage | `sessions.py`: SQLite, `started`/`done` call records, resume | server-side; `--attach` re-joined a killed client's session | `watch()` reconnect and de-dup (`managed/main.py:106`) |
| Context management | **Eliminated** | prompt caching enabled by hand; tool output truncated at 10,000 chars; peak prompt 54,818 tokens | built in; 0 compaction events in 5 runs | nothing at this task size |
| Error handling | **Simplified** | 5/20/60 s API backoff, API/sandbox/tool error classes, "outcome unknown" rule (`agent_loop.py:28,116,132`) | 0 `session.error` in 568 events; tool failures arrive flagged `is_error` | our own client bugs; validating agent output |
| Observability | **Simplified** | `Tracer`, JSONL, SQLite, `summary.md` | event stream provided | trace writer, redaction, metrics (`managed/main.py:163,209`) |
| Execution limits | **Simplified** | iteration, time, estimated-cost limits in our loop | platform tool timeout (295 s) and dollar `budget`; no iteration limit | wall-clock timeout we send as `user.interrupt` |
| Artifact handling | **Left to us, arguably harder** | bind-mounted workspace | Files API upload/list/download (`managed/main.py:179`) | see discovery 3 |

## 4. Experimental results

**Main experiment:** 10 runs (5 per side), same task text and model, alternating order, 00:45-01:25 IST. Source: `results/experiment-1/report.json` (not committed; `results/` is gitignored).

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

**Conditions differ:** built-in bash/read/write vs 5 custom tools; sandbox up to 8 GB vs 1 GB/1 CPU; DIY has a 40-iteration limit, Managed none; DIY enabled caching explicitly; task text differs only in file paths. DIY cost is our estimate from token counts; Managed adds $0.005-0.007 runtime, and the platform's reported list cost (median $0.56) agrees. DIY local compute cost was **not measured**. Spend $4.79. Managed rep 1 wrote a `summary.json` with a boolean evidence value the schema rejects, so its recall is unavailable, not zero.

**Reliability scenarios:** one run per side each, micro-tasks with a known answer (`results/failures/failure-report.md`). Faults go in through the task because a hosted runtime cannot be faulted from outside, so the two sides are not identical tests.

| Scenario | DIY | Managed | Who handled it |
|---|---|---|---|
| Syntax error | `OK`, 6.6 s | `OK`, 6.7 s | model read the traceback; both returned it as an error result |
| Missing input file | `MISSING`, 7.7 s | `MISSING`, 62.8 s | model (Managed's agent ran `find /`, about 59 s on its large filesystem) |
| Tool error | wrote `2` | wrote `2` | model |
| Tool timeout (600 s sleep) | `TIMEOUT`, 305 s; see discovery 2 | `TIMEOUT`, 302 s; platform killed it at 295 s | DIY: model; Managed: runtime plus model |
| Budget cap $0.05 | stopped at $0.0503 | paused `budget_reached`, list cost $0.06 | DIY: our loop; Managed: runtime |
| Iteration limit (3) | `max_iterations` | not applicable | our loop |
| Client killed (SIGKILL after 6 / 7 tool calls) | resumed; 1 in-flight call not re-run; completed, $0.35 | re-attached; completed, $0.49 | DIY: our recovery code; Managed: session persisted |

Both interrupted runs scored recall 0.9 and valid artifacts. We did not measure whether the Managed session kept working while the client was dead.

## 5. Five most interesting discoveries

**1. `docker run` was not yet a sandbox: PID 1 never reaped zombies.** A test forking 2,000 children left our container unusable (`OCI runtime exec failed`): `sleep infinity` never reaps defunct processes, which filled the 256-process limit. Docker's error also came back as ordinary output with exit code 128. Fix: `--init` (`sandbox.py:75`) and classifying it as a sandbox failure. Evidence: log (Phase 3), `tests/test_diy_sandbox.py`. Why it matters: sandboxing is a list of failure modes found one test at a time. We did not run the same fork loop against Managed's sandbox, so we cannot say how it fares.

**2. Same fault, different test: the DIY agent routed around our guardrail.** Asked to run `python3 -c "import time; time.sleep(600)"`, the DIY agent was rejected by our command allowlist (it allows `python`, not `python3`), then wrote its own subprocess timeout inside `execute_python`, and its final message admitted the command "wasn't run exactly as given". So our sandbox kill path was never exercised. Managed's bash tool simply returned `command timed out after 295s` flagged `is_error`, and the model reported it accurately. Evidence: `results/failures/tool_timeout/*`. Why it matters: reliability tests through a prompt measure agent plus harness together, and a harness's own restrictions become failure injection.

**3. The agent finished; our client crashed afterwards.** The Files API listing includes the uploaded CSV, marked `downloadable: false`, and downloading it returns a 400. A file written to `outputs/charts/x.png` comes back as `x.png`, so `managed/main.py` rebuilds `charts/` by extension. Because the session lives server-side, we re-attached (`--attach`) at no cost. Evidence: log, `tests/test_managed.py`. Why it matters: Managed moves the failure boundary: client crashes stop costing the run, but artifact plumbing becomes your code against someone else's file model. The kill-and-resume scenario also exposed a bug in that code (a re-attach duplicated 37 trace events), now fixed.

**4. Evaluation was the biggest code and produced the most bugs.** Evaluator plus runners are 815 lines against 171 for the DIY loop. Found by running real output: ground-truth IDs recorded after a re-sort (wrong rows); a prompt example that leaked an answer; a correct "burst" finding scored as a miss; a numeric-claims check labelling correct claims "incorrect" (redesigned to supported/unsupported/unverifiable); our price table had Sonnet 5.5 cache reads at $0.20 instead of $0.10, noticed only because the platform's reported cost ($0.60) disagreed with ours ($0.68). Even now most numbers cannot be checked: on average 42 of 51 evidence values (DIY) and 65 of 72 (Managed) per run are unverifiable. Why it matters: neither platform removes this work.

**5. Run-to-run variance exceeded the gap between implementations.** The Apparel dip (A09) was found by both pilot runs but by only 1 of 5 runs per side in the experiment; cause unknown. Median recall was identical; Managed cost varied $0.42-0.73. Evidence: `report.md`, log. Why it matters: a single-run demo of either approach would tell a different story depending on the run.

## 6. Managed Agents limitations

**Observed:**
- Output directories are flattened; the input file appears in the output listing as non-downloadable.
- In our sample, 1.6x the active time, 1.7x the median cost, 1.4x the tool calls and 1.6x the output tokens of DIY; cause not isolated (different tools, system prompt, sandbox).
- No iteration limit; budget is dollars only. On a $0.05 cap the session paused at $0.06 list cost; the docs say the cap can be exceeded by at most one model request, and we did not check per-request cost.
- A platform tool timeout of about 295 s, which we saw enforced. We did not test whether it can be raised.
- API-created environments default to unrestricted networking (documented; we set `limited` with no hosts).
- 1 of 5 Managed runs wrote a schema-invalid `summary.json` (0 of 5 DIY); weak evidence, and it is the agent's output.
- Tool choice is the platform's: 91 of 120 calls were bash.
- Beta; the recommended `ant` CLI was not installed, so we used the SDK (about 40 lines `ant apply` would replace).

**Hypothetical, not tested:** vendor dependence, data residency, scale, price changes, sessions long enough to trigger compaction.

## 7. Best LinkedIn angles

1. **"A sandbox is a list of failure modes."** *"I thought `docker run --network none` was a sandbox until a fork loop left our container permanently unusable."* Most people picture flags; this shows the unglamorous part and what a managed runtime might spare you.
2. **"The loop was the small part."** *"Our agent loop was 171 lines; the code to find out whether the agent was right was nearly five times that."* Leaders budget for the agent and forget the eval, and the bugs we hit (a leaked answer, mis-keyed ground truth) would have silently corrupted any conclusion.
3. **"Managed removes infrastructure, not integration."** *"The managed version had no loop and no sandbox, and its client still came to 246 lines."* It is an honest trade: smaller blast radius, survives client crashes, but you still write streaming, files and cost tracking, and our sample showed higher cost and time we could not explain.

## 8. Repository information

- https://github.com/ngainedi-dg/claude-agent-diy-vs-managed. **Phases 0-4 are pushed; Phase 5 tooling, the experiment runners, the log correction and this file were local-only when written.**
- Inspect: `docs/engineering-log.md`, `diy/agent_loop.py`, `diy/sandbox.py`, `diy/sessions.py`, `managed/main.py`, `managed/agent.md`, `evaluation/claims.py`, `experiments/run_comparison.py`.
- Reproduce (Docker, `uv`, API key with Managed Agents access in `.env.local`; on WSL set `UV_PROJECT_ENVIRONMENT` to a Linux path): `uv sync`; `uv run python task/generate_data.py --seed 42`; `uv run pytest` (153 tests, no API calls); `uv run python -m experiments.run_comparison --repetitions 5 --yes` (about $5); `uv run python -m experiments.failure_scenarios --yes` (about $1.50); `--report-only` rebuilds reports free. Managed runs create a real agent, environment and sessions in your account.

## 9. Evidence and confidence

| Claim | Source |
|---|---|
| LOC, file counts | `experiments/loc.py` |
| 47 / 19 test functions; 153 total | `tests/`; `pytest --collect-only` |
| Medians, ranges, completion, rerun counts | `results/experiment-1/report.json` |
| Tool mix (DIY: execute_python 77, write_file 7, run_command 4, list_files 1) | managed `trace.jsonl`, DIY `session.sqlite` |
| Peak prompt 54,818; 0 compactions; 0 errors in 568 events | DIY `run.json`; managed traces |
| Scenario outcomes, timings, costs | `results/failures/failure-report.md` and traces |
| $0.60 reported vs $0.68 estimated | log, Phase 4 |
| Timeline | git commit times, session timestamps |

**Missing or weak:** per-component effort, human hours, DIY compute cost, Managed peak context, whether Managed kept working while disconnected. **Small samples:** n=5 per side for the experiment, n=1 per scenario; one task, dataset and model; no significance testing. **Not equivalent:** tools, sandbox size, iteration limits, caching control, and the fault-injection method. **Evaluator limits:** rule-based matching (one false negative fixed after seeing results, all runs re-scored by the same rule); most numeric claims unverifiable. **Process note:** raw results are not committed, so these numbers are reproducible by rerunning, not by reading data.
