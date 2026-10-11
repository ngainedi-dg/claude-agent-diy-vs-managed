
# Project Brief: Claude Agent — DIY vs. Managed

**Repository:** `claude-agent-diy-vs-managed`  
**Project type:** Engineering experiment / public GitHub repository  
**Primary language:** Python  
**Platform:** Anthropic Claude Messages API and Claude Managed Agents  
**Status:** Proposed

---

## 1. Executive Summary

Build two implementations of the same autonomous AI agent:

1. **DIY Agent:** Built using the Claude Messages API, with our own agent execution loop, tool orchestration, sandbox, session management, and error handling.
2. **Managed Agent:** Built using Anthropic Managed Agents, relying on Anthropic's managed execution infrastructure.

Both agents will receive the same dataset and natural-language instructions. They must autonomously investigate the data, write and execute Python code, inspect the results, recover from errors, and generate analytical reports.

The objective is not primarily to compare the intelligence or accuracy of the agents. Both use Claude.

The objective is to understand and demonstrate **how much engineering infrastructure Anthropic Managed Agents eliminates when building an autonomous AI application.**

The project should produce:

- Two working agent implementations.
- A reproducible synthetic dataset.
- A common evaluation framework.
- Execution traces and performance metrics.
- A documented comparison of engineering effort.
- A public GitHub repository.
- Evidence and insights suitable for a LinkedIn post.

The final comparison must distinguish between infrastructure that Managed Agents eliminates, infrastructure that it simplifies, and responsibilities that remain with the application developer.

---

## 2. Motivation

Calling an LLM through an API is straightforward.

Building an autonomous agent is more complicated.

An autonomous agent must repeatedly:

1. Reason about the task.
2. Decide what action to take.
3. Invoke a tool.
4. Execute that tool.
5. Observe the result.
6. Decide what to do next.
7. Continue until the task is complete.

Supporting this behavior requires infrastructure beyond a simple model API call.

Potential responsibilities include:

- Agent execution loop.
- Tool invocation and result handling.
- Code execution environment.
- Sandbox isolation and security.
- Conversation and session state.
- Long-running task management.
- Error handling and retries.
- Execution limits and timeouts.
- Context-window management.
- Observability and execution tracing.
- Artifact management.
- Recovery from interruptions.

Anthropic Managed Agents provides a managed runtime for many of these responsibilities.

This experiment will compare building these capabilities ourselves against using the managed service.

### Central research question

**How much engineering work does Anthropic Managed Agents eliminate when building an application that autonomously writes code, executes it, examines results, and decides what to do next?**

### Working hypothesis

Managed Agents substantially reduces the engineering required for generic agent execution infrastructure.

However, application-specific tools, permissions, validation, business logic, and evaluation remain the developer's responsibility.

The experiment must test this hypothesis rather than assume it.

---

## 3. Scope and Non-Goals

### In scope

- Implementing a minimal autonomous agent using the Messages API.
- Implementing the same agent using Managed Agents.
- Providing both agents with code execution capabilities.
- Running both against identical analytical tasks.
- Comparing engineering effort and operational responsibilities.
- Evaluating task completion, runtime behavior, and cost.
- Documenting architectural differences.
- Publishing a reproducible repository.

### Out of scope

- Building a production-grade agent platform.
- Building a sophisticated web application.
- Supporting multiple LLM providers.
- Implementing distributed agent orchestration.
- Building a general-purpose workflow engine.
- Supporting enterprise authentication and authorization.
- Creating an exhaustive benchmark of Claude's reasoning ability.
- Reproducing every feature of Anthropic Managed Agents.

The DIY implementation should be reasonably safe and functional, but it is not intended to replicate a production managed service.

---

## 4. Experiment: Autonomous Sales Data Analyst

### 4.1 Task description

Build an autonomous agent that analyzes a synthetic sales dataset.

The user provides a CSV file and the following task:

> Analyze the supplied sales transactions. Explore the dataset, identify meaningful trends and anomalies, investigate anything suspicious, generate useful visualizations, and produce a concise report explaining your findings.
>
> Write and execute Python code as needed. Inspect the results of your analysis and revise your approach when necessary.
>
> You decide how to approach the investigation and when the analysis is sufficiently complete.

The agent should autonomously:

1. Discover the dataset schema.
2. Inspect data types and distributions.
3. Identify relevant analytical questions.
4. Write Python analysis code.
5. Execute the code.
6. Examine the results.
7. Investigate unexpected findings.
8. Recover from execution errors.
9. Generate charts.
10. Produce a written report.

Do not prescribe the exact analytical sequence.

The agent must make meaningful decisions about what to investigate and which tools to use.

### 4.2 Dataset

Generate approximately 10,000 synthetic sales transactions covering one year.

Required fields:

| Field | Description |
|---|---|
| transaction_id | Unique transaction identifier |
| date | Transaction date |
| customer_id | Customer identifier |
| product_id | Product identifier |
| category | Product category |
| region | Sales region |
| quantity | Units sold |
| unit_price | Price per unit |
| discount_pct | Discount percentage |
| revenue | Recorded transaction revenue |

The generator should use a fixed random seed and produce deterministic output.

Include realistic variation in:

- Sales volume.
- Product mix.
- Regional performance.
- Seasonal demand.
- Customer purchasing behavior.
- Discounts.
- Transaction sizes.

### 4.3 Seeded anomalies

Introduce approximately 10–15 known anomalies, including:

- An unexpected revenue decline in one region.
- Unusually high discounts for selected products.
- A sudden increase in transaction volume.
- Revenue values inconsistent with quantity, price, and discount.
- Suspiciously large transactions.
- Missing or inconsistent product categories.
- A customer with unusual purchasing behavior.
- A temporary drop in sales for a particular product category.

Include a few unusual but legitimate patterns to test whether the agent distinguishes anomalies from genuine business variation.

Create a separate ground-truth file identifying the seeded anomalies.

The agent must never receive the ground-truth file.

Ground truth should distinguish injected data-quality defects from unusual but legitimate business patterns. Some findings may be probabilistic rather than objectively wrong.

---

## 5. Architecture

The project contains two independent execution paths.

```text
                  User Task + CSV Dataset
                           |
              +------------+------------+
              |                         |
              v                         v
        DIY Agent                  Managed Agent
              |                         |
        Messages API             Managed Agents API
              |                         |
        Custom Agent Loop         Managed Runtime
              |                         |
        Tool Dispatcher           Built-in Tool Execution
              |                         |
        Docker Sandbox            Managed Sandbox
              |                         |
        Session Management        Managed Sessions
              |                         |
              +------------+------------+
                           |
                           v
                  Generated Artifacts
                           |
                           v
                    Common Evaluator
                           |
                           v
                  Comparison Report
```

Both implementations should use:

- The same Claude model, where supported.
- The same dataset.
- The same task instructions.
- Equivalent execution capabilities.
- The same output contract.
- The same evaluation logic.

Do not introduce a shared agent framework that hides the differences between the two implementations.

The purpose is to expose those differences clearly.

---

## 6. Implementation A: DIY Agent

### 6.1 Technical requirements

Use the Claude Messages API directly.

Do not use:

- LangChain.
- LangGraph.
- CrewAI.
- AutoGen.
- Claude Agent SDK.
- Any framework that implements the agent execution loop.

Use the official Anthropic Python SDK for API access.

### 6.2 Agent execution loop

Implement a loop that:

1. Sends the current conversation to Claude.
2. Examines the response.
3. Detects tool-use requests.
4. Validates requested tool calls.
5. Executes the requested tools.
6. Captures tool results.
7. Returns those results to Claude.
8. Updates conversation history.
9. Repeats until completion or an execution limit is reached.

