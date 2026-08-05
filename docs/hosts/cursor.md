# Running the eval on Cursor

The Cursor adapter (`scripts/hosts/cursor.py`) runs each prompt headless via
`cursor-agent`, enforces per-arm MCP isolation, gates tools to read-only, and
parses `stream-json` for the answer, model, tool calls, and (version-dependent)
token usage. The eval methodology is identical to Claude Code — same crossover
design, golden prompts, blind judge, and aggregation — only *how a run is
launched and measured* differs. See [METHODOLOGY.md](../METHODOLOGY.md).

This guide is written for running **standalone from a plain terminal** (no IDE
needed). It uses the small **3-prompt baseline** whose Arm B (vendor-direct) is
**Atlassian + Slack**. A 4-connector / larger-prompt variant is in the
[appendix](#appendix-4-connector-variant).

Verified against `cursor-agent` `2026.07.23`.

## Prerequisites

- **Cursor CLI** (`cursor-agent`) on PATH — `cursor-agent --version`.
- **Signed in** — `cursor-agent status` should show `Logged in`. (Headless
  `cursor-agent` has its own auth state, separate from the Cursor desktop app.)
- **Your MCP servers configured in `~/.cursor/mcp.json`** with the identifiers
  the config expects: `glean_default`, `Atlassian-MCP-Server`, `slack`. The kit
  authenticates them for you (see below), but the entries must exist.
- **Python 3** (no packages required).
- **A judge host.** By default the blind judge runs on Claude Code (`claude` on
  PATH) for structured output. If your `claude` uses Vertex, export its env
  before `grade` (see step 4). No Claude Code? Set `"judge_host": "cursor"`.

## 1. Configure

```bash
cp config/eval.config.cursor.example.json   eval.config.json
cp prompts/golden_prompts.cursor.example.tsv golden_prompts.tsv
mkdir -p mcp
cp config/mcp.cursor.glean.example.json  mcp/glean.mcp.json
cp config/mcp.cursor.direct.example.json mcp/direct.mcp.json
```

Then edit `mcp/glean.mcp.json` to point at your Glean subdomain.

> **Server identifiers must match `~/.cursor/mcp.json`.** `cursor-agent` resolves
> a server by its canonical identifier there, and reports that identifier in its
> tool-call events. Notably Atlassian is usually `Atlassian-MCP-Server` (not
> `atlassian` as in the Claude examples). If the names don't match, the live
> preflight and validity gates won't see the server as "used".

## 2. How isolation and auth work (automatic)

`cursor-agent` **merges the global `~/.cursor/mcp.json` into every run**, so
`--workspace` alone does *not* isolate arms. The kit handles this for you:

- **Isolation** (`cursor_manage_global_mcp`, default on): before each arm the kit
  enables only that arm's servers in the global approved list and disables the
  rest, then **restores the prior state afterward** (even if the run errors).
- **Auth** (`cursor_ensure_auth`, default on): for each of the arm's servers that
  isn't already authenticated, the kit runs `cursor-agent mcp login <server>`.
  **A browser window may open for you to approve** — this is the only manual
  input a run needs. It prints `Authentication complete: <server>` per server.
  When global MCP management is enabled, prompts reuse the repository Cursor
  workspace so project-scoped OAuth/approval state is not lost on every new prompt.

## 3. Sanity check (no tokens), then preflight

```bash
python3 scripts/glean_mcp_eval.py doctor   --config eval.config.json
python3 scripts/glean_mcp_eval.py run      --config eval.config.json --arm glean --participant-id user01 --dry-run

python3 scripts/glean_mcp_eval.py preflight --config eval.config.json --arm glean  --live
python3 scripts/glean_mcp_eval.py preflight --config eval.config.json --arm direct --live
```

`--dry-run` prints the exact `cursor-agent` invocation without executing. Live
preflight runs one harmless probe per arm and must see every expected server
actually get called.

## 4. Run, grade, report, package

```bash
# caffeinate -i keeps macOS awake so a long run isn't killed (and its per-prompt
# timeout isn't paused) if the machine sleeps.
caffeinate -i python3 scripts/glean_mcp_eval.py run --config eval.config.json --arm glean  --participant-id user01
caffeinate -i python3 scripts/glean_mcp_eval.py run --config eval.config.json --arm direct --participant-id user01

# Judge on Claude Code. If your claude uses Vertex, export its env first, e.g.:
#   export CLAUDE_CODE_USE_VERTEX=1 CLOUD_ML_REGION=us-east5 ANTHROPIC_VERTEX_PROJECT_ID=<project>
python3 scripts/glean_mcp_eval.py grade   --config eval.config.json --participant-id user01
python3 scripts/glean_mcp_eval.py report  --config eval.config.json
python3 scripts/glean_mcp_eval.py package --config eval.config.json
```

Outputs land in `results/`: `aggregate_summary.md` (run-validity banner + MCP
usage by row + token/cost/latency + quality), `aggregate_rows.csv`, and
`eval_submission.zip`. Ignore headline numbers if the banner says
`Run validity: FAIL`.

## What Cursor can and cannot measure

| Metric | Cursor | Note |
|---|---|---|
| Answer + quality (judge) | ✅ | Judge runs on `judge_host`; Cursor answers graded blind |
| Latency | ✅ | `duration_ms` from the `stream-json` result event |
| Model pinning / reported | ✅ / ⚠️ | `--model` pins; served model from the `stream-json` system event |
| Per-arm MCP isolation | ✅ | Global enable/disable per arm (restored after), since `--workspace` alone doesn't isolate |
| Read-only tool gating | ✅ | `.cursor/cli.json` permissions (deny wins) |
| Tool-call attribution | ✅ | Server/tool parsed from the nested `mcpToolCall` event and normalized |
| Per-run **token usage** | ⚠️ | Parsed from `stream-json`; field is version-dependent — verify on your CLI version |
| Per-run **$ cost** | ❌ | Cursor exposes no per-run cost — use the list-price-normalized `computed_cost_usd`; `reported_cost` is null |
| Structured judge output | ❌ | No JSON-schema enforcement — run the judge on `judge_host` |

## Troubleshooting

- **A server shows `Connection failed` / `requires_authentication`** — re-run the
  arm; the kit re-logs in. Or manually: `cursor-agent mcp login <server>`.
- **`HTTP 503` from `cursor-agent`** — transient Cursor backend outage; retry.
- **A prompt runs away and hits the timeout** — tighten the prompt or lower
  `run_timeout_seconds`. A timed-out prompt is recorded as a failed row and the
  run continues to the next prompt.
- **Judge fails on auth** — your `claude` subprocess isn't authenticated; export
  the Vertex vars (above) or set `"judge_host": "cursor"`.

## Appendix: 4-connector variant

To make Arm B span **Atlassian + Slack + GitHub + Google Drive**, add those
servers to `mcp/direct.mcp.json` and to the direct arm's `expected_mcp_servers`
/ `require_live_tool_servers` / `allowed_tools`. Extra setup:

- **GitHub** injects a PAT into its header via `${env:GITHUB_PERSONAL_ACCESS_TOKEN}`.
  Export one before running (e.g. `export GITHUB_PERSONAL_ACCESS_TOKEN="$(gh auth token)"`).
- Broad, open-ended prompts make the agent fan out across all four connectors and
  can hit the per-prompt timeout — keep prompts bounded.
