# Cursor Glean plugin evaluation

This is a separate Cursor variant that compares the **Glean vNext Cursor plugin**
against the same Cursor/MCP environment with the plugin inactive. It does not
replace the existing Glean-MCP-vs-direct-MCP evaluation.

## Safety and lifecycle behavior

The evaluator is intentionally fail-closed:

- It never silently uninstalls or edits the Cursor plugin.
- It never retries enable/disable actions in a loop.
- Treatment can load an installed plugin directory with Cursor's `--plugin-dir`
  flag. The plugin directory is discovered from `~/.cursor/plugins/cache` or can
  be pinned with `CURSOR_GLEAN_PLUGIN_DIR`.
- Control prints a manual instruction to deactivate or uninstall the plugin and
  accepts one confirmation (`PLUGIN_OFF`). Any other response stops the run.
- If the plugin is observed during a control preflight, that arm fails validity
  and the report must not be used for claims.
- If the plugin directory is missing for treatment, the command stops and tells
  the tester to install the plugin or set `CURSOR_GLEAN_PLUGIN_DIR`.

The intended order is treatment first, then control:

1. Plugin active / installed.
2. Run treatment.
3. Manually deactivate or uninstall the plugin when prompted.
4. Run control.

## Prerequisites

- Cursor CLI (`cursor-agent`) on PATH.
- Cursor CLI signed in.
- The Glean vNext plugin installed, or a local plugin directory supplied through
  `CURSOR_GLEAN_PLUGIN_DIR`.
- Python 3.
- The same customer MCP servers configured and authenticated for both arms.
- Claude Code on PATH for the blind judge, unless the less reliable Cursor judge
  fallback is explicitly selected.

### Plugin identity used by this variant

The observed official plugin is:

- Plugin ID: `glean`
- Display name: `Glean`
- Version: `2.1.0`
- MCP server: none — the plugin supplies skills, agents, commands, and rules

The separate `glean-vnext` plugin is a different experimental plugin and must
not be used for this variant. Verify the customer’s installed official version
before a customer-facing run; update the config if it changes.

## Setup

From the cloned repository, run one bootstrap command:

```bash
python3 scripts/glean_mcp_eval.py setup-cursor-plugin \
  --config eval.config.json
```

By default it:

- Creates the ignored local `eval.config.json` from the plugin example.
- Copies the ten-prompt starter pack to the configured prompt path.
- Reads the customer’s exact MCP server entries from `~/.cursor/mcp.json`.
- Writes one local shared MCP file at `mcp/plugin.shared.mcp.json` for both arms.
- Copies no unrelated ambient servers.
- Reports only server names and credential-field locations, never credential values.

If the customer’s Cursor MCP file is elsewhere, use:

```bash
python3 scripts/glean_mcp_eval.py setup-cursor-plugin \
  --config eval.config.json \
  --source /path/to/customer/mcp.json
```

If only a subset of the configured servers should be part of the evaluation,
pass `--servers server_a,server_b`. The command stops with a clear list if a
configured server is missing rather than writing a partially valid experiment.

The customer still needs to review the prompt pack and replace the starter
prompts with real customer engineering workflows where appropriate. The
config intentionally uses the same shared MCP file for both arms; the plugin is
the experimental variable, not the MCP server set.

### Source-specific routing guidance

The plugin example also enables `prompt_routing_instruction`. The runner
prepends that instruction to both arms and fills `{expected_evidence}` from the
prompt TSV metadata. For example, a prompt whose metadata names Jira,
Confluence, Slack, and GitHub tells the agent to use the corresponding
read-only source-specific MCPs—Atlassian for Jira/Confluence, Slack for Slack,
and GitHub for pull requests or code—rather than relying only on generic Glean
retrieval.

This is an explicit preference, not a guarantee that a tool will be called.
Always inspect `mcp_servers_used` and `routing_outcome` in each `run.json`.
The instruction is identical in treatment and control so it does not change
the paired question. Prompts intended to test explicit plugin commands such as
`/glean_run` should remain a separate routing/UX track rather than being pooled
with this quality comparison.

