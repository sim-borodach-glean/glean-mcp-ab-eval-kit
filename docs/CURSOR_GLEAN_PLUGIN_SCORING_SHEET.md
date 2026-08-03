# Cursor Glean plugin scoring sheet

The evaluator generates `results/aggregate_rows.csv`. This sheet defines the
per-prompt fields and the review rules for the customer-facing comparison.

## Per-prompt review table

| Field | Treatment: plugin active | Control: plugin inactive | Notes |
|---|---|---|---|
| Prompt ID / workflow | Same | Same | Must match exactly |
| Answer success | `run.json.success` | `run.json.success` | Failed runs are invalid |
| Accuracy | 1–5 blind score | 1–5 blind score | Correctness against cited evidence |
| Completeness | 1–5 blind score | 1–5 blind score | Covers the requested workflow |
| Source coverage | Reviewer/judge note | Reviewer/judge note | Expected evidence sources reached |
| Citation usefulness | Reviewer/judge note | Reviewer/judge note | Correct and actionable citations |
| Freshness | Reviewer/judge note | Reviewer/judge note | Current information, where required |
| Instruction following | Reviewer/judge note | Reviewer/judge note | Read-only, requested format, scope |
| Workflow fit | Reviewer/judge note | Reviewer/judge note | Useful to the actual engineer/operator |
| Latency | `treatment_latency_ms` | `control_latency_ms` | Cursor wall-clock duration |
| Input tokens | `treatment_input_tokens` | `control_input_tokens` | Cursor stream-json, if available |
| Output tokens | `treatment_output_tokens` | `control_output_tokens` | Cursor stream-json, if available |
| Tool calls | `treatment_tool_call_count` | `control_tool_call_count` | Includes MCP and built-ins |
| MCP calls | `treatment_mcp_tool_call_count` | `control_mcp_tool_call_count` | Retrieval/tool activity |
| Routing outcome | `treatment_routing_outcome` | `control_routing_outcome` | `plugin`, `mcp`, `mixed`, `other_mcp`, `none` |
| Plugin servers used | `treatment_plugin_servers_used` | `control_plugin_servers_used` | Control should be empty |
| Overall winner | Blind grade | Blind grade | Treatment, control, or tie |
| Validity flags | Shared row | Shared row | Exclude flagged rows from headline claims |

## Quality rubric

Use the same rubric for both answers:

- **1 — poor:** materially wrong, unsupported, incomplete, or unusable.
- **2 — weak:** some useful information, but major omissions or evidence issues.
- **3 — adequate:** mostly correct and useful, with meaningful limitations.
- **4 — strong:** accurate, complete, well-supported, and useful for the workflow.
- **5 — excellent:** comprehensive, current, source-faithful, precise, and directly
  actionable.

A shorter answer does not win merely for being shorter. Efficiency is a
secondary dimension after quality.

## Validity rules

Exclude a row from headline metrics if it has any of the following:

- Arm run failure
- No MCP retrieval in an arm where retrieval was required
- Model mismatch
- `routing_confounded`
- Plugin present during the control arm
- Required plugin server absent during a plugin-required preflight
- Any write or shell action

Keep invalid rows in the CSV and report them as diagnostic evidence. Do not
silently delete them.

## Aggregation guidance

Report separately:

1. Quality: accuracy/completeness/source/citation/workflow scores and blind wins.
2. Efficiency: latency, marginal tokens, tool calls, MCP calls, and normalized cost.
3. Routing: plugin usage, MCP fallback, mixed-routing rows, and control leakage.

The three-prompt smoke test is a harness check only. The preferred customer
result uses ten valid prompts. If valid sample size is small, lead with
marginal tokens, latency, and directional blind-judge win rate, and show the
confidence interval.
