# Engineering Log

Entries are written as work happens, not reconstructed later. Times are rough
unless marked measured.

## 2026-10-11 — Phase 0: Pre-implementation review

**Work attempted**
- Read the project brief and the Managed Agents quickstart, environments, files and pricing docs.
- Checked the local toolchain and credentials.

**Findings: Managed Agents (beta)**
- Beta header is `managed-agents-2026-04-01`. The Python SDK sets it automatically on `client.beta.{agents,environments,sessions}.*`.
- The object flow is Agent (create once, versioned) -> Environment -> Session -> event stream. `model`, `system` and `tools` live on the agent, never the session.
- The built-in toolset is `agent_toolset_20260401` (bash, file operations, web). It can run under a permission policy such as `auto`.
- Input files go through `client.beta.files.upload` and are attached as a session `resources` entry. Mounted files are read-only and land under `/mnt/session/uploads/`.
- Output files written to `/mnt/session/outputs/` are retrieved with `client.beta.files.list(scope_id=session.id, betas=["managed-agents-2026-04-01"])` and `files.download`. There is a ~1-3s indexing lag after idle. Needs `anthropic` >= 0.92.0 for `scope_id`.
- Cloud environments use `networking: {type: "limited"}` (deny by default) or `unrestricted`. Installing packages under `limited` requires `allow_package_managers: true`.
- Pricing: tokens at the normal model rates, plus session runtime at $0.08 per session-hour, metered only while status is `running`. This replaces container-hour billing, so there is no separate charge for containers.
- Not yet verified, because it needs an API key: account eligibility, whether pandas, numpy and matplotlib are preinstalled in the cloud sandbox, and what `session.usage` returns.

**Findings: local environment**
- Python is not installed (only the Windows Store stub). `uv` 0.10.7 is present and can provision Python.
- Docker is not installed, and the DIY sandbox requires it.
- The `ant` CLI is not installed. It is optional, since the SDK can create agents and environments.
- `ANTHROPIC_API_KEY` is not set.
- git is installed. The repo is initialised and pushed with the brief only.

**Design decisions so far**
- The DIY loop is hand-written against `client.messages` with `stop_reason == "tool_use"` handling. The SDK's `tool_runner` helper is not used, because it implements the loop and the brief forbids that.
- The Managed implementation uses only the documented session lifecycle and no local loop or sandbox.

**Infrastructure avoided through Managed Agents:** none built yet.
**Time spent:** about 0.5h on reading and checks (estimate).

### Update: WSL environment and decisions
- The user chose to run the project inside WSL. Ubuntu (WSL2) has Python 3.12.3, git, pip, Docker CLI 29.3.0 and Podman 4.9.3. `uv` is missing.
- The Docker daemon is not running (`/var/run/docker.sock` is absent). Needs starting before the DIY sandbox can be tested.
- No Anthropic credentials in WSL either.
- Model decision: `claude-sonnet-5-5` ($2 / $10 per MTok) for both agents.

## 2026-10-11 — Phase 1: Repository and dataset

**Work done**
- Scaffolding: `pyproject.toml` (uv, `package = false`), `.env.example`, `.gitignore`, `tests/`.
- `task/generate_data.py`: seeded generator producing 9,980 rows plus a separate ground-truth file. Output is byte-identical across runs for a given seed (tested).
- 14 ground-truth entries: 7 injected anomalies, 4 injected data defects, 1 ambiguous (negative quantities that could be returns), 3 legitimate patterns that should not be flagged as problems.
- `task/instructions.md` (the prompt both agents will share), `task/schema.md` and `task/summary.schema.json`.
- `evaluation/ground_truth.py` (deterministic matching) and `evaluation/evaluate.py` (artifact checks, schema validation, numeric-claim checks, anomaly recall). No LLM judge.
- 30 tests, all passing: `uv run pytest`.

