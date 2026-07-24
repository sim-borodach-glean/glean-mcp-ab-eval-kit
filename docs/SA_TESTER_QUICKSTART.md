# SA tester quickstart: Glean MCP vs direct MCP eval

Repo: https://github.com/matt-incledon-glean/glean-mcp-ab-eval-kit

Goal: run the same golden prompts in Claude Code twice — once with **Glean MCP** and once with **direct/vendor MCPs** — then share the generated result zip.

## 1. Clone the repo

```bash
git clone https://github.com/matt-incledon-glean/glean-mcp-ab-eval-kit.git
cd glean-mcp-ab-eval-kit
```

## 2. Choose MCP mode

Use **strict mode** unless you are only debugging locally.

| Mode | What controls MCPs? | Do I manually add/remove MCPs between arms? |
|---|---|---:|
| Strict | `mcp/glean.mcp.json` and `mcp/direct.mcp.json` | No |
| Ambient | current `claude mcp list` state | Yes |

Strict mode is cleaner because each arm uses only the MCP servers in its JSON file.

## 3. Create local editable files — strict mode

```bash
cp config/eval.config.strict.example.json eval.config.json
cp prompts/golden_prompts.example.tsv golden_prompts.tsv
mkdir -p mcp
cp config/mcp.glean.example.json  mcp/glean.mcp.json
cp config/mcp.direct.example.json mcp/direct.mcp.json
```

Edit:

```bash
open eval.config.json
open golden_prompts.tsv
open mcp/glean.mcp.json
open mcp/direct.mcp.json
```

In `eval.config.json`, keep:

```json
"mcp_mode": "strict"
```

In strict mode, `claude mcp list` is useful for auth/debugging, but the eval arm uses only the corresponding `mcp/*.json` file.

## 4. Configure MCP JSON files

Find your working server details:

```bash
claude mcp list
claude mcp get glean_default
claude mcp get atlassian
claude mcp get slack
```

Make sure:

- `mcp/glean.mcp.json` contains only Glean MCP.
- `mcp/direct.mcp.json` contains only direct/vendor MCPs, e.g. Atlassian + Slack.
- If `claude mcp get <server>` shows required headers, mirror them in the strict JSON file.

Example Glean endpoint shape:

```json
{
  "mcpServers": {
    "glean_default": {
      "type": "http",
      "url": "https://<your-glean-backend-host>/mcp/default"
    }
  }
}
```

If your working server from `claude mcp get glean_default` shows required headers, mirror those headers in this JSON file.

Example direct endpoint shape:

```json
{
  "mcpServers": {
    "atlassian": {
      "type": "http",
      "url": "https://mcp.atlassian.com/v1/mcp"
    },
    "slack": {
      "type": "http",
      "url": "https://mcp.slack.com/mcp",
      "oauth": {
        "clientId": "1601185624273.8899143856786",
        "callbackPort": 3118
      }
    }
  }
}
```

If auth is needed:

```bash
claude mcp login glean_default
claude mcp login atlassian
claude mcp login slack
```

## 5. Configure eval settings

In `eval.config.json`, update:

- `model`: same Claude model for both arms
- `arms.glean.expected_mcp_servers`: usually `["glean_default"]`
- `arms.glean.forbidden_mcp_servers`: direct/vendor MCPs, e.g. `atlassian`, `slack`
- `arms.direct.expected_mcp_servers`: direct/vendor MCPs, e.g. `["atlassian", "slack"]`
- `arms.direct.forbidden_mcp_servers`: Glean MCP names, e.g. `glean_default`, `glean`
- `allowed_tools`: exact read/search tool names, usually `mcp__<server>__<tool>`
- `disallowed_tools`: write/mutation tools

The tool also blocks local built-ins like `Bash`, `Read`, `Write`, `WebSearch`, and `WebFetch` by default.

To inspect exact MCP tool names, start Claude Code and use:

```text
/mcp
```

## 6. Configure prompts

Edit `golden_prompts.tsv`.

Keep exact tab-separated columns:

```text
ID	Dept	Prompt
```

Every row must have the same number of tab-separated columns as the header. The CLI now fails fast if the TSV is malformed.

## 7. Doctor + preflight

```bash
python3 scripts/glean_mcp_eval.py doctor --config eval.config.json
python3 scripts/glean_mcp_eval.py preflight --config eval.config.json --arm glean --live
python3 scripts/glean_mcp_eval.py preflight --config eval.config.json --arm direct --live
```

Do **not** run the eval until both preflights pass.

Preflight checks:

- expected MCP servers present
- forbidden MCP servers absent
- no placeholder values in strict MCP configs
- live run uses every expected server for that arm

## 8. Run both arms

Use the same participant ID for both arms.

```bash
python3 scripts/glean_mcp_eval.py run --config eval.config.json --arm glean --participant-id tester01
python3 scripts/glean_mcp_eval.py run --config eval.config.json --arm direct --participant-id tester01
```

`run` refuses to start if latest live preflight failed or is missing. Use `--force` only for debugging.

## 9. Grade, report, package

```bash
python3 scripts/glean_mcp_eval.py grade --config eval.config.json --participant-id tester01
python3 scripts/glean_mcp_eval.py report --config eval.config.json
python3 scripts/glean_mcp_eval.py package --config eval.config.json
```

Share this file with the eval owner through an approved secure channel:

```text
results/eval_submission.zip
```

## 10. Inspect local summary

```bash
open results/aggregate_summary.md
open results/aggregate_rows.csv
```

Look for:

- `Run validity: PASS`
- both preflights passed
- MCP usage by row shows the intended servers
- invalid rows = 0
- quality judging is marked as run, not `NOT RUN`

## Ambient mode fallback

Use this only for quick local debugging.

```bash
cp config/eval.config.ambient.example.json eval.config.json
```

Then manually isolate MCPs before each arm:

- Glean arm: only Glean MCP enabled
- Direct arm: only direct/vendor MCPs enabled

Confirm with:

```bash
claude mcp list
```

## Common fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| `missing_expected: ["slack"]` | Strict `mcp/direct.mcp.json` lacks Slack | Add Slack to `mcp/direct.mcp.json` |
| `claude mcp list` connected, but strict preflight has no tools | Strict JSON differs from user-level server config | Run `claude mcp get <server>` and mirror URL/type/headers |
| Placeholder config error | Copied example MCP JSON but did not customize | Replace placeholder URL/token/client values |
| Prompt file validation failed | Bad TSV tabs/columns | Keep `ID<TAB>Dept<TAB>Prompt` and one row per prompt |
| `run` refuses to start | Latest live preflight missing/failed | Fix setup and rerun preflight |
| Quality shows `NOT RUN` | `grade` not run | Run `grade`, then `report` |

Do not share `eval.config.json`, `golden_prompts.tsv`, `mcp/*.json`, or `results/eval_submission.zip` publicly; they may contain customer-specific data or secrets.
