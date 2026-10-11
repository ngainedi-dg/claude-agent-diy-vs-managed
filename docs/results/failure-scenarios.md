# Reliability scenarios

Facts recorded by the runner. Who handled each failure is judged from the traces in docs/comparison.md.

## syntax_error

Expected: the run completes and result.txt says OK

| | diy | managed |
|---|---|---|
| status | completed | completed |
| result_txt | OK | OK |
| model_calls | 3 | 3 |
| tool_calls | 2 | 2 |
| tool_calls_with_error_result | 1 | - |
| infrastructure_errors | 0 | 0 |
| active_seconds | 6.6 | 6.725 |
| cost_usd | 0.0066 | 0.0061 |

## missing_file

Expected: result.txt says MISSING, with no invented row count

| | diy | managed |
|---|---|---|
| status | completed | completed |
| result_txt | MISSING | MISSING |
| model_calls | 4 | 3 |
| tool_calls | 3 | 2 |
| tool_calls_with_error_result | 1 | - |
| infrastructure_errors | 0 | 0 |
| active_seconds | 7.7 | 62.814 |
| cost_usd | 0.0084 | 0.0076 |

## tool_timeout

Expected: observed only: the two sandboxes enforce different, undocumented-for-us tool timeouts

| | diy | managed |
|---|---|---|
| status | completed | completed |
| result_txt | TIMEOUT | TIMEOUT |
| model_calls | 4 | 3 |
| tool_calls | 3 | 2 |
| tool_calls_with_error_result | 1 | - |
| infrastructure_errors | 0 | 0 |
| active_seconds | 304.6 | 301.924 |
| cost_usd | 0.0173 | 0.023 |

## tool_error

Expected: result.txt says 2

| | diy | managed |
|---|---|---|
| status | completed | completed |
| result_txt | 2 | 2 |
| model_calls | 3 | 3 |
| tool_calls | 2 | 2 |
| tool_calls_with_error_result | 1 | - |
| infrastructure_errors | 0 | 0 |
| active_seconds | 6.2 | 6.316 |
| cost_usd | 0.006 | 0.005 |

## budget_limit

| | diy | managed |
|---|---|---|
| status | budget_exhausted | budget_exhausted |
| model_calls | 5 | 4 |
| tool_calls | 5 | 4 |
| tool_calls_with_error_result | 1 | - |
| infrastructure_errors | 0 | 0 |
| active_seconds | 21.7 | 24.79 |
| cost_usd | 0.0503 | 0.0594 |

## iteration_limit

| | diy | managed |
|---|---|---|
| status | max_iterations | Managed Agents has no iteration limit to set. |
| model_calls | 3 | - |
| tool_calls | 3 | - |
| tool_calls_with_error_result | 1 | - |
| infrastructure_errors | 0 | - |
| active_seconds | 10.8 | - |
| cost_usd | 0.0151 | - |

## client_interrupted

| | diy | managed |
|---|---|---|
| status | completed | completed |
| model_calls | 17 | 17 |
| tool_calls | 20 | 21 |
| tool_calls_with_error_result | 2 | - |
| infrastructure_errors | 0 | 0 |
| active_seconds | 177.8 | 235.598 |
| cost_usd | 0.3511 | 0.4874 |
| interruption | {'killed_after_tool_calls': 6} | {'killed_after_tool_calls': 7} |