**Complications and bugs**
- **Bug caught by the tests:** the first version recorded ground-truth transaction IDs *after* inserting the duplicate rows and re-sorting, so the IDs pointed at the wrong rows. The row-level data was correct but the answer key was wrong. This would have silently corrupted every recall score that relied on ID overlap. Fixed by capturing IDs before the re-sort.
- **Leak caught by a test:** the example in the task prompt used `"region": "West"` and the exact A01 date range, which is the answer to A01. Replaced it with neutral values and added a test that scans the prompt for ground-truth terms.
- The `ant` and `uv` tools were missing in WSL; `uv` is now installed there with `pip install --user`. Running on the Windows filesystem through `/mnt/c` is slow (about 40s for the test suite, mostly pandas imports).
- Multi-line shell here-docs with mixed quoting failed in the Bash tool, so files were written with the editor tool instead.

**Design decisions**
- Ground truth lives in `evaluation/data/`, separate from `task/data/`. Only the CSV is ever given to an agent.
- Matching is conservative keyword, segment and ID-overlap rules. Recall is therefore a lower bound; unmatched findings are listed for manual review instead of being counted as false positives automatically.
- A legitimate pattern counts as flagged only if the agent also set `further_investigation_recommended`. Mentioning a trend is not penalised.
- The evaluator never executes agent-written code. Checking that `analysis.py` runs is deferred to Phase 5 and will run inside the sandbox.
- `evaluation/metrics.py` is deferred until there is runtime data to record (Phase 2/3).

**Infrastructure avoided through Managed Agents:** not applicable yet.
**Time spent:** about 2.5h (estimate, including debugging the ID bug).

## 2026-10-11 — Phase 2: Minimal DIY agent (built, not yet run against the API)

**Work done**
- `diy/agent_loop.py`: the loop, written by hand against `client.messages.create`. It checks `stop_reason`, runs every `tool_use` block, returns all results in one user message with matching IDs, and echoes `response.content` back unchanged (so thinking blocks stay valid). Handles `end_turn`, `tool_use`, `max_tokens` (a truncated tool call is not executed), `refusal` and `pause_turn`.
- Limits: max iterations, wall-clock and an estimated-cost ceiling. A tool_use ID is never executed twice.
- `diy/tools.py`: `list_files`, `read_file`, `write_file`, `execute_python`, `run_command`. Arguments are validated against JSON Schema before anything runs. Paths are confined to `/workspace` (and `/data` read-only). `run_command` is an allowlist with no shell.
- `diy/sandbox.py` and `diy/Dockerfile`: one container per run, driven by `docker exec`.
- `diy/main.py` with `--dry-run` (sandbox and tools only, no model calls) and `experiments/estimate_cost.py` (uses the free `count_tokens` endpoint).
- 64 tests pass, including real-Docker tests for isolation (no network, non-root uid 1000, read-only root and data mount, no API key in the environment, timeout kill).

**Decision: Docker pulled forward from Phase 3**
The brief schedules the sandbox in Phase 3 and allows a simpler executor in Phase 2. I did not do that. The API key sits in `.env.local` on a filesystem the host process can read, and the first paid run would execute model-written code. A subprocess executor would have been quicker but unsafe, so the basic container came first. This inflates Phase 2 effort relative to the brief's plan; it belongs to the DIY column of the comparison either way. Phase 3 still owns sessions, persistence, recovery and the extra error handling.

**Complications**
- `uv run pytest` with the virtualenv on `/mnt/c` took about 96s per run. Putting the venv on the Linux filesystem (`UV_PROJECT_ENVIRONMENT=$HOME/.venvs/diy-vs-managed`) brought a run down to about 4s. Worth documenting for WSL users.
- A nested here-doc inside a script piped to `bash -s` hung `uv run`. Avoid nested here-docs in piped scripts.
- `anthropic` 1.13.0 installed (the pin `>=0.92.0` is satisfied) and it no longer depends on `httpx`, so a test that imported `httpx` to build an API error failed. The test now builds `anthropic.APIError` directly.
- A test expecting exactly `$2.00` was wrong by `$0.0001` (the fake usage had 10 output tokens). Test bug, not a code bug.
- Whether the Messages API accepts the top-level `cache_control` I enabled for prompt caching is not confirmed by the SDK signature alone; the first real call will show it.

