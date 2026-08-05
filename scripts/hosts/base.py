"""Host adapter contract.

A host adapter encapsulates the two things that differ between agent hosts
(Claude Code, Cursor, ...):

  1. how to build the argv for one headless single-prompt run, and
  2. how to harvest normalized per-run metrics from that run.

Everything else — config, prompts, the blind judge, aggregation, reporting,
packaging — is host-agnostic and lives in `glean_mcp_eval.py`.

`harvest()` must return a dict with this normalized shape so the core can write a
uniform `run.json` regardless of host:

    {
      "ok": bool,                      # host-reported success (beyond returncode == 0)
      "session_id": str | None,
      "transcript": dict,              # must contain: usage, models, tool_calls,
                                       #   mcp_servers_used, retrieval_attempted, found
      "usage": {input_tokens, output_tokens,
                cache_creation_input_tokens, cache_read_input_tokens},
      "reported_cost_usd": float | None,   # None if the host does not expose cost
      "duration_ms": int | None,
      "num_turns": int | None,
      "answer_text": str,
      "raw_output_path": str | None,
      "output_type": str | None,
      "output_subtype": str | None,
    }
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REGISTRY: Dict[str, "HostAdapter"] = {}


class HostSetupError(Exception):
    """A fail-closed host lifecycle or state-verification error."""



def register(adapter: "HostAdapter") -> None:
    REGISTRY[adapter.name] = adapter


def get_adapter(name: Optional[str]) -> "HostAdapter":
    key = name or "claude-code"
    if key not in REGISTRY:
        raise KeyError(f"Unknown host {key!r}; registered: {', '.join(sorted(REGISTRY)) or '(none)'}")
    return REGISTRY[key]


class HostAdapter:
    name: str = "base"

    # Honest capability flags, surfaced by `doctor` and the README matrix.
    caps: Dict[str, bool] = {
        "per_arm_isolation": False,
        "readonly_gating": False,
        "per_run_token_usage": False,
        "reported_cost": False,
        "structured_output": False,
    }

    def executable_present(self) -> bool:
        """Whether this host's CLI is available on PATH."""
        raise NotImplementedError

    def build_command(
        self,
        root: Path,
        cfg: Dict[str, Any],
        arm_cfg: Dict[str, Any],
        prompt: str,
        out_dir: Path,
        *,
        model_key: str = "model",
        max_turns: Optional[int] = None,
        json_schema: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[str], Dict[str, Any]]:
        """Return (argv, ctx). ctx carries host state for prepare()/harvest()
        (e.g. a staged workspace path under `cwd`)."""
        raise NotImplementedError

    def prepare(self, ctx: Dict[str, Any]) -> None:
        """Optional: stage per-run files before execution (e.g. Cursor writes a
        per-arm `.cursor/mcp.json` + `cli.json`). Default: no-op."""
        return None

    def suspend_mcp(self) -> None:
        """Optional: temporarily suspend host-global MCP state for one run."""
        return None

    def restore_mcp(self) -> None:
        """Optional: restore MCP state suspended for one run."""
        return None

    def setup_arm(self, root: Path, cfg: Dict[str, Any], arm_cfg: Dict[str, Any], arm_name: str) -> None:
        """Optional: prepare host-global state before an arm's prompts run
        (e.g. Cursor isolates by enabling only this arm's MCP servers and
        disabling the rest, then ensures they are authenticated). Called once
        per arm by `run`/`preflight`, paired with teardown_arm(). Default: no-op."""
        return None

    def teardown_arm(self, root: Path, cfg: Dict[str, Any], arm_cfg: Dict[str, Any], arm_name: str) -> None:
        """Optional: restore host-global state changed by setup_arm(). Runs in a
        finally block so it executes even if the arm errors. Default: no-op."""
        return None

    def harvest(
        self,
        proc: Dict[str, Any],
        root: Path,
        out_dir: Path,
        cfg: Dict[str, Any],
        ctx: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Parse the finished subprocess into the normalized record (see module docstring)."""
        raise NotImplementedError

    def doctor(self, root: Path) -> Dict[str, Any]:
        """Optional: host-specific diagnostics for the `doctor` command."""
        return {}
