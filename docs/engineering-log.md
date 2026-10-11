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