**Design decisions**
- Prompt caching is on for the DIY loop (top-level `cache_control`). A competent developer would enable it; without it the same run costs roughly 3x more. Managed Agents presumably handles this itself, so the comparison must report cache usage separately.
- Tool output is truncated to 10,000 characters per stream (head and tail kept). This is the only context management so far.
- No completion check beyond `end_turn`. If the agent stops without writing a deliverable, the evaluator reports it; the harness does not nudge it.
- The "Environment" paragraph appended to the task differs per implementation (paths only). Everything else in the prompt is shared.

**Cost estimate for the first paid run** (measured fixed prompt: 2,342 tokens; projections below are a model, not a measurement)

| Scenario | With caching | Without caching |
|---|---|---|
| 12 turns | $0.26 | $0.53 |
| 25 turns | $0.89 | $2.67 |
| 40 turns (the limit) | $2.29 | $8.99 |

**Infrastructure avoided through Managed Agents:** not applicable yet.
**Time spent:** about 3h (estimate), including the Docker work pulled forward.

### Phase 2 result: first paid DIY run (`results/diy/run-001`, not committed; `results/` is ignored)

Approved by the user before running: one run on `claude-sonnet-5-5`, $3.00 cap.

| Measure | Value |
|---|---|
| Status | completed (`end_turn`) |
| Model calls / tool calls | 17 / 17 (one error, recovered on the next turn) |
| Duration | 174.5 s |
| Tokens | 36 uncached input, 22,216 output, 41,344 cache write, 303,444 cache read |
| Estimated cost | $0.39 (projection for a 12-turn run was $0.26, for 25 turns $0.89) |
| Artifacts | `report.md`, `analysis.py`, 6 charts, valid `summary.json` with 15 findings |

**Acceptance:** met. Claude wrote Python, ran it, read the output, hit a Python error on turn 2, fixed it, and decided for itself when to stop. Prompt caching worked first time (`cache_control` accepted). `analysis.py` re-ran offline in a fresh container against the original CSV (exit 0, regenerated all charts and `summary.json`).

**Evaluator result (single run, so anecdote, not a finding):** 9 of 10 injected anomalies/defects matched (A02-A10), A11 (ambiguous) also matched. **A01 (West decline) was missed**: the report says regional shares "are stable month to month", so this is a real miss, not a matcher gap. All three legitimate patterns were mentioned. One legitimate pattern (Home & Garden, F10) counted as "flagged as a problem" because the agent set `further_investigation_recommended` while calling it consistent with seasonality; that is a judgement call and the heuristic is crude. Three findings matched nothing and are real extra trends (a large customer, category mix, weekday pattern), not false positives.

**Evaluator problems found by running it on real output**
- The numeric-claim check marked `row_count: 6` as incorrect inside a finding about six transactions. The metric name was about the segment, not the file. Fixed: only dataset-wide findings (empty segment) are checked.
- After the fix, 0 of 67 evidence values were verifiable, because the agent used its own metric names (`revenue_Electronics`, `orders_monday`). The numeric-claims check is nearly empty for free-form evidence. Phase 5 needs either a way to recompute segment-scoped numbers or a different approach; I have not decided which.

**Not changed on purpose:** I did not tune the dataset to make A01 easier after seeing this result. Whether a 45% drop in West transactions over 2.5 months is findable is a property of the benchmark and should be judged on all runs, not one.

**Time spent on Phase 2 including the run and its analysis:** about 4h (estimate).

## 2026-10-12 — Phase 3: DIY infrastructure

