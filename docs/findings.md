# Findings

Answers to the nine questions in the project brief. Numbers and their sources are in [comparison.md](comparison.md); what went wrong along the way is in the [engineering log](engineering-log.md). Anything that is our judgement rather than a measurement is labelled as such.

**Who built this:** one person working with an AI coding assistant. Elapsed-time figures here are not estimates of what a human team would need.

## 1. How difficult was it to build a minimal autonomous agent with the Messages API?

The loop itself was not hard. The core `while` is about 45 lines (`diy/agent_loop.py:171-217`): call the model, read `stop_reason`, run every `tool_use` block, return all results in one message, repeat. The first complete, successful run on the real API finished 17 turns and cost about $0.36.

What takes care is what the loop assumes: tool results batched into a single user message with matching IDs; thinking blocks replayed exactly as received; a truncated reply never executed as a tool call; refusals and unexpected stop reasons handled. We have no timing for how long this took in effort. The wall-clock from the commit before to the first complete run was about 20 minutes, but that included a Docker sandbox and five tools, and was done with an AI assistant.

## 2. What infrastructure was required beyond the basic loop?

The basic loop was 171 lines of code in its file once retries, limits and bookkeeping were included. Around it: five tools with schema validation and path confinement (126 lines), a Docker sandbox (80 lines plus a Dockerfile), a SQLite session store with crash recovery (144 lines), a tracer, cost accounting, secret redaction, and an interrupt-and-resume path. That is 625 lines in five files, plus 47 tests.

Each piece had failure modes found one test at a time: a container wedged by unreaped zombie processes, Docker's own errors arriving as ordinary tool output, scratch files overwritten after a resume, and an orphaned container after the client was killed.

## 3. Which responsibilities did Managed Agents eliminate?

The agent loop, tool dispatch for built-in tools, the code-execution sandbox, session storage, and context management (including prompt caching; 0 compaction events in five runs, though the task never needed it). We wrote and tested all of these for DIY and none for Managed.

## 4. Which did it simplify?

Error handling (tool failures arrive flagged as errors; 0 `session.error` events in 568), observability (an event stream is provided), and execution limits (a platform-enforced dollar budget and a tool timeout). In each case we still wrote code on top, for example the trace writer and metrics.

## 5. What responsibilities remained with the application?

- Starting and watching the session: the event stream has no replay, so reconnecting means listing stored events and de-duplicating them.
- Moving files: uploading the input, and downloading outputs through a Files API that flattens directories and lists the uploaded input as non-downloadable.
- Policy: which tools, which permission mode, and the network setting (an environment created through the API defaults to unrestricted).
- Cost tracking and a wall-clock timeout.
- Checking the agent's output. Neither platform validates a `summary.json` or knows whether the findings are right. Evaluation was the largest code in the repository: 815 lines, against 171 for the DIY loop.

The managed client still came to 246 lines.

## 6. How did the implementations compare in cost, reliability and debugging?

**Cost.** Median per run: DIY $0.34, Managed $0.56 (range $0.33-0.45 and $0.42-0.73). Managed used more output tokens (33,485 vs 20,791) and more tool calls (25 vs 18). We did not isolate why; the tools, system prompt and sandbox all differ.

**Reliability.** All 10 experiment runs completed. Artifacts and schema were valid in 5 of 5 DIY runs and 4 of 5 Managed runs. In seven fault scenarios (one run each, one not applicable to Managed) both sides ended as intended; the difference was who enforced the limit. When the client was killed mid-run, our code resumed the session without re-running the in-flight call, and the managed session was re-attached and completed.

**Quality.** Median anomaly recall was 0.90 on both. Run-to-run variance was larger than any difference between them.

**Debugging.** On DIY the bugs were in sandboxing and recovery, which we found by writing tests for them. On Managed the bugs were in our client and in learning its file model. Visibility was good on both sides: Managed returns every tool input and result in its event stream. We did not evaluate the Console session viewer.

## 7. What surprised us?

- A `docker run --network none` container was not yet a sandbox: it stopped working after a fork loop because PID 1 never reaped zombies.
- In the timeout scenario, the DIY agent hit our own command allowlist and wrote its own timeout instead, so the sandbox's kill path was never tested. Prompt-injected faults test the agent and the harness together.
- The evaluator, not either agent, produced the most bugs: mis-keyed ground truth, a leaked answer in the task prompt, a correct finding scored as a miss, and a numeric-claim check that labelled correct claims "incorrect".
- An anomaly both pilot runs found was found by only 1 of 5 runs per side in the experiment.
- Our own price table had a cache price wrong. We noticed only because the platform reports its own cost.

## 8. When would we choose Managed Agents?

*Our judgement from this experiment, not a measured result.* When the generic runtime is enough: you want code execution and files without building a sandbox, you want sessions that survive your client crashing, and you want a spend ceiling enforced by someone else. It also fits teams that would rather learn an API than operate containers. Nothing in our results says it produces better analysis.

## 9. When would we prefer a self-managed harness?

*Also judgement; several of these were not tested.* When you need control the platform does not give you: your own tools and tool names, an iteration limit, your own recovery semantics, a specific network or filesystem policy, or compute you operate yourself. In our sample it was also cheaper and faster, though we did not establish why. Not tested: data residency, behaviour at scale, longer sessions, and the effect of Managed Agents being a beta service.

## What would change our conclusions

More runs per side; a second task and model; matched toolsets, so cost and time can be attributed; per-component effort tracked from the start; and an independent check of whether the Managed session keeps working while the client is down.
