# End-user MCP setup for the A/B eval

This runbook configures the four direct MCP servers in the current reference profile:

- `slack`
- `atlassian`
- `notion`
- `github`

Glean is configured separately in the local `mcp/glean.mcp.json` file. Run these commands in a normal terminal, not as prompts inside Claude. Use the exact server names above so they match `eval.config.json`.

After running `setup`, replace the placeholders in `mcp/glean.mcp.json` with the Glean MCP endpoint and authentication details supplied for your Glean instance. Keep that file local; it is ignored by Git and may contain authorization material.

## 0. Prerequisites

```bash
claude --version
node --version
npm --version
```

Claude Code 2.1.1 or newer is recommended because `claude mcp add-json` is supported there. Node.js 18+ is needed for `npx`-based servers and is useful for the Atlassian proxy path.

Start from the eval directory:

```bash
cd "/Users/matthewincledon/Documents/MCP Eval/July 30 - claude - 1"
```

MCP definitions created with `--scope user` are stored in the user's Claude configuration and are available across projects. Do not commit the resulting local MCP configuration or copy secrets into Git. The eval's strict-mode files under `mcp/` are ignored for this reason.

If a server name already exists, inspect it first:

```bash
claude mcp list
claude mcp get <name>
```

Only replace an existing server after confirming it is the intended definition.

## 1. Slack — official remote server

### Prerequisites

A Slack admin must approve an internal or directory-published Slack app for MCP access. Slack requires a fixed app/client ID; dynamic client registration is not supported.

Use the value labeled **Client ID** under the Slack app's **Basic Information → App Credentials** or **OAuth & Permissions** page. It is typically a numeric value with a dot, such as `1234567890.1234567890`. Do not use the Slack App ID (`A...`), Signing Secret, Verification Token, Bot Token, or Client Secret.

For a read-only eval, request only the user-token scopes needed for search and retrieval:

- `search:read.public`
- `search:read.private`
- `search:read.mpim`
- `search:read.im`
- `search:read.files`
- `files:read`
- `search:read.users`
- `channels:history`
- `groups:history`
- `mpim:history`
- `im:history`
- `channels:read`
- `groups:read`
- `users:read`
- `users:read.email` if user email lookup is required

Do not grant write scopes such as `chat:write`, `channels:write`, `groups:write`, `reactions:write`, or `canvases:write` for this eval.

### Add and authenticate

Replace the placeholder with the fixed Slack app/client ID supplied by the Slack admin:

```bash
export SLACK_CLIENT_ID="<SLACK_FIXED_CLIENT_ID>"

claude mcp add \
  --scope user \
  --transport http \
  --client-id "$SLACK_CLIENT_ID" \
  --callback-port 3118 \
  slack \
  https://mcp.slack.com/mcp

claude mcp login slack
unset SLACK_CLIENT_ID
```

Complete the browser OAuth flow. If Claude Code reports a callback-port conflict, choose another fixed port and use the same port consistently in the Slack app configuration and command.

Slack uses Streamable HTTP at `https://mcp.slack.com/mcp`; do not use the legacy SSE transport.

If Slack shows `Invalid client_id parameter`, remove the bad registration and retry with the actual Slack OAuth Client ID:

```bash
claude mcp remove slack

export SLACK_CLIENT_ID="<NUMERIC_SLACK_OAUTH_CLIENT_ID>"
claude mcp add \
  --scope user \
  --transport http \
  --client-id "$SLACK_CLIENT_ID" \
  --callback-port 3118 \
  slack \
  https://mcp.slack.com/mcp
claude mcp login slack
unset SLACK_CLIENT_ID
```

If the correct Client ID is still rejected, the Slack app is likely not an internal/directory-published app or has not been approved by the workspace administrator.

## 2. Atlassian — official Rovo remote server

### Prerequisites

The user needs access to the relevant Atlassian Cloud Jira/Confluence sites. Atlassian organization settings and product permissions still control what the MCP can read.

For the eval, grant read/search permissions where possible. The eval configuration must also allow only read/search tools and deny issue/page creation or update tools.

### Add and authenticate with OAuth

The current recommended Atlassian endpoint is `/authv2`:

```bash
claude mcp add \
  --scope user \
  --transport http \
  atlassian \
  https://mcp.atlassian.com/v1/mcp/authv2

claude mcp login atlassian
```

Complete the browser OAuth flow and select the Atlassian sites the eval should access.

The legacy endpoint `https://mcp.atlassian.com/v1/sse` may still work, but `/v1/mcp/authv2` is the preferred endpoint for new setups.

## 3. Notion — official remote server

Notion is a good replacement for Google Drive because it is vendor-hosted, supports direct OAuth, and does not require local packages, a GCP project, or custom credentials.

Add it from the eval directory:

```bash
claude mcp add \
  --scope user \
  --transport http \
  notion \
  https://mcp.notion.com/mcp
```

Start Claude Code:

```bash
claude
```

Inside Claude Code, run:

```text
/mcp
```

Select `notion` and complete the OAuth flow. Notion MCP can read and write content based on the user's Notion permissions. For this read-only eval, the eval configuration must allow only search/read tools and block create/update/archive tools.