**Work done**
- `diy/sessions.py`: SQLite session store (conversation, tool calls, usage, errors, status, elapsed time). One database per run. All text is redacted on the way in.
- `diy/agent_loop.py` rewritten around the session. Messages are plain dicts in the API's shape; response blocks are stored with `model_dump(exclude_unset=True)`. A resumed run rebuilds the history from the database.
- Recovery rule: a tool call is written as `started` before it runs and `done` after. On resume, `done` calls are reused, calls that never began are run, and calls left `started` are **not** run again; the model gets an explicit "outcome unknown" result instead.
- API failures: transient errors (connection, 408/409/429, 5xx) are retried with 5/20/60s backoff on top of the SDK's own two retries; other errors (400, 401, ...) fail at once. Both are recorded in an errors table, separately from tool failures and sandbox failures.
- Ctrl-C ends the run as `interrupted`; any status except `completed` and `refusal` can be resumed (`--resume RUN_DIR`, with optional new limits).
- Output per run: `session.sqlite`, `trace.jsonl`, `run.json` and a human-readable `summary.md`.
- `experiments/loc.py` fixes the LOC methodology (non-blank, non-comment, non-docstring Python lines; docs, data, lockfiles, config excluded and listed separately).
- Tests: 87 pass (loop, retries, sessions, recovery, redaction, tools, CLI, and real-container tests).

**Complications and bugs found by tests**
- **Sandbox wedged by a fork loop.** A test that forked 2000 children left the container unusable ("OCI runtime exec failed: procReady not received"). Cause: PID 1 was `sleep infinity`, which never reaps zombies, so they filled the 256-process limit for good. Fix: `docker run --init`. Any model-written code that spawns subprocesses could have hit this. It is the sort of thing a managed sandbox presumably handles, and we found it only because we wrote a test for it.
- **Misclassified failure.** The same failure came back as a normal result with exit code 128 and the error text in stdout, so the loop would have shown Docker's message to the model as if it were the program's output. `exec` now treats "OCI runtime exec failed" as a sandbox failure.
- A test that parsed `/proc/mounts` broke on a trailing blank line (test bug).
- Scratch-script numbering after resume would have overwritten `step_001.py`; `Dispatcher` now takes the starting count from the session.
- A run that crashes after saving an assistant message that was cut off by `max_tokens`, but before saving the "not run" results, would run that truncated tool call on resume. This is a narrow gap and I have not closed it. (Fix would be to store `stop_reason` with the message.)

**What is not verified yet**
- The refactored loop has not run against the real API, and neither has resume. The block serialisation is checked against the SDK's own response types offline, but only the API can confirm it accepts the dicts back. A real run plus a real interruption is needed (it costs money, so it needs approval).

**DIY size at this point (`uv run python -m experiments.loc`):** `diy/` 619 lines of code in 6 Python files, plus one Dockerfile. `agent_loop.py` 171, `sessions.py` 144, `tools.py` 126, `main.py` 98, `sandbox.py` 80. Evaluator 216, dataset generator 300, tests 645.

**Infrastructure implemented for the DIY agent that Managed Agents is expected to provide (to be confirmed in Phase 4):** the agent loop, tool dispatch and argument validation, the sandbox (isolation flags, limits, timeouts, zombie reaping), session persistence and crash recovery, API retry policy, token and cost accounting, execution tracing.

**Time spent on Phase 3:** about 5h (estimate), of which roughly 1h was the two sandbox bugs and one serialisation round-trip check.

### Phase 3 validation: real run, SIGKILL, resume (`results/diy/run-002`)

Approved by the user before running: one run plus one resume, $1.50 combined cap.

- The client was killed with `kill -9` after 6 tool calls. The session was left as `running` with 5 calls `done` and 1 `started` (the kill landed while a call was in flight).
- `--resume` started a new container on the same workspace. The `started` call was **not** re-run; the model received the "outcome unknown" result and carried on. The run then completed at iteration 20.
- The API accepted the history rebuilt from SQLite (including any thinking blocks), so the `exclude_unset` serialisation works against the real API.
- Whole interrupted-and-resumed run: 20 model calls, 190 s of active time, $0.41, peak prompt 41,949 tokens (nowhere near the 1M window, so no context compaction was needed). The cost matches the uninterrupted run-001 ($0.39) closely; resume did not add a visible penalty.
- Evaluator: valid schema, all artifacts, 15 findings, **10 of 10** injected entries matched including A01, which run-001 missed. Two runs is not a result, but it does show the run-to-run variation the 5-run stage is meant to measure.