Conceptual pseudocode:

```python
while not finished:
    response = call_claude(conversation)

    if response.requests_tools:
        for tool_call in response.tool_calls:
            result = execute_tool(tool_call)
            conversation.append(result)

    elif response.is_complete:
        finished = True

    check_execution_limits()
```

The actual implementation must follow the Messages API protocol, including tool-use IDs, content blocks, tool-result messages, and stop reasons.

Handle multiple tool requests in one response.

Avoid duplicate execution of tool calls during retries or recovery.

### 6.3 Agent tools

Implement the following tools:

**list_files**

Lists files available in the task workspace.

**read_file**

Reads a text file or a bounded portion of a file.

**write_file**

Creates or updates files in the permitted workspace.

**execute_python**

Executes Python code inside the sandbox and returns:

- Standard output.
- Standard error.
- Exit code.
- Execution duration.

**run_command**

Executes a restricted command inside the sandbox.

Avoid implementing unrestricted shell execution on the host.

All paths must be validated to prevent access outside the permitted workspace.

### 6.4 Execution sandbox

Use Docker for isolated code execution.

The sandbox should contain:

- Python.
- pandas.
- numpy.
- matplotlib.
- Standard Python libraries.

Security requirements:

- Run as a non-root user.
- Disable network access during execution.
- Mount the source dataset read-only.
- Restrict filesystem access.
- Set CPU and memory limits.
- Set process limits.
- Enforce command timeouts.
- Never expose API keys inside the container.
- Never mount the Docker socket.
- Use a fresh sandbox for each experimental run.

The agent should be able to write scripts and artifacts inside an explicitly writable workspace.

Document the limitations of Docker isolation. Do not describe this as a production-grade sandbox for executing untrusted code.

### 6.5 Session management

Implement minimal session persistence.

Each session should track:

- Session ID.
- Task instructions.
- Conversation history.
- Tool invocations.
- Tool results.
- Start timestamp.
- End timestamp.
- Current execution status.
- Errors.
- Output artifacts.
- Usage information.

Use SQLite or JSON files.

Support basic recovery after interruption where practical.

Do not automatically repeat an operation if its prior execution status is uncertain.

### 6.6 Error handling

Handle:

- API failures.
- Tool failures.
- Invalid tool arguments.
- Python syntax errors.
- Missing files.
- Execution timeouts.
- Sandbox failures.
- Maximum iteration limits.
- Maximum execution duration.
- Budget exhaustion.

Recoverable tool errors should be returned to Claude so it can decide whether to retry or change its approach.

Infrastructure failures should be recorded distinctly.

### 6.7 Context management

Initially maintain the complete conversation history.

Track token usage and context growth.

If context limits become a problem, implement a simple compaction or summarization strategy.

Document any context-management logic that was necessary.

Do not build a sophisticated long-term memory system.

### 6.8 Observability

Produce a structured execution trace.

For each step, capture:

- Timestamp.
- Model response.
- Requested tool.
- Tool arguments, with sensitive values redacted.
- Tool result.
- Execution duration.
- Errors.
- Token usage, where available.

Store traces as JSONL.

Provide a human-readable summary of the execution.

---

## 7. Implementation B: Managed Agents

### 7.1 Technical requirements

Use Anthropic's official Managed Agents API.

Documentation:

https://platform.claude.com/docs/en/managed-agents/quickstart

Do not substitute the Claude Agent SDK for Managed Agents.

Before implementation, verify:

- Account eligibility.
- Required API access.
- Supported models.
- SDK version.
- Beta headers, if required.
- Available managed tools.
- Sandbox capabilities.
- Session APIs.
- Artifact upload and download mechanisms.
- Pricing and usage reporting.

Managed Agents is a beta service. Follow the current documentation rather than assuming specific API names or schemas.

### 7.2 Agent configuration

Create a reusable managed agent configured with:

- The selected Claude model.
- The same task instructions as the DIY agent.
- File access.
- Code execution capabilities.
- Appropriate permission restrictions.
- Necessary execution limits.

Prefer built-in managed capabilities over custom implementations.

### 7.3 Environment

Use an Anthropic-hosted execution environment.

Ensure that:

- The CSV dataset is available to the agent.
- Python execution is supported.
- Required analysis libraries are available.
- Generated files can be retrieved.
- Network access is restricted where supported.

If dependencies must be installed or configured, document that effort.

### 7.4 Session execution

Implement the following lifecycle:

1. Create or reuse the agent configuration.
2. Create or reuse the managed environment.
3. Start a session.
4. Supply the dataset and task instructions.
5. Observe execution through streamed events or documented polling APIs.
6. Detect completion or failure.
7. Retrieve generated artifacts.
8. Capture usage and execution metrics.
9. Save a sanitized execution trace.

Use the current documented API lifecycle.

### 7.5 Application responsibilities

The Managed Agents implementation should only contain code required to:

- Configure the agent.
- Configure the environment.
- Submit the task.
- Supply input files.
- Observe execution.
- Handle application-level errors.
- Retrieve outputs.
- Record metrics.

Do not recreate the generic agent execution loop locally.

Do not implement a separate local sandbox for the managed version.

The purpose is to measure how much infrastructure is delegated to Anthropic.

---

## 8. Common Output Contract

Both agents must produce the following artifacts.

### report.md

A concise analytical report containing:

- Executive summary.
- Important sales trends.
- Identified anomalies.
- Supporting numerical evidence.
- Relevant charts.
- Uncertainty and limitations.
- Recommended follow-up investigations.

### analysis.py

A reproducible Python script containing the analytical code.

The script should run against the original dataset without requiring the agent session.

### charts/

At least two useful visualizations.

Examples:

- Revenue by month.
- Revenue by region.
- Discount distribution.
- Category performance.
- Anomaly investigation.

### summary.json

Structured findings in a documented schema.

Each finding should include:

- Finding identifier.
- Description.
- Relevant data segment.
- Supporting evidence.
- Confidence or uncertainty.
- Whether further investigation is recommended.

The evaluator should validate this schema.

Do not force the agent to produce a predetermined list of findings.

---

## 9. Evaluation Framework

Build a common evaluation system independent of both agent implementations.

### 9.1 Engineering effort

Record actual development time for:

| Component | DIY | Managed |
|---|---|---|
| API integration | Measure | Measure |
| Agent loop | Measure | Managed |
| Tool orchestration | Measure | Managed |
| Sandbox provisioning | Measure | Configure |
| Session management | Measure | Integrate |
| Error handling | Measure | Measure |
| Execution tracing | Measure | Measure |
| Artifact handling | Measure | Measure |
| Debugging | Measure | Measure |

Also record:

- Source files.
- Lines of code.
- Dependencies.
- Configuration files.
- Setup steps.
- Manual operations required.

Define a consistent LOC measurement methodology.

Exclude generated datasets, lockfiles, and documentation from application LOC.

Do not interpret fewer lines of code as proof of better architecture.

### 9.2 Runtime metrics

Capture:

- Total execution duration.
- Model calls, where observable.
- Tool calls, where observable.
- Input tokens.
- Output tokens.
- Total API cost.
- Managed runtime charges, if applicable.
- Execution errors.
- Completion status.
- Number of generated artifacts.

Where a metric is unavailable, mark it as unavailable rather than zero.

Separate API charges from infrastructure charges.

### 9.3 Output quality

Evaluate:

- Required artifact completion.
- Whether generated Python executes successfully.
- Whether numerical claims are correct.
- Whether seeded anomalies were detected.
- False-positive findings.
- Whether the agent acknowledges uncertainty.
- Whether the report is supported by evidence.

Use deterministic checks wherever possible.

Do not rely solely on another LLM to judge the results.

