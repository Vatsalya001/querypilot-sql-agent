# QueryPilot evaluation report

- **Model:** groq / `openai/gpt-oss-120b` (fast model `openai/gpt-oss-20b`)
- **Run:** 2026-10-16T02:09:31+05:30 - 4 of 38 cases (subset: case=['dbg_wrong_join_key', 'fu_furniture_top3', 'gen_top5_customers_delivered', 'opt_not_in_subquery']), 6 agent turns, 0.2 min total
- **Golden set:** `evals/golden.json` v1 (sha256 `09692ddd527c`); system prompts sha256 `41aea344629a`
- **Database unchanged after the run:** yes
- **Structured LLM output:** 18 LLM calls; 0 replies recovered from text, 0 JSON fallbacks

## Headline metrics

| Metric | Result |
|---|---|
| Execution accuracy (generation + follow-up) | 2/2 (100.0%) |
| Held-out execution accuracy (without the brief's few-shot examples) | 2/2 (100.0%) |
| Guardrail compliance | n/a |
| Optimize pass rate | 1/1 (100.0%) |
| Debug pass rate | 1/1 (100.0%) |
| Debug answers that explain the issue (problem + reason) | 1/1 (100.0%) |
| Intent accuracy | 4/4 (100.0%) |
| All cases | 4/4 (100.0%) |
| Average latency per LLM-backed turn | 2.3 s (p50 2.1 s, p90 2.8 s) |

Average time per LLM node (per turn that ran it, repairs included): detect_intent 0.6 s, generate_sql 1.0 s, explain_sql 0.6 s.

## By category

| Category | Cases | Passed | Accuracy | Avg latency per case |
|---|---:|---:|---:|---:|
| Generation | 1 | 1 | 100.0% | 1.9 s |
| Follow-up (multi-turn) | 1 | 1 | 100.0% | 6.7 s |
| Optimize | 1 | 1 | 100.0% | 2.8 s |
| Debug | 1 | 1 | 100.0% | 2.1 s |

## Failures

None.

## All cases

| Case | Category | Result | Status | Intent | Attempts | Latency | Detail |
|---|---|---|---|---|---:|---:|---|
| `gen_top5_customers_delivered` | generation | PASS | success | generate_sql | 1 | 1.9 s | match (+1 extra column) |
| `fu_furniture_top3` | follow_up | PASS | success | modify_sql | 1 | 6.7 s | match (+6 extra columns) |
| `opt_not_in_subquery` | optimize | PASS | success | optimize_sql | 1 | 2.8 s | equivalent rewrite; 2 changes, 0 suggestions |
| `dbg_wrong_join_key` | debug | PASS | success | debug_sql | 1 | 2.2 s | match |

## Scoring

- Generation and follow-up: the agent's SQL and the gold SQL are executed on the sample database and their result sets compared (values only: column names and order are ignored, extra columns are allowed, numbers are compared at 2 decimals; row order only matters for ordering / top-N questions). Follow-ups run every turn on one conversation thread and score the last turn.
- Debug: the corrected SQL must pass validation and return the gold result set.
- Optimize: the rewrite must pass validation, return the same result set as the original query, and report at least one change or suggestion.
- Guardrails: the expected status (refused / rejected / clarification), no SQL in the reply, and the exact brief message for out-of-scope requests.
- Latency is wall-clock time per agent turn, measured sequentially after the prompt-cache warm-up; turns blocked by the deterministic input guard never reach the LLM and are excluded from the LLM-backed average.