**Found:** SIGKILL leaves the container running. The loop's cleanup never ran, so one `diy-agent-*` container was orphaned and I removed it by hand. A managed runtime owns its own container lifecycle; for the DIY agent, a reaper (label containers, remove stale ones at startup) is another piece we would have to write. Not built, because the experiment doesn't need it, but it belongs in the comparison.

**Phase 3 acceptance:** met. The DIY agent completes the task with generated code running only in Docker, with sessions, structured traces, error handling and tested recovery.

## 2026-10-12 — Phase 4: Managed Agents implementation (built; first paid run pending approval)

**Reading and verification (all free)**
- Account has Managed Agents access (empty `GET /v1/agents` earlier). Beta header `managed-agents-2026-04-01` is set by the SDK.
- The cloud sandbox ships Python 3.10-3.13 with NumPy, pandas and Matplotlib installed, so the environment needs **no `packages`**. Setup effort for the analysis libraries: zero. (The DIY side needed a Dockerfile and a pip install.)
- Sandbox is much larger than ours: up to 8 GB RAM and 10 GB disk, Ubuntu 24.04, many tools (git, ffmpeg, LibreOffice, Playwright...). Our Docker sandbox has 1 GB, 1 CPU and a handful of allowed commands. This is a fairness caveat for runtime comparisons, not something I can equalise.
- **Gotcha:** an environment created through the API without a `networking` block defaults to `unrestricted`. I set `networking: {type: limited}` with no hosts and no package managers, which is the closest match to the DIY `--network none`.
- Default effort on the agent echoes as `high` (the DIY loop gets the same default for Sonnet 5.5), so the two are comparable on that axis.
- The built-in toolset (`agent_toolset_20260401`: bash, read, write, edit, glob, grep, web tools) is broader than our five tools. I disabled web search and fetch and set the permission policy to `always_allow`, because nobody is present to approve calls and the sandbox has no network or secrets.
- Platform features we had to build for DIY and did not here: the loop, tool dispatch, sandbox, session persistence, prompt caching, context compaction (built in), and a hard spend ceiling (`budget`, enforced by the platform before each model call; ours is an estimate checked by our own loop).
- Pricing: tokens at the model's rates plus $0.08 per session-hour while `running` (about $0.004 for a 3-minute run).

**Work done**
- `managed/agent.md` and `managed/environment.yaml`: the agent and environment as files (agent.md is in the `ant apply` format), plus `managed/main.py` (229 lines of code): create-or-reuse agent and environment (ids kept in a git-ignored state file), upload the CSV, create a budgeted session with the task as `initial_events`, stream events with reconnect and de-duplication, interrupt on timeout, download `/mnt/session/outputs`, record usage, write `run.json`, `summary.md` and a redacted `trace.jsonl`, `--attach` to re-join a session after the client dies.
- A free dry run created the agent (v1), the environment, uploaded the CSV, created a session with no work and deleted it. This validated every API shape without spending anything.
- 21 offline tests: reuse of resources, agent versioning on edit, stream reconnect, event interpretation, timeouts, redaction, output download safety, cost arithmetic, and checks that `managed/main.py` imports no subprocess, docker, `diy` or agent framework.

**Complications**
- The `ant` CLI is not installed, so I used the SDK to create resources. About 40 lines of `managed/main.py` (state file, hashing, create/update) would disappear with `ant apply`; the quickstart recommends that route. I kept the SDK so the repo needs nothing but `uv sync`. Count that when comparing.
- A test that drained a generator forever caught a real design point: after a dropped stream we must keep reconnecting until the caller stops, because stopping is the caller's decision. The reconnect loop does that, and the stored-event listing makes re-attaching to an already-finished session work.
- Patching a source file through a shell here-doc broke two string literals (escaped newlines). Edit the file directly instead.

