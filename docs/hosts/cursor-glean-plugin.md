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

The observed local plugin is:

- Plugin ID: `glean-vnext`
- Plugin server: `plugin-glean-vnext-glean`
- Skill: `glean_run`

Verify the customer’s installed version before a customer-facing run. The
example currently records `0.2.42`; update the config if the tested version
changes.

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

Optional explicit plugin path:

```bash
export CURSOR_GLEAN_PLUGIN_DIR="$HOME/.cursor/plugins/cache/gleanwork-glean-plugins-vnext/glean-vnext/<version-hash>"
```

If omitted, the evaluator discovers the newest matching `glean-vnext` plugin
manifest under `~/.cursor/plugins/cache`.

Optional explicit plugin path:

```bash
export CURSOR_GLEAN_PLUGIN_DIR="$HOME/.cursor/plugins/cache/gleanwork-glean-plugins-vnext/glean-vnext/<version-hash>"
```

If omitted, the evaluator discovers the newest matching `glean-vnext` plugin
manifest under `~/.cursor/plugins/cache`.

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

The treatment ran, but the expected plugin MCP server was not observed. Check
that the prompt actually exercises plugin discovery, that the plugin version is
correct, and that the `plugin-glean-vnext-glean` server is present in the live
transcript. Do not classify this as a plugin-quality result until routing is
understood.

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
