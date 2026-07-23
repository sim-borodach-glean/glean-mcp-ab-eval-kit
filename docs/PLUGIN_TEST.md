# Cursor Glean-plugin A/B test

This variant measures the effect of the **Glean Cursor plugin**
(`plugin-glean-vnext-glean`) on a realistic **write + read** workflow, holding
everything else constant.

- **Arm 1 — baseline:** Atlassian-write MCP + Glean-read MCP (`glean_default`)
- **Arm 2 — plugin:** Arm 1 **+ the Glean plugin** (the `glean_run` skill and the
  `find_skills` / `run_tool` action gateway)

The plugin is the only variable. Both arms use Glean for knowledge retrieval and
Atlassian for a **reversible sandbox write**, and both are scored on the same
metrics: cost, latency, tokens, and (via the judge) grounding/quality.

> Files: config `config/eval.config.plugin.example.json`, prompts
> `prompts/golden_prompts.plugin.example.tsv`, gate `scripts/plugin_gate.py`,
> orchestrator `scripts/plugin_ab.py`.

---

## The gating finding: the plugin can't be toggled programmatically (headless Cursor)

The natural design would be to flip the plugin on/off between arms from the
command line. **That does not work for headless `cursor-agent`.** Verified live
on `cursor-agent 2026.07.20-8cc9c0b`, three mechanisms were tested:

| Mechanism | Result |
|---|---|
| Kit's per-run staged `mcp-disabled.json` | **Ineffective** — the plugin still loaded and `find_skills` ran in a run where it was listed disabled. (Also observed the bare `glean_default` leak past it.) |
| Documented `cursor-agent mcp disable <id>` | Reports success, and `mcp list` never even lists plugin servers; effect on a running session was **not immediate/reliable**, and it cannot be cleanly re-enabled (`mcp enable` errors: server "not found in configuration"). |
| Physically moving the plugin bundle out of `~/.cursor/plugins` | **Ineffective within a session** — a fresh run still saw `plugin-glean-vnext-glean` with all 10 tools. |

Why: `plugin-glean-vnext-glean` is a **locally-launched stdio server**
(`node start.mjs`) that authenticates itself from `~/.glean`, independent of the
workspace. So neither workspace isolation nor auth-staging gates it.

An important measurement subtlety this surfaced: `mcp_servers_used` records
servers the agent **actually called**, not what was **available**. Arms can look
cleanly isolated only because a steered prompt never reached for the other
server — not because it was truly disabled. Always confirm availability with an
explicit probe, not by absence of calls.

### Conclusion

A rigorous plugin on/off comparison must be run with a **real
uninstall/install** of the plugin (and a Cursor/CLI restart) between the two
batches — not a programmatic toggle. The kit makes that safe and defensible by
**never trusting the operator's word**: at each toggle point it runs a live
presence probe and refuses to run the arm until the plugin is verifiably in the
required state.

---

## How the kit enforces it

1. **Presence probe (`scripts/plugin_gate.py`).** A bare `cursor-agent` run asks
   the agent to enumerate its available MCP server ids. Presence of
   `plugin-glean-vnext-glean` in that list is the authoritative signal — it does
   **not** go through the kit's isolation staging (that environment enumerates
   inconsistently). `find_skills`/`run_tool` exist on the bare Glean server too,
   so only the **server id** disambiguates the plugin.
   - `verify --expected present|absent` → exit 0 if the observed state matches.
2. **Interactive gate.** Before each arm, the orchestrator pauses, tells the
   operator to install or uninstall the plugin + restart, then probes and loops
   until the required state is confirmed. `--assume-yes` skips the pause (for a
   pre-arranged environment) but **still verifies**.
3. **Per-run labeling + leak flag.** Every run records the Glean servers it
   actually used. The report flags `plugin_leak_in_baseline` if the plugin was
   called in Arm 1 — so contamination is caught even if a gate were bypassed.

---

## Runbook

```bash
# 0a. Authenticate the MCP servers FOR THE cursor-agent CLI (one time).
#     The desktop Cursor app's plugin OAuth does NOT carry into headless
#     cursor-agent — a bare `cursor-agent -p` returns NEEDS_AUTH for Atlassian
#     until you log in here. `atlassian` and `glean_default` must be in
#     ~/.cursor/mcp.json (see config/mcp.plugin_ab.example.json for the entries).
cursor-agent mcp list                 # both should appear
cursor-agent mcp login atlassian      # completes an interactive browser OAuth
cursor-agent mcp login glean_default  # if it shows requires_authentication
# Note: the Atlassian *plugin* server (plugin-atlassian-atlassian) CANNOT be
# CLI-logged-in, which is why the write path uses the bare `atlassian` remote.

# 0b. Configure the eval
cp config/eval.config.plugin.example.json eval.config.json
# Edit eval.config.json → set sandbox.jira_issue_key (a throwaway issue you own,
# in a project that does NOT post to a shared Slack channel on create/edit).
# Writes are allow-listed ONLY against the sandbox; no default means no accidents.

# 1. See what your Cursor install currently exposes and which arm you're ready for
python3 scripts/plugin_ab.py detect --config eval.config.json

# 2. Run both arms in one session (pauses to gate each on a live probe)
python3 scripts/plugin_ab.py run --config eval.config.json --participant-id user01
#   → PAUSE: uninstall the Glean plugin + restart Cursor  → verifies ABSENT → runs Arm 1
#   → PAUSE: install the Glean plugin   + restart Cursor  → verifies PRESENT → runs Arm 2

# 3. Report (efficiency + plugin-usage integrity)
python3 scripts/plugin_ab.py report --config eval.config.json
#   → results/plugin_ab_summary.md, results/plugin_ab_rows.json
```

Preview any command without spending tokens with `run ... --dry-run` (prints the
exact `cursor-agent` invocations; no gate, no execution).

### Grounding / quality
Efficiency (cost, latency, tokens) and plugin-usage integrity come from
`plugin_ab.py report`. Grounding/quality is graded by the blind judge in
`glean_mcp_eval.py` — see [METHODOLOGY.md](METHODOLOGY.md); point it at the arm
pair `arm1_baseline` / `arm2_plugin`.

---

## Write-then-revert safety protocol

This is the only write-capable path in the kit, so it is deliberately narrow:

- **Sandbox only.** Every write targets `sandbox.*` from config; the arm's
  `allowed_tools` permit only the reversible Atlassian write tools
  (`editJiraIssue`, `transitionJiraIssue`, `addCommentToJiraIssue`) and
  `createJiraIssue`/`createConfluencePage` are explicitly denied.
- **Reversible edits only.** The Atlassian MCP exposes no delete tool, so
  "revert" means **restore the original value**: capture a field (e.g. an
  issue Description or status), change it behind a unique `[mvl-eval …]` marker,
  verify, then write the original value back / transition back.
- **Self-checking.** The prompt wrapper requires the agent to report what it
  changed and prove it reverted. Review `answer.md` per run; a run that fails to
  revert should be excluded and the sandbox item manually reset.

> Alpha status: the gate, config, prompts, orchestrator, and report are wired and
> the presence gate is validated live. Live write-arm runs require you to
> designate a sandbox and opt in; they have not been executed in this kit's CI.
