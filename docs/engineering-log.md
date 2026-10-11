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
