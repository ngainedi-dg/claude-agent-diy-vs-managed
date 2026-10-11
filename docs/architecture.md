# Architecture

Two independent execution paths share one task, one dataset and one evaluator. Nothing hides the differences between them: there is no common agent framework.

```text
                 task/instructions.md + task/system.md + sales_transactions.csv
                                       |
                  +--------------------+--------------------+
                  |                                         |
             DIY (diy/)                              Managed (managed/)
                  |                                         |
     Messages API, our own loop                  Managed Agents API
     agent_loop.py                               main.py: start, watch, collect
                  |                                         |
     tools.py: validate and dispatch             agent_toolset_20260401 (bash, read, write...)
                  |                                         |
     sandbox.py: Docker container                Anthropic-hosted cloud sandbox
                  |                                         |
     sessions.py: SQLite                         server-side session
                  |                                         |
                  +--------------------+--------------------+
                                       |
          outputs: report.md, analysis.py, charts/, summary.json
                                       |
           evaluation/ (never imports diy/ or managed/)
           answer-key matching, schema, numeric claims, metrics, analysis.py re-run
                                       |
                    experiments/ -> results, reports
```

## What is shared

- **Task text:** `task/instructions.md` and the system prompt `task/system.md`. A test checks that `managed/agent.md` carries the same system prompt. The only difference is a short "Environment" paragraph giving each side's file paths.
- **Model and defaults:** both run `claude-sonnet-5-5` at its default effort. The experiment runner refuses to run if `managed/agent.md` names a different model.
- **Output contract:** `task/instructions.md` and `task/summary.schema.json`.
- **Answer key isolation:** the dataset goes to `task/data/`; the answer key to `evaluation/data/`. Only the CSV is mounted or uploaded. A test scans the task prompt for answer-key terms.

## DIY (`diy/`)

**`agent_loop.py`** is the loop. Each pass sends the conversation, reads `stop_reason`, runs every `tool_use` block, and returns all results in one user message with matching IDs. The loop body is `agent_loop.py:171-217`. Response content, including thinking blocks, is stored and replayed unchanged. It handles `end_turn`, `tool_use`, `max_tokens` (a truncated tool call is never run), `refusal` and `pause_turn`. API failures that survive the SDK's own retries get 5, 20 and 60 second backoff; non-retryable errors fail immediately. Limits: iterations, wall-clock time and an estimated-cost ceiling.

**`tools.py`** defines five tools (`list_files`, `read_file`, `write_file`, `execute_python`, `run_command`). Arguments are validated against JSON Schema before anything runs. Paths are confined to `/workspace` (read-write) and `/data` (read-only). `run_command` is an allowlist with no shell. Problems the model can act on come back as error results; sandbox failures are raised separately.

**`sandbox.py`** runs one container per run, driven by `docker exec`: no network, non-root, all capabilities dropped, read-only root filesystem, CPU, memory and process limits, `--init` so zombies are reaped, and a hard timeout enforced inside the container. No API key enters it. Docker isolation is not a production sandbox for untrusted code and is not described as one.

**`sessions.py`** is a SQLite store: messages, tool calls, usage, errors and status. A tool call is recorded `started` before it runs and `done` after. On resume, finished calls are reused and calls still `started` are never re-run, because they may have taken effect; the model is told the outcome is unknown. All text is redacted before it is stored.

## Managed (`managed/`)

`agent.md` and `environment.yaml` define the agent and environment (the agent file is in `ant apply` format). `main.py` does only application-side work:

1. **Create or reuse** the agent and environment. IDs are kept in a git-ignored state file; editing `agent.md` creates a new agent version.
2. **Upload** the CSV and **create a session** with the file mounted, a dollar budget, and the task as `initial_events`.
3. **Watch** the event stream. It has no replay, so after each (re)connect the stored events are listed and de-duplicated by ID.
4. **Interrupt** if our wall-clock timeout passes.
5. **Collect** outputs through the Files API (rebuilding `charts/`, which the listing flattens) and **record** usage.
6. **`--attach`** re-joins a session after the client died.

The environment sets `networking: limited` with no hosts and no package managers.

## Evaluation (`evaluation/`)

Deterministic only; no LLM judge. `ground_truth.py` matches findings to the answer key with segment, date, transaction-ID and keyword rules. `claims.py` checks the narrow set of reported numbers it can recompute. `metrics.py` normalises usage and cost for both sides with one price table. `reproduce.py` re-runs each `analysis.py` in a locked-down container. It never runs agent code on the host.

## Experiment runners (`experiments/`)

`run_comparison.py` runs repeated alternating-order runs with cost caps and builds the report. `failure_scenarios.py` injects seven faults. `loc.py` counts code with one fixed method. `estimate_cost.py` projects cost from the free token-counting endpoint. Both runners need `--yes` before spending money.
