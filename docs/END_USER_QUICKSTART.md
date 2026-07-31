# End-user quickstart

This is the short version for someone who wants Claude Code to guide the setup.

## Start here

Open this repository in Claude Code and send the following message. You do not need to run the evaluation commands yourself.

```text
I want to run the Glean MCP A/B evaluation in this folder.

Read README.md and docs/END_USER_QUICKSTART.md first. Act as my setup guide and run the local setup commands on my behalf.

Before doing any setup, ask me:
- Which direct MCP profile I want: current-reference (Slack + Atlassian + Notion + GitHub), minimal (Slack + Atlassian), or a custom profile if available.
- Which prompt pack I want: the 16-prompt reference suite, the smaller example suite, or a custom TSV path.

Show me the selected direct MCP profile and prompt pack, explain what will run, and wait for my explicit confirmation. Do not run setup, authentication checks, preflight, smoke tests, or evaluation prompts before I confirm those choices.

After confirmation, run the local setup commands on my behalf. I will complete browser-based OAuth or other required authentication steps, provide approved Glean MCP connection details when needed, and approve the smoke test and full evaluation. Stop and explain any failure instead of asking me to run commands manually. Do not ask me to paste tokens, OAuth secrets, refresh tokens, or PATs into this chat.

Before the full evaluation, verify both arms with doctor and live preflight, then run the three-prompt smoke test. Only run the selected prompt pack after I approve the smoke-test result. Keep the evaluation strictly read-only, do not add ambient MCPs to either arm, keep all comprehensive artifacts local, and ask before sharing any result files or customer-facing excerpts.
```

## What you do

- Open the repository in Claude Code and paste the setup message above.
- Choose the direct MCP profile and prompt pack when Claude asks, then confirm the displayed selection.
- Complete browser-based OAuth or administrator approval when Claude Code pauses for it.
- Provide the approved Glean MCP endpoint and authentication details if the local Glean configuration still has placeholders. Do not paste secrets into the Claude conversation.
- Review the smoke-test result and explicitly approve the full evaluation if it passes.
- Review the final report and decide which excerpts, evidence, or summary files—if any—should be shared.

For vendor-specific authentication instructions, see [END_USER_MCP_SETUP.md](END_USER_MCP_SETUP.md).

## What Claude Code handles in the background

Claude Code first presents the available direct MCP profiles and prompt packs, records your choices, and waits for confirmation. It then checks the local prerequisites and login, creates the local evaluation config, copies the selected prompt pack into the local working file, and applies the selected direct-server profile.

It then materializes a strict direct-arm MCP config containing only Slack, Atlassian, Notion, and GitHub. It runs static checks and live read-only preflights for both the Glean and direct arms. If a required server, tool, or permission is missing, it stops before running evaluation prompts.

After authentication and preflight pass, it runs the smoke test. After your approval of the smoke-test result, it runs both arms across the selected prompt pack, grades the paired answers, generates the report, and creates the optional checksum package. Successful prompt rows can be resumed; a deliberate fresh rerun uses `--rerun-existing`.

The complete local record remains available for auditability, including per-prompt answers, transcripts, command metadata, usage, grades, reports, and package files. The facilitator chooses what to share externally.

## Commands Claude runs (reference only)

These commands show the default current-reference/16-prompt path so an operator can troubleshoot or reproduce the workflow. Claude substitutes the confirmed profile and prompt pack when you choose different options. A normal end user should not need to run them manually.

```bash
python3 scripts/glean_mcp_eval.py setup \
  --config eval.config.json \
  --profile current-reference

python3 scripts/glean_mcp_eval.py setup-direct \
  --config eval.config.json \
  --dry-run

python3 scripts/glean_mcp_eval.py setup-direct \
  --config eval.config.json

python3 scripts/glean_mcp_eval.py doctor \
  --config eval.config.json

python3 scripts/glean_mcp_eval.py preflight \
  --config eval.config.json \
  --arm glean \
  --live

python3 scripts/glean_mcp_eval.py preflight \
  --config eval.config.json \
  --arm direct \
  --live

python3 scripts/glean_mcp_eval.py smoke-test \
  --config eval.config.json \
  --participant-id smoke01

python3 scripts/glean_mcp_eval.py run-all \
  --config eval.config.json \
  --participant-id mi01
```

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

- **MCP missing:** Claude Code should explain which server is missing and pause for authentication or administrator approval.
- **`setup-direct` missing server:** the command intentionally refuses to create a partial direct arm.
- **Preflight fails:** do not run the evaluation; ask Claude Code to explain the exact missing server, tool, or permission.
- **Glean config still has placeholders:** provide the approved endpoint and authentication details through the local configuration process described in [END_USER_MCP_SETUP.md](END_USER_MCP_SETUP.md).
- **Google Drive/Gmail OAuth says dynamic client registration is unsupported:** stop; do not copy tokens from Claude Desktop. Use another direct MCP or obtain an approved Google OAuth client.
- **A PAT or secret is needed:** enter it through the vendor or terminal authentication flow, never in Claude Code chat.

## Best operating model by audience

- **Business power user:** use the guided Claude Code prompt above.
- **Data analyst:** use the CLI quickstart in the repository README and inspect `aggregate_summary.md`.
- **Executive/customer sponsor:** have a facilitator run setup and share the final workbook/readout; debugging OAuth should not be part of the executive workflow.