**Size so far (`uv run python -m experiments.loc`):** managed 229 lines of code (1 Python file) plus 2 configuration files. DIY: 619 lines of code in 6 files plus a Dockerfile. These numbers are not a verdict. `managed/main.py` carries concerns DIY also has (redaction, trace, output handling), and neither side's number says anything about how much *thinking* each took.

**Time spent so far on Phase 4:** about 2h (estimate).

### Phase 4 result: first paid Managed run (`results/managed/run-001`, not committed)

Approved by the user before running: one run on `claude-sonnet-5-5`, session budget $3.00.

| Measure | Value |
|---|---|
| Outcome | `end_turn`, status completed, no session errors |
| Model calls / tool calls | 24 / 27 (bash 22, read 5) |
| Active session time | 294 s (DIY runs: 175-190 s) |
| Tokens | 50 uncached input, 34,022 output, 67,792 cache write, 807,914 cache read |
| Platform-reported list cost | **$0.60** (my token estimate $0.591 + runtime $0.0065 = $0.597) |
| Artifacts | `report.md`, `analysis.py`, 5 charts, valid `summary.json` with 14 findings |
| Evaluator | 10 of 10 injected entries, 0 unmatched findings, 0 legitimate patterns flagged as problems |

**Acceptance:** met. The Managed agent completed the task with no local agent loop and no local sandbox.

**Problems found, in the order they appeared**
1. **My post-processing crashed after the work was done.** `files.list(scope_id=session)` also returns the CSV we uploaded, marked `downloadable: false`, and downloading it returns a 400. The session was fine and the money was spent; only my client code failed. I fixed the client, then re-attached to the finished session with `--attach` (free) to collect the outputs. This is an argument for the design: the work survives a client bug.
2. **Directories are flattened in the Files API.** The agent wrote `/mnt/session/outputs/charts/01_x.png`; the listing returns `01_x.png`. The output contract requires `charts/`, so `managed/main.py` now puts image files back under `charts/` by extension. If an agent wrote two files with the same name in different directories I do not know what would happen. This is extra application code the DIY side does not need (it owns the filesystem).
3. **My Sonnet 5.5 price table was wrong.** I had cache reads at $0.20 per million; the pricing page says $0.10 (0.05x input; the $0.20 figure applies to Opus 5.5). I found it because my estimate ($0.68) disagreed with the platform's reported list cost ($0.60). With $0.10 the two agree to within a cent. **Consequence:** every DIY cost I logged earlier is overstated: run-001 was $0.356, not $0.386, and run-002 was $0.368, not $0.411 (arithmetic from the recorded token counts). The code and tests are corrected. The pre-run projections in the Phase 2 table were also a little high.
4. **The session `usage` object splits cache writes** into `cache_creation.ephemeral_5m_input_tokens` and `..._1h_...`, not the single field the Messages API uses. My first metrics code returned `None` for cost until I read the real response. 1-hour writes are priced at 2x input.
5. **A matcher false negative.** The Managed agent correctly reported the 14-16 June burst, but the A03 rule required words like "spike" or "volume" and the agent wrote "burst", so it was scored as a miss. I changed A03 so a finding scoped to a window of 10 days or fewer overlapping those dates matches regardless of wording. I made this change after seeing a result, which is the risk I flagged before; the reason it is defensible is that the finding was objectively correct and I re-scored all three runs by the same rule (the DIY scores did not change). Added a regression test, including a wide-window case that must not match.

**Three runs so far, same evaluator: n=3, an anecdote and not a comparison.** DIY run-001 90% (missed West), DIY run-002 100%, Managed run-001 100%.

