# Cursor Glean plugin experiment brief

## Objective

Measure whether adding the Glean vNext Cursor plugin improves real engineering
workflows in Cursor when the underlying MCP environment remains constant.

The primary variable is **plugin presence**, not Atlassian-vs-Glean access. This
experiment therefore does not replace the existing Glean-MCP-vs-direct-MCP
comparison.

## Arms

| Arm | Plugin state | MCP environment |
|---|---|---|
| Treatment | Glean vNext Cursor plugin active | Shared Glean MCP plus the same direct MCP servers |
| Control | Glean vNext Cursor plugin inactive | The exact same shared Glean MCP plus direct MCP servers |

The evaluator runs treatment first, then requires the tester to deactivate or
uninstall the plugin before control. It never silently changes plugin state.

## Variables held constant

- Exact prompt wording and prompt order
- Model and model settings
- Cursor CLI version, where possible
- MCP server versions and canonical identifiers
- MCP server availability and permissions
- User identity/participant ID structure
- Data sources and freshness window
- Host, network, timeout, and read-only settings
- Direct MCP configuration, including Jira/Atlassian, Confluence, GitHub, and
  other required customer connectors
- Participant instructions and grading process

The only intended experimental difference is the plugin-specific capability.

## Prompt selection

Use five to ten prompts based on real customer workflows. Ten is preferred for
the final comparison. Every prompt should:

- Require current internal information.
- Require retrieval from one or more expected evidence sources.
- Be meaningful to an engineer, operator, or support workflow.
- Have stable permissions in both arms.
- Include expected sources, why the workflow matters, and expected answer traits.

Do not use questions answerable from general model knowledge alone.

The primary track uses identical wording in both arms. A separate UX/routing
track may use explicit `/glean_run` or plugin instructions, but those results
must not be pooled with the primary plugin-presence comparison.

## Hypothesis

The plugin may improve:

- Discovery of Glean-specific capabilities.
- Routing to the appropriate Glean skill or plugin MCP server.
- Search-then-read execution.
- Retrieval completeness and source coverage.
- Prompting efficiency and user experience.
- Latency, token usage, redundant retrieval, and rework.

The experiment must not assume that the plugin changes Glean backend ranking or
underlying retrieval quality. A benefit may primarily come from better routing
and interaction.

## Measurements

### Answer quality

Per prompt, blind grading should consider:

- Accuracy
- Completeness
- Source coverage
- Citation usefulness and correctness
- Faithfulness to cited material
- Freshness
- Instruction following
- Workflow fit
- Whether the system used an appropriate source instead of guessing

### Efficiency and token economics

Record where Cursor exposes the value:

- End-to-end latency
- Input and output tokens
- Marginal tokens
- List-price-normalized cost
- Tool-call count
- MCP-call count
- Retrieval/routing outcome
- Redundant retrieval or rework indicators
- Plugin/MCP server and version metadata

Cursor does not expose reliable per-run billed dollar cost, and token fields are
Cursor-version-dependent. Unavailable metrics remain unavailable instead of
being fabricated.

## Routing validity

The plugin and Glean MCP can coexist. A request such as `/glean_run` may
intermittently route to the Glean MCP server instead of the plugin/skill.

The evaluator records:

- Plugin server calls
- Ordinary Glean MCP calls
- Other MCP calls
- Routing outcome (`plugin`, `mcp`, `mixed`, `other_mcp`, `none`)
- Whether the plugin appeared in the control arm
- Whether an expected plugin server was absent in treatment

Mixed-routing rows are confounded and should be reported separately, not
attributed to plugin quality. Any control row with the plugin present invalidates
the clean comparison until rerun.

## Success criteria

A successful evaluation should demonstrate:

1. Both arms use equivalent MCP servers and permissions.
2. Treatment plugin state is verified and plugin routing is observable.
3. Control plugin state is verified absent.
4. No write actions occur.
5. Prompts complete with valid transcripts and comparable model settings.
6. Routing-confounded rows are excluded from headline metrics.
7. Results report quality and efficiency separately.
8. The ten-prompt final run has enough valid rows for a useful directional result.

Do not define success as “the plugin wins every prompt.” A defensible result may
show no material improvement, improvement only for certain workflows, or an
inconclusive outcome due to routing or sample-size limitations.

## Known limitations and gotchas

- Quality grading currently requires Claude Code because the judge needs
  schema-enforced JSON. Cursor cannot enforce that schema reliably. A Claude-free
  path is in progress; Cursor judging is a less reliable fallback.
- Cursor exposes no per-run billed dollar cost.
- Cursor token reporting depends on the installed `cursor-agent` version.
- OAuth MCP tokens expire and may require browser approval.
- GitHub may require `GITHUB_PERSONAL_ACCESS_TOKEN` or the customer’s configured
  authentication mechanism.
- The evaluator temporarily replaces `~/.cursor/mcp.json` for arm isolation and
  restores it afterward.
- Plugin install/uninstall state may live in Cursor Desktop rather than the CLI;
  the evaluator stops and asks the tester to act manually.
- Long runs should use `caffeinate -i` on macOS.
- Small prompt counts produce wide confidence intervals.
- Beta, bug, rollout, and GA language must be verified against the current
  approved product status before external distribution.
