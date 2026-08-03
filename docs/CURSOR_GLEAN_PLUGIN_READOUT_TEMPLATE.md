# Cursor Glean plugin evaluation — executive readout

> Status: template. Replace bracketed text only after a valid ten-prompt run and
> review of routing-confounded rows.

## Executive summary

We compared Cursor with the Glean vNext plugin **[active/inactive result]**
against the same Cursor/MCP environment with the plugin inactive. The comparison
used **[N] valid prompts** from **[workflow categories]**.

**Recommendation:** [recommend broader rollout / continue targeted testing / no
material benefit demonstrated / inconclusive].

## What was tested

- Cursor version: `[version]`
- Cursor agent version: `[version]`
- Glean plugin: `glean-vnext [version]`
- Plugin server: `plugin-glean-vnext-glean`
- Model: `[model]`
- Participant/sample: `[participant count and IDs]`
- Prompt count: `[N]`
- Shared MCP servers: `[list]`
- Judge host: `Claude Code`

## Quality impact

| Metric | Plugin active | Plugin inactive | Difference |
|---|---:|---:|---:|
| Accuracy | [ ] | [ ] | [ ] |
| Completeness | [ ] | [ ] | [ ] |
| Source/citation usefulness | [ ] | [ ] | [ ] |
| Workflow fit | [ ] | [ ] | [ ] |
| Blind overall wins | [ ] | [ ] | [ ] |

### Where the plugin helped

- [workflow/prompt and evidence]
- [workflow/prompt and evidence]

### Where it did not help

- [workflow/prompt and evidence]
- [workflow/prompt and evidence]

## Efficiency and token economics

| Metric | Plugin active | Plugin inactive | Difference |
|---|---:|---:|---:|
| Median/average latency | [ ] | [ ] | [ ] |
| Marginal tokens | [ ] | [ ] | [ ] |
| MCP/tool calls | [ ] | [ ] | [ ] |
| List-price-normalized cost | [ ] | [ ] | [ ] |

Cursor does not expose reliable per-run billed dollar cost. Treat normalized
cost as a rate-card comparison, not spend.

## Routing and validity

- Valid rows: `[ ]`
- Invalid rows: `[ ]`
- Mixed-routing rows: `[ ]`
- Control rows with plugin leakage: `[ ]`
- Treatment rows with plugin not observed: `[ ]`
- Rows excluded from claims: `[ ]`

Explain whether any `/glean_run` or equivalent request routed to ordinary Glean
MCP instead of the plugin. Do not attribute mixed-routing failures to answer
quality or plugin value.

## Limitations

- Three-prompt results are smoke-test results, not customer-facing evidence.
- Small samples produce wide confidence intervals.
- Cursor token usage is version-dependent.
- Cursor does not expose per-run billed dollar cost.
- OAuth expiration and browser reauthentication may affect runs.
- Plugin lifecycle may require manual deactivation/uninstallation in Cursor
  Desktop.
- Quality grading currently requires Claude Code for reliable schema-enforced
  JSON output. A Claude-free judging path is in progress; Cursor judging is a
  less reliable fallback.
- Product beta, bug, rollout, and GA statements must use approved current
  language.

## Decision and follow-up

- Decision: `[ ]`
- Scope of recommendation: `[ ]`
- Additional prompts or participants needed: `[ ]`
- Routing fixes needed before rollout: `[ ]`
- Product/version follow-up: `[ ]`