**Observations for the comparison (not conclusions)**
- Managed used more tool calls (27 vs 15-19) and more output tokens (34k vs 22k) for roughly the same findings. That could be a larger default toolset (bash/read vs our execute_python), different default behaviour, or chance; five runs each should say.
- Managed's `analysis.py` hardcodes `/mnt/session/uploads/...` and DIY's `/data/...`. A reproducibility check in Phase 5 has to mount the CSV at whichever path the script uses.
- No iteration limit exists on the Managed side (only the dollar budget and our timeout); the brief's "max iterations where applicable" is not applicable.

**Leftovers on the Anthropic account** (nothing archived without asking, since archive is permanent): 1 agent (`diy-vs-managed-sales-analyst`, v1), 1 environment (same name), 1 session with its output files. The dry-run session and both uploaded input files were deleted.

**Time spent on Phase 4:** about 4h (estimate), of which about 1h was the post-run fixes and the price correction.

## 2026-10-12 — Phase 5: Comparison experiments (tooling built; paid runs awaiting approval)

Pushed Phases 0-4 to `origin/main` at the user's request before starting (secret scan of tracked files first: only the fake keys used in tests; no `.env`, dataset, ground truth, trace or database tracked).

**Built (all free to test; 151 tests pass)**
- `evaluation/metrics.py`: one normalised metrics record for both implementations. One price table, tests assert it equals the two copies in the implementations. A value we could not observe is `None`. Platform runtime charge is kept separate from token charge; the platform-reported list cost is kept alongside.
- `evaluation/reproduce.py`: runs each `analysis.py` in a locked-down container (no network, non-root, read-only root, limits, hard timeout) with the CSV mounted at both implementations' hardcoded paths. It runs in a scratch copy, so `results/` is untouched. Real check on the three pilot runs: **all three scripts run and reproduce on the original CSV.**
- `evaluation/claims.py`: the numeric-claims check, redesigned (see below).
- `experiments/run_comparison.py`: alternating-order repeated runs, per-run and per-experiment cost caps, `--yes` required before any spend, finished runs skipped so an interrupted experiment resumes, `--report-only`, a report with mean (min-max) per measure and per-anomaly detection counts. `--model` must match `managed/agent.md` or the runner refuses, so the comparison cannot silently be unlike for like.
- `experiments/failure_scenarios.py`: the brief's reliability scenarios as micro-tasks with known answers, plus budget/iteration limits and a real kill-and-resume for both implementations. `--bare` was added to both `main.py` files so micro-tasks do not inherit the sales-report deliverables.