Official setup reference: [Notion MCP](https://developers.notion.com/guides/mcp/get-started-with-mcp)

## 4. GitHub — official remote server, read-only endpoint

### Prerequisites

Use a fine-grained GitHub token with the minimum repository access needed for the prompts. Typical read-only permissions are:

- Repository metadata: read-only
- Contents: read-only
- Issues: read-only
- Pull requests: read-only
- Discussions: read-only if the prompt set needs them

The official remote server also supports OAuth, but PAT authentication is the simplest portable baseline for Claude Code automation. Do not place the token in Git or shell history.

### Add with a fine-grained PAT

If GitHub CLI is already authenticated, this avoids typing the token directly:

```bash
export GITHUB_PAT="$(gh auth token)"

claude mcp add \
  --scope user \
  --transport http \
  -H "Authorization: Bearer $GITHUB_PAT" \
  github \
  https://api.githubcopilot.com/mcp/readonly

unset GITHUB_PAT
```

If `gh auth token` is unavailable, read the token without putting it in command history:

```bash
read -s GITHUB_PAT
printf '\n'

claude mcp add \
  --scope user \
  --transport http \
  -H "Authorization: Bearer $GITHUB_PAT" \
  github \
  https://api.githubcopilot.com/mcp/readonly

unset GITHUB_PAT
```

The `/readonly` URL restricts the server to read tools. The Claude configuration still contains the authorization header locally, so keep the config private.

## 5. Recommended: generate the strict direct-arm config

The eval does not use Claude Desktop's Connector state directly. The runner uses its own ignored, strict MCP file so the direct arm cannot see `glean_default` or any unrelated ambient servers.

After adding and authenticating the direct servers with Claude Code, run this from the eval directory:

```bash
python3 scripts/glean_mcp_eval.py setup-direct --config eval.config.json
```

The command reads Claude Code's user MCP configuration from `~/.claude.json`, selects only the names in `arms.direct.expected_mcp_servers`, and writes:

```text
mcp/direct.mcp.json
```

It fails closed if any expected server is missing. Preview the selection without writing:

```bash
python3 scripts/glean_mcp_eval.py setup-direct \
  --config eval.config.json \
  --dry-run
```

You can override the selected server set explicitly:

```bash
python3 scripts/glean_mcp_eval.py setup-direct \
  --config eval.config.json \
  --servers slack,atlassian,notion,github
```

The generated file is ignored by Git, but it may contain local authorization headers or OAuth metadata. Never commit or share it.

## 6. Verify all four direct servers

```bash
claude mcp list

claude mcp get slack
claude mcp get atlassian
claude mcp get notion
claude mcp get github
```

Then start Claude Code in the eval folder:

```bash
cd "/Users/matthewincledon/Documents/MCP Eval/July 30 - claude - 1"
claude
```

Inside Claude Code, run:

```text
/mcp
```

Confirm that all four direct servers are connected and record the exact read/search tool names. Also confirm the configured Glean server is available. Do not invoke write tools during verification.

## 7. Verify the strict configuration

The A/B kit uses strict mode, so the run itself uses the generated file under `mcp/`, not whatever unrelated servers happen to be enabled globally.

Inspect the generated configuration with:

```bash
claude mcp list
python3 scripts/glean_mcp_eval.py doctor --config eval.config.json
```

For the strict config, preserve:

- Direct server names exactly: `slack`, `atlassian`, `notion`, `github`
- Glean server name exactly: `glean_default` in the Glean arm only
- The exact transport and endpoint/command
- Required non-secret OAuth metadata or headers
- No extra MCP servers

Then update `eval.config.json` with:

- Exact read/search tool names in `arms.direct.allowed_tools`
- All write/mutation tools in `arms.direct.disallowed_tools`
- The same four names in `arms.direct.expected_mcp_servers`
- `glean_default` in the Glean arm only

Run validation before the eval:

```bash
python3 scripts/glean_mcp_eval.py doctor --config eval.config.json
python3 scripts/glean_mcp_eval.py preflight --config eval.config.json --arm glean --live
python3 scripts/glean_mcp_eval.py preflight --config eval.config.json --arm direct --live
```

Do not run the evaluation if a server is missing, a placeholder remains, or a write tool is allowed.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `add-from-claude-desktop` says no MCP servers found | The newer Claude Desktop Connectors UI is not importable by that command. Configure/authenticate the servers with Claude Code, then run `setup-direct`. |
| `setup-direct` says a server is missing | Run `claude mcp list` and add/authenticate the named server. The command intentionally refuses to create a partial direct arm. |
| `setup-direct` sees `glean_default` or `google_drive` in `~/.claude.json` | This is expected; it copies only `arms.direct.expected_mcp_servers` and excludes unrelated ambient servers. |
| `server already exists` | Run `claude mcp get <name>` first; remove only the intended duplicate with `claude mcp remove <name>`. |
| OAuth browser does not open | Run `claude mcp login <name> --no-browser`, open the printed URL, and paste the redirect when prompted. |
| Slack OAuth fails | Confirm the Slack app is internal/directory-published, admin-approved, and has the fixed client ID and callback port configured. |
| Atlassian connects but returns no data | Check the user's Jira/Confluence site access and organization Rovo MCP permissions. |
| `brew: command not found` | Install `uv` with `curl -LsSf https://astral.sh/uv/install.sh | sh`, then run `source "$HOME/.local/bin/env"`. |
| `uvx: command not found` | Reload the shell environment with `source "$HOME/.local/bin/env"`, then verify `uvx --version`. |
| Notion OAuth fails | Run `/mcp` inside Claude Code, select `notion`, and complete the browser OAuth flow. Confirm the user has access to the intended Notion workspace and pages. |
| `SDK auth failed: Unable to connect` | The local server is not listening on port 8000, or the `WORKSPACE_MCP_PORT`/URL do not match. |
| GitHub returns forbidden | Check the fine-grained PAT repository selection and read-only Contents/Issues/Pull requests permissions. |
| Strict preflight sees no tools | Compare `mcp/direct.mcp.json` with `claude mcp get <name>` and verify the transport, endpoint, auth metadata, and server name exactly match. |
| A live run tries to write | Stop the run; remove the write tool from `allowed_tools`, add it to `disallowed_tools`, and rerun preflight. |