### 9.4 Reliability tests

Test the following scenarios:

1. Generated Python contains a syntax error.
2. Input file is missing.
3. Tool execution times out.
4. Tool execution returns an error.
5. The client process is interrupted.
6. The agent exceeds its iteration or budget limit.
7. The task requires multiple rounds of code execution.

Record whether the failure is handled by:

- The model.
- The application.
- The execution runtime.
- A combination of these.

For tests requiring injected faults, use equivalent controlled conditions wherever possible. Document cases where the two environments cannot be tested identically.

---

## 10. Experimental Methodology

### Stage 1: Basic functional test

Run each implementation once.

Verify that both:

- Inspect the dataset.
- Execute Python.
- Perform iterative analysis.
- Generate charts.
- Produce the required artifacts.

### Stage 2: Repeated runs

Run each implementation five times.

Use:

- The same model version.
- The same dataset.
- The same task instructions.
- Fresh sessions.
- Isolated execution environments.
- Equivalent execution budgets where possible.

Record results for every attempt, including failures.

### Stage 3: Failure testing

Execute the reliability scenarios.

Record:

- What failed.
- How the failure was detected.
- Whether recovery occurred.
- What application code was involved.
- Whether the managed runtime simplified recovery.

### Stage 4: Engineering comparison

Compare the implementation responsibilities.

Classify each capability as:

**Eliminated:** No application-side implementation required.

**Simplified:** Still requires integration or configuration, but substantially less infrastructure.

**Still required:** Remains the developer's responsibility.

### Stage 5: Findings

Prepare a concise technical report based on measured results.

Do not assume the managed version will always be faster, cheaper, or more reliable.

---

## 11. Repository Structure

```text
claude-agent-diy-vs-managed/
|
|-- README.md
|-- PROJECT_BRIEF.md
|-- pyproject.toml
|-- .env.example
|-- .gitignore
|
|-- task/
|   |-- instructions.md
|   |-- generate_data.py
|   |-- schema.md
|
|-- diy/
|   |-- __init__.py
|   |-- main.py
|   |-- agent_loop.py
|   |-- tools.py
|   |-- sandbox.py
|   |-- sessions.py
|   |-- Dockerfile
|
|-- managed/
|   |-- __init__.py
|   |-- main.py
|   |-- agent.md
|   |-- environment.yaml
|
|-- evaluation/
|   |-- __init__.py
|   |-- evaluate.py
|   |-- metrics.py
|   |-- ground_truth.py
|
|-- experiments/
|   |-- __init__.py
|   |-- run_comparison.py
|   |-- failure_scenarios.py
|
|-- tests/
|   |-- test_diy.py
|   |-- test_managed.py
|   |-- test_evaluation.py
|
|-- results/
|   |-- .gitkeep
|
|-- docs/
    |-- architecture.md
    |-- engineering-log.md
    |-- comparison.md
    |-- findings.md
```

The structure is a starting point.

Consolidate files if doing so improves clarity.

Do not introduce unnecessary architectural layers.

---

## 12. Command-Line Interface

Provide commands equivalent to:

```bash
# Generate the synthetic dataset
uv run python task/generate_data.py --seed 42

# Run the DIY agent
uv run python -m diy.main \
  --task task/instructions.md

# Run the Managed Agent
uv run python -m managed.main \
  --task task/instructions.md

# Evaluate generated outputs
uv run python -m evaluation.evaluate \
  --results results/

# Generate comparison report without paid API calls
uv run python -m experiments.run_comparison \
  --report-only
```

The comparison runner should support:

- Number of repetitions.
- Selected model.
- Execution timeout.
- Maximum tool iterations, where applicable.
- Maximum experiment budget.
- Output directory.

Do not initiate paid API calls during installation, unit testing, or report-only operations.

---

## 13. Development Plan

### Phase 1: Repository and dataset

Implement:

- Project scaffolding.
- Dependency management.
- Dataset generator.
- Ground-truth generation.
- Common task instructions.
- Output schema.
- Basic evaluator.

Acceptance criteria:

- Dataset generation is deterministic.
- Seeded anomalies are documented.
- Ground truth is inaccessible to the agents.
- Evaluation tests pass.

### Phase 2: Minimal DIY agent

Implement:

- Messages API integration.
- Basic agent loop.
- Tool-call processing.
- Python execution tool.
- Conversation history.
- Completion detection.

Acceptance criteria:

Claude can independently write Python code, execute it, inspect the result, and decide what to do next.

Do not proceed until this works.

### Phase 3: DIY infrastructure

Implement:

- Docker sandbox.
- File access restrictions.
- Execution limits.
- Session persistence.
- Structured logging.
- Error handling.
- Basic recovery behavior.

Acceptance criteria:

The DIY agent can complete the analytical task without executing generated code directly on the host.

Document implementation effort.

### Phase 4: Managed Agents

Implement:

- Managed agent configuration.
- Managed environment.
- Session creation.
- Task submission.
- Execution monitoring.
- Artifact retrieval.
- Usage reporting.

Acceptance criteria:

The Managed Agent completes the same analytical task without a locally implemented agent loop or sandbox manager.

Document implementation effort.

### Phase 5: Comparison experiments

Implement:

- Repeatable execution runner.
- Metrics collection.
- Functional evaluation.
- Failure testing.
- Comparison report generation.

Acceptance criteria:

Both implementations have recorded runs and comparable evaluation outputs.

### Phase 6: Documentation and publication

Prepare:

- README.
- Architecture diagrams.
- Setup instructions.
- Engineering diary.
- Comparison tables.
- Findings.
- Known limitations.
- Reproducibility instructions.

Acceptance criteria:

An independent developer can clone the repository, configure an API key, and reproduce the experiment.

---

## 14. Engineering Diary

Maintain `docs/engineering-log.md` throughout development.

This is a critical project artifact.

Record:

- Date and development phase.
- Work attempted.
- Actual time spent, when known.
- Unexpected complications.
- Debugging effort.
- Design decisions.
- Infrastructure components implemented.
- Infrastructure components avoided through Managed Agents.
- Limitations discovered.
- Interesting observations.

Do not reconstruct the diary from memory at the end.

Record observations as they happen.

The diary should make it possible to explain the actual experience of building both implementations.

---

## 15. Cost and Security Controls

### Cost

Before running repeated experiments:

1. Identify current API pricing.
2. Identify Managed Agents runtime charges.
3. Estimate the cost of the proposed runs.
4. Obtain explicit approval for paid experiments.
5. Set conservative execution budgets.
6. Track actual charges where available.

Do not assume that API promotional credits cover all managed-runtime charges.

### Secrets

- Read API keys from environment variables.
- Provide `.env.example`.
- Never commit `.env`.
- Never print API keys.
- Redact credentials from logs.
- Never pass credentials into the DIY code-execution sandbox.

### Data

Use only synthetic data.

Do not use customer information or proprietary DataGrokr datasets.

### Publishing

Do not push to GitHub without explicit approval.

Review execution traces for secrets before publishing.

---

## 16. Required Final Deliverables

### A. Working implementations

Both the DIY and Managed Agents versions must complete the common task.

### B. Reproducible dataset

Include the dataset generator and documented anomaly definitions.

### C. Evaluation harness

Provide repeatable scoring and comparison metrics.

### D. Engineering comparison

Create `docs/comparison.md` covering:

- Infrastructure responsibilities.
- Engineering effort.
- Code complexity.
- Operational complexity.
- Runtime behavior.
- Costs.
- Reliability.
- Limitations.

### E. Findings

Create `docs/findings.md` answering:

1. How difficult was it to build a minimal autonomous agent with the Messages API?
2. What infrastructure was required beyond the basic execution loop?
3. Which responsibilities did Managed Agents eliminate?
4. Which responsibilities did Managed Agents simplify?
5. What responsibilities remained with the application?
6. How did the implementations compare in cost, reliability, and debugging?
7. What surprised us?
8. When would we choose Managed Agents?
9. When would we prefer a self-managed harness?

### F. Public-facing README

The README should explain:

- What the project demonstrates.
- Why the comparison matters.
- How the two approaches work.
- How to run the experiment.
- Key measured findings.
- Limitations of the comparison.

Avoid marketing language.

Do not claim that Managed Agents is superior unless the results support that conclusion.

---

## 17. Success Criteria

The project is successful if:

- Both agents autonomously complete the same analytical task.
- The DIY version genuinely implements its own execution loop.
- The Managed version genuinely uses Anthropic Managed Agents.
- The comparison is technically fair.
- Engineering effort is recorded.
- Runtime metrics are collected.
- Differences in infrastructure responsibilities are clearly documented.
- The repository is reproducible.
- The findings support an insightful technical LinkedIn post.

The project does not need to demonstrate that one implementation produces better analytical results.

The primary measure of success is how clearly it demonstrates the engineering responsibilities involved in building an autonomous agent.

---

## 18. Guidance for Claude Code

Follow these principles throughout implementation.

### Keep the DIY implementation minimal

Do not build a general-purpose agent framework.

Implement only the capabilities needed for the experiment.

### Do not handicap the DIY implementation

Use reasonable engineering practices and standard libraries.

The comparison should reflect a competent developer building a small agent.

### Use Managed Agents as intended

Prefer Anthropic's built-in runtime capabilities.

Do not recreate functionality that the managed service already provides.

### Keep the implementations understandable

Favor explicit code over elaborate abstractions.

A reader should be able to understand the DIY agent loop by reading a small number of files.

### Maintain the engineering diary

Capture actual complications and discoveries.

The development experience is part of the experiment.

### Validate assumptions

Managed Agents is a beta service.

Consult current documentation before implementing API integrations.

If a capability described in this brief is unavailable, document the limitation and propose the simplest alternative.

Do not silently substitute another product.

### Work incrementally

Complete and test each phase before moving on.

Do not build the entire project in one pass without verification.

### Control costs

Do not execute paid experiments without approval.

### Avoid unnecessary UI work

A CLI and Markdown reports are sufficient.

Do not build a dashboard unless it materially improves the comparison.

---

## 19. LinkedIn Post Objective

The project should produce firsthand insights for a LinkedIn post about Anthropic Managed Agents.

The intended angle is:

**"I tried building an autonomous agent myself. Then I tried Anthropic Managed Agents. Here's how much infrastructure I no longer had to build."**

The post should not be a generic product announcement.

It should explain:

- What building an autonomous agent actually involves.
- What was surprisingly easy.
- What was unexpectedly complicated.
- Which responsibilities Anthropic removed.
- What developers still need to build.
- Why managed agent runtimes change the economics of agentic applications.

Include a link to the public GitHub repository.

Do not write the final LinkedIn post until the experiment is complete and the results are available.

---

## 20. Initial Execution Instructions

Begin by:

1. Reading this entire project brief.
2. Reviewing the current Anthropic Messages API documentation.
3. Reviewing the current Anthropic Managed Agents documentation.
4. Verifying API availability and compatibility.
5. Identifying any technical assumptions requiring confirmation.
6. Producing a concise implementation plan.
7. Creating the repository structure.
8. Implementing the synthetic dataset and evaluator.
9. Building the minimal DIY agent.
10. Proceeding incrementally through the remaining phases.

Before paid API execution, show the estimated cost and obtain approval.

Before pushing to GitHub, obtain approval.

The ultimate objective is not merely two functioning agents.

It is a technically credible, reproducible demonstration of how much agent infrastructure Anthropic Managed Agents takes off a developer's plate.