### Deterministic source prefetch

The plugin example enables a Cursor-mediated prefetch plan for the scored
prompts. Before the answer session, the runner starts a separate read-only
Cursor session that must call the exact configured MCP tools for that
prompt—currently ordinary Glean search plus Jira/Confluence through Atlassian
and Slack through Slack. The runner verifies the observed tool names, saves the
prefetch transcript under
`results/<participant>/<arm>/<prompt>/prefetch/`, and injects the returned
evidence digest into the answer prompt. If a required tool is missing, the
prompt stops rather than being scored. The example uses `strict: false`
because Cursor emits automatic tool-discovery and helper calls; those
unexpected calls are recorded as warnings in `verification.json`. Set
`strict: true` only when the Cursor build's discovery behavior is part of the
plan.

This reuses Cursor's existing OAuth session. It is deterministic at the
verified-tool level, but Cursor still supplies valid query arguments and
returns the evidence; it is not a direct MCP HTTP client. Ordinary Glean search
is now part of every shared plan so both arms have the same Glean baseline. The
official plugin supplies skills, agents, commands, and rules rather than a
separate plugin MCP server, so treatment must not require
`plugin-glean-vnext-glean` setup/search provider events; control has the plugin
removed.
The injected `answer_instruction` keeps synthesis bounded by the verified
prefetch evidence instead of starting a second broad retrieval loop. The
example sets `answer_mcp_tools: none`; the runner gives the synthesis session an
empty MCP workspace and temporarily suspends global MCP state, so all retrieval
for the comparison happens in the verified prefetch phase. The example uses short
`prefetch.query_by_prompt` terms so
one broad question cannot consume the entire prefetch timeout before the other
required tools run. Add a plan under `prefetch.tool_plan_by_prompt` only for
tools present in the arm allowlist.
Existing successful rows are skipped, so use `--rerun-existing` after enabling
or changing a prefetch plan.

Optional explicit plugin path:

```bash
export CURSOR_GLEAN_PLUGIN_DIR="$HOME/.cursor/plugins/cache/cursor-public/glean/<version-hash>"
```

If omitted, the evaluator discovers the newest matching `glean` plugin manifest
under `~/.cursor/plugins/cache`.

## Doctor and dry run

```bash
python3 scripts/glean_mcp_eval.py doctor --config eval.config.json
python3 scripts/glean_mcp_eval.py run --config eval.config.json \
  --arm treatment --participant-id user01 --dry-run
python3 scripts/glean_mcp_eval.py run --config eval.config.json \
  --arm control --participant-id user01 --dry-run
```

The treatment dry run should include `--plugin-dir`. The control dry run should
not include it.

## Explicit live preflight commands

`doctor` checks local files, the host executable, plugin installation metadata,
and static MCP configuration. It does **not** call the model or MCP tools.

The standalone live preflight commands are still available. Run them in arm
order; do not preflight both arms back-to-back if the plugin is active:

```bash
# 1. Verify the plugin-enabled treatment environment.
python3 scripts/glean_mcp_eval.py preflight \
  --config eval.config.json \
  --arm treatment \
  --live

# 2. After treatment is complete, manually deactivate/uninstall the plugin.
#    Then verify the control environment.
python3 scripts/glean_mcp_eval.py preflight \
  --config eval.config.json \
  --arm control \
  --live
```

A live preflight runs a harmless probe and records its result under
`results/_preflight/<arm>/latest.json`. It verifies expected MCP tool usage and,
for this variant, verifies that the plugin is observed in treatment and absent
in control. A failed preflight blocks the corresponding arm unless `--force` is
used.

You can then run the arms separately:

```bash
python3 scripts/glean_mcp_eval.py run \
  --config eval.config.json --arm treatment --participant-id user01

# Deactivate/uninstall the plugin manually, then:
python3 scripts/glean_mcp_eval.py run \
  --config eval.config.json --arm control --participant-id user01
```

## Three-prompt smoke test

Use three prompts only to validate lifecycle, routing, logging, and scoring:

```bash
caffeinate -i python3 scripts/glean_mcp_eval.py smoke-test \
  --config eval.config.json \
  --participant-id user01 \
  --prompt-count 3
```

The smoke test runs each arm sequentially. After treatment, it pauses before the
control arm and asks you to deactivate or uninstall the plugin. If you already
know the plugin cannot be toggled in your Cursor build, uninstall it manually
at that checkpoint and type `PLUGIN_OFF`.

Inspect:

```bash
cat results/_preflight/treatment/latest.json
cat results/_preflight/control/latest.json
cat results/user01/treatment/*/run.json
cat results/user01/control/*/run.json
```

Look especially at:

- `transcript.routing_outcome`
- `transcript.plugin_servers_used`
- `transcript.plugin_present_when_disabled`
- `transcript.plugin_required_but_unobserved`
- `validity_flags`

Routing outcomes include `plugin`, `mcp`, `mixed`, `other_mcp`, and `none`.
`mixed` means plugin and ordinary Glean MCP routing were both observed in one
run; treat it as a confounder, not as an answer-quality result.

## Full ten-prompt run

After the smoke test is clean, run the preferred customer evaluation:

```bash
caffeinate -i python3 scripts/glean_mcp_eval.py run-all \
  --config eval.config.json \
  --participant-id user01
```

This runs treatment, pauses for control plugin deactivation, then runs control,
grades the paired answers, writes the report, and packages the results.

Use identical prompt wording in both arms for the primary comparison. If you
want to test explicit `/glean_run` or plugin-specific instructions, run that as
a separate UX/routing track and do not pool it with the causal comparison.

## Read-only boundaries

The evaluator denies Cursor `Write` and `Shell` actions. The example also denies
known write-capable MCP tools. The plugin’s generic `run_tool` dispatcher is
disallowed by default because it may reach write actions; do not enable it for a
scored read-only run unless you have a separately verified read-only policy for
the customer’s plugin build.

## Troubleshooting

### Treatment says the plugin directory is missing

Install the Glean vNext plugin in Cursor, or set:

```bash
export CURSOR_GLEAN_PLUGIN_DIR=/absolute/path/to/the/plugin-directory
```

The directory must contain `.cursor-plugin/plugin.json`.

### Control preflight says the plugin is present

Stop. Do not use the results. Deactivate or uninstall the plugin in Cursor
Desktop, then rerun the control preflight. The evaluator does not attempt to
force this transition because Cursor builds may retain plugin state outside the
CLI configuration.

### `plugin_server_not_observed`

The treatment ran, but the expected official plugin behavior was not
observed. Confirm that the configured `glean` plugin directory and version are
present and that the live transcript shows the expected Glean/Atlassian routing.
The official plugin does not emit a `plugin-glean-vnext-glean` MCP server event;
do not use that event as its treatment gate.

### OAuth or connector authentication failures

Run `cursor-agent mcp list` and authenticate the affected server. OAuth tokens
can expire and the evaluator may open a browser for approval. Re-run the
affected arm’s live preflight before running prompts.

### Judge failure

Quality grading currently requires Claude Code for schema-enforced JSON output.
Set the appropriate Claude/Vertex authentication variables or, less reliably,
set `judge_host` to `cursor`. A Claude-free structured-judge path is in progress.

## Result interpretation

Use `results/aggregate_summary.md` only when it says `Run validity: PASS`.
Lead with marginal tokens, latency, routing validity, and blind-judge win rate.
Cursor exposes no per-run billed dollar cost, so cost is list-price-normalized.
Small prompt counts produce wide confidence intervals; three prompts are a
smoke test, not a customer-facing result.
