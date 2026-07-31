# End-user quickstart

This is the short version for someone who wants Claude Code to guide the setup.

## Start here

Open this repository in Claude Code and send:

```text
I want to run the Glean MCP A/B evaluation in this folder.

Read README.md and docs/END_USER_QUICKSTART.md first. Act as a setup guide:
1. Check Python, Claude Code, and Claude Code login.
2. Ask me which direct MCPs I want to compare.
3. Configure and authenticate one MCP at a time, without asking me to paste tokens or client secrets into chat.
4. Run setup-direct, doctor, and live preflight for both arms.
5. Stop and explain any failure; do not run the evaluation until both preflights pass.
6. Once I approve, run both arms for participant ID mi01, then grade and report.

Keep the evaluation read-only and do not add ambient MCPs to either strict arm.
```

Claude Code should explain each command before running it. Browser-based OAuth is preferred. Never paste a PAT, OAuth client secret, refresh token, or signing key into the Claude conversation.

## What the setup agent should do

1. Confirm Claude Code login and Python.
2. Read the expected direct MCP names from `eval.config.json`.
3. Check `claude mcp list`.
4. Add/authenticate missing MCPs one at a time using the vendor instructions in [END_USER_MCP_SETUP.md](END_USER_MCP_SETUP.md).
5. Run:

```bash
python3 scripts/glean_mcp_eval.py setup-direct --config eval.config.json --dry-run
python3 scripts/glean_mcp_eval.py setup-direct --config eval.config.json
python3 scripts/glean_mcp_eval.py doctor --config eval.config.json
python3 scripts/glean_mcp_eval.py preflight --config eval.config.json --arm glean --live
python3 scripts/glean_mcp_eval.py preflight --config eval.config.json --arm direct --live
```

6. Stop if either preflight fails.
7. With approval, run the full evaluation:

```bash
python3 scripts/glean_mcp_eval.py run-all \
  --config eval.config.json \
  --participant-id mi01
```

This runs both arms, grades the paired answers, generates the report, and packages the results. Existing successful prompts are resumable; use `--rerun-existing` for a deliberate fresh run.

## Current reference direct set

The current tested direct set is:

```text
Slack
Atlassian
Notion
GitHub
```

Google Drive and Gmail are not included because their Google-hosted MCP endpoints require an OAuth client registration that is not available through the current Claude Desktop Connector setup or the evaluator’s Google Cloud permissions.

## If setup fails

- **MCP missing:** run `claude mcp list`; complete that vendor’s OAuth/API-key setup.
- **`setup-direct` missing server:** the command intentionally refuses to create a partial direct arm.
- **Preflight fails:** do not run the evaluation; ask Claude Code to explain the exact missing server, tool, or permission.
- **Google Drive/Gmail OAuth says dynamic client registration is unsupported:** stop; do not copy tokens from Claude Desktop. Use another direct MCP or obtain an approved Google OAuth client.
- **A PAT or secret is needed:** paste it into a terminal prompt, not into Claude Code chat.

## Best operating model by audience

- **Business power user:** use the guided Claude Code prompt above.
- **Data analyst:** use the CLI quickstart in the repository README and inspect `aggregate_summary.md`.
- **Executive/customer sponsor:** have a facilitator run setup and share the final workbook/readout; debugging OAuth should not be part of the executive workflow.