**Design problems found and what I decided**
- **The numeric-claims check cannot honestly say "incorrect".** Applied to real pilot output, my first version labelled 5 claims "incorrect" in one run. Looking at them: "19 rows, $8,801" was about the 19 deep-discount rows (a sub-selection the finding's segment does not express); a customer's "171 orders" was the customer total while the segment also listed a region; a product's "68 orders" included rows with a corrupted category that my category filter excluded. None were agent errors. A check that mislabels correct claims would damage the comparison more than it helps. It now tries the segment and every sub-combination of its keys, on four cleaning variants, and reports **supported / unsupported / unverifiable**. "Unsupported" does not mean wrong. Even so, coverage is low (only metrics whose name uses plain words like revenue, orders, rows, mean discount are checked) and a number can match by coincidence. I am recording this as a limitation of the evaluation, not a solved problem.
- A budget-accounting bug in the runner (`cost or cap` treats a genuine $0.00 as unknown) was caught by a test that passed for the wrong reason; fixed with an explicit `None` check and a test for it.
- `evaluation/` imports nothing from `diy/` or `managed/`; it only depends on the sandbox image built from `diy/Dockerfile`.

**Evidence of the three pilot runs under the new evaluator**: all produce valid artifacts, all `analysis.py` files re-run, recall 90% / 100% / 100%.

**LOC now:** DIY 625 (6 files + Dockerfile), Managed 246 (1 file + 2 config files), evaluation 395, experiments 420, tests 1,121.

**Time spent so far on Phase 5 tooling:** about 4h (estimate).

**Pending: paid runs, in two parts, each needing approval**
1. 5 + 5 repetitions of the main task, estimated $4.80 from the pilot means ($0.36 DIY, $0.60 managed), hard stop $8.00.
2. Failure scenarios, estimated $1.50-$2.00.

## 2026-10-12 — Correction: the "time spent" figures above are not measurements

Every "Time spent" line in this log (0.5h, 2.5h, 3h, 4h, 5h, 2h, 4h, 4h) was an estimate I wrote down without a timer, and they are inconsistent with the commit timestamps. For example Phase 1 is logged as about 2.5h, but the repository shows it started at 23:17 and was committed at 23:36 (19 minutes). Do not use those figures.

The only time data this project has is wall-clock timestamps from git and from the run files. They measure elapsed time between milestones, **including** time spent waiting for approvals, paid runs and tool output, so they are neither effort nor a fair "how long would a human take". Per-component effort (loop, sandbox, sessions, etc.) was **not tracked**, which the brief asked for, and cannot be reconstructed.

| Milestone commit | Time (IST, 2026-10-11/12) | Elapsed since previous |
|---|---|---|
| Brief committed | 23:14 | - |
| Phase 0 findings logged | 23:17 | 3 min |
| Phase 1 (dataset, evaluator) | 23:36 | 19 min |
| Phase 2 (DIY loop, tools, Docker sandbox) | 23:52 | 16 min |
| Phase 3 (sessions, recovery, retries) | 00:09 | 17 min |
| Phase 4 (Managed implementation) | 00:22 | 13 min after Phase 3 validation commit at 00:14 |
| Phase 4 fixes and first Managed run logged | 00:33 | 11 min |
| Phase 5 tooling | 00:45 | 12 min |

Other measured points: first complete DIY run finished about 23:56; first complete Managed session finished about 00:28 (session timestamps 18:53-18:58 UTC).

## 2026-10-12 — Phase 5 results: experiment and reliability scenarios

All runs approved by the user beforehand (experiment about $4.80 with an $8.00 stop; scenarios about $1.50-$2.00). Raw results are in `results/` (gitignored); the committed, metrics-only summary is in `docs/results/` and the write-up is in `docs/comparison.md` and `docs/findings.md`.

**Main experiment (5 + 5 runs, alternating order):** all 10 completed. Median active time 171 s (DIY) vs 271 s (Managed); median cost $0.34 vs $0.56; median recall 0.90 on both (Managed n=4 scored; rep 1 failed the `summary.json` schema with a boolean evidence value). `analysis.py` reran in a clean container for all 10. Spend $4.79.

**Reliability scenarios (one run per side each):** syntax error, missing file, tool error: handled by the model on both sides. Tool timeout: both wrote TIMEOUT, but the DIY agent was rejected by our command allowlist (`python3` is not allowed, `python` is) and wrote its own subprocess timeout, so our sandbox kill path was never exercised; Managed's bash tool returned "command timed out after 295s". Budget cap $0.05: DIY stopped at $0.0503 (our loop); Managed paused with `budget_reached` at $0.06 list cost. Iteration limit: DIY `max_iterations`; not applicable to Managed. Client killed with SIGKILL: DIY resumed, did not re-run the one in-flight call, completed; Managed re-attached and completed.

**Problems found while running**
1. The first launch of the failure scenarios never ran. The experiment runner's child processes inherited stdin and consumed the rest of the shell script that launched them. Fixed with `stdin=DEVNULL` everywhere, with a test.
2. A re-attach to a Managed session appended every stored event to the trace again (37 duplicates in the interrupted run). Fixed (a re-attach now rewrites the trace), with a test.
3. Managed's `missing_file` run took 63 s because the agent ran `find /` on the large cloud filesystem; DIY's took 8 s. A property of the agent's command and the sandbox, not an error.
4. The A09 (Apparel dip) anomaly was found by 2 of 2 pilot runs but by 1 of 5 per side in the experiment. Cause unknown; confirmed from the findings text that it is a real miss, not a matcher gap.

**Not measured:** whether the Managed session kept working while the client was dead; DIY local compute cost; per-component development effort.
