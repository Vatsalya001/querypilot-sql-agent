# QueryPilot evaluation report

- **Model:** groq / `openai/gpt-oss-120b` (fast model `openai/gpt-oss-20b`)
- **Run:** 2026-10-16T02:10:15+05:30 - 1 of 21 cases (subset: case=['dev_fu_group_by_month']), 2 agent turns, 0.1 min total
- **Golden set:** `evals/dev.json` v1 (sha256 `45a6a6b5e675`); system prompts sha256 `41aea344629a`
- **Database unchanged after the run:** yes
- **Structured LLM output:** 6 LLM calls; 0 replies recovered from text, 0 JSON fallbacks

## Headline metrics

| Metric | Result |
|---|---|
| Execution accuracy (generation + follow-up) | 1/1 (100.0%) |
| Held-out execution accuracy (without the brief's few-shot examples) | 1/1 (100.0%) |
| Guardrail compliance | n/a |
| Optimize pass rate | n/a |
| Debug pass rate | n/a |
| Debug answers that explain the issue (problem + reason) | n/a |
| Intent accuracy | 1/1 (100.0%) |
| All cases | 1/1 (100.0%) |
| Average latency per LLM-backed turn | 1.9 s (p50 2.0 s, p90 2.0 s) |

Average time per LLM node (per turn that ran it, repairs included): detect_intent 0.7 s, generate_sql 0.6 s, explain_sql 0.6 s.

## By category

| Category | Cases | Passed | Accuracy | Avg latency per case |
|---|---:|---:|---:|---:|
| Follow-up (multi-turn) | 1 | 1 | 100.0% | 3.8 s |

## Failures

None.

## All cases

| Case | Category | Result | Status | Intent | Attempts | Latency | Detail |
|---|---|---|---|---|---:|---:|---|
| `dev_fu_group_by_month` | follow_up | PASS | success | modify_sql | 1 | 3.8 s | match (+1 extra column) |

## Scoring

- Generation and follow-up: the agent's SQL and the gold SQL are executed on the sample database and their result sets compared (values only: column names and order are ignored, extra columns are allowed, numbers are compared at 2 decimals; row order only matters for ordering / top-N questions). Follow-ups run every turn on one conversation thread and score the last turn.
- Debug: the corrected SQL must pass validation and return the gold result set.
- Optimize: the rewrite must pass validation, return the same result set as the original query, and report at least one change or suggestion.
- Guardrails: the expected status (refused / rejected / clarification), no SQL in the reply, and the exact brief message for out-of-scope requests.
- Latency is wall-clock time per agent turn, measured sequentially after the prompt-cache warm-up; turns blocked by the deterministic input guard never reach the LLM and are excluded from the LLM-backed average.
