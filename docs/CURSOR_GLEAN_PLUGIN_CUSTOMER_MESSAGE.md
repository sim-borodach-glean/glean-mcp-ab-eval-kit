# Customer communication: Cursor Glean plugin evaluation

Subject: Proposed Cursor Glean plugin evaluation

We will compare two Cursor configurations using the same model, prompts, MCP
servers, permissions, user context, and data sources:

- **Treatment:** the Glean vNext Cursor plugin is active.
- **Control:** the Glean vNext Cursor plugin is inactive.

The existing Glean MCP and required direct MCP connectors remain available in
both arms. This isolates the plugin’s effect on discovery, routing, retrieval
execution, answer quality, latency, and token usage rather than comparing
Glean against a vendor connector.

We will first run a three-prompt smoke test to validate authentication, plugin
routing, logging, read-only enforcement, and arm isolation. The customer-facing
comparison will use ten real engineering workflow prompts with identical
wording in both arms.

The evaluator records whether each request was handled by the plugin, ordinary
Glean MCP, another MCP, or a mixed route. Mixed routing is a known confounder:
a request such as `/glean_run` can intermittently route to MCP instead of the
plugin. Confounded rows will be reported separately and excluded from headline
claims. If the plugin is still observed in the control arm, the run is invalid
and will be rerun after manual deactivation.

The Cursor evaluator will not silently uninstall or toggle the plugin. When a
manual transition is required, it stops and tells the tester exactly what to do.
There is no unbounded retry loop. Treatment may load a verified local plugin
directory with Cursor’s `--plugin-dir` option; control still requires the
customer to deactivate or uninstall the plugin in Cursor Desktop when the
Cursor build retains global plugin state.

We will report quality and efficiency separately, including accuracy,
completeness, source/citation usefulness, freshness, workflow fit, latency,
marginal tokens, tool calls, routing outcome, and normalized cost where
available.

Important limitations:

- Quality grading currently requires Claude Code because the blind judge needs
  schema-enforced JSON output. Cursor cannot enforce that schema reliably. A
  Claude-free judging path is in progress; using Cursor as the judge is
  currently a less reliable prompt-and-parse fallback.
- Cursor exposes no per-run billed dollar cost, so cost is list-price-normalized
  rather than vendor-reported spend.
- Cursor token usage is version-dependent.
- OAuth MCP tokens can expire and require browser reauthentication.
- Long macOS runs should use `caffeinate -i`.
- Three prompts are a smoke test only; the final result should use ten prompts
  where possible.
- Beta, bug, rollout, and GA statements will use the currently approved product
  language rather than being inferred from this experiment.

The output will include a run-validity status, per-prompt scoring sheet,
aggregate CSV/Markdown report, routing notes, and an executive readout with any
invalid or inconclusive rows called out explicitly.
