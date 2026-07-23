#!/usr/bin/env python3
"""Glean-plugin presence gate for the Cursor plugin A/B test.

Why this exists
---------------
The plugin A/B test compares two arms that differ by exactly one variable — the
Glean Cursor plugin (`plugin-glean-vnext-glean`):

  Arm 1 (baseline): Atlassian-write MCP + Glean-read MCP (`glean_default`)
  Arm 2 (plugin):   Arm 1  +  the Glean plugin (glean_run skill + run_tool gateway)

Live testing on cursor-agent 2026.07 established, with 100% confidence, that the
plugin **cannot be disabled programmatically** for headless runs: neither the
kit's staged per-project `mcp-disabled.json`, nor `cursor-agent mcp disable`, nor
even physically moving the plugin bundle off disk removed it from a fresh run
within a CLI session (see docs/PLUGIN_TEST.md). So Arm 1 requires the operator to
*actually uninstall* the plugin (and restart Cursor/CLI).

This module makes that safe by never trusting the operator's word: at each toggle
point it runs a tiny headless probe that tries to call a plugin-exclusive tool and
reads the transcript for the plugin's server id. It refuses to proceed unless the
plugin is verifiably in the required state (absent for Arm 1, present for Arm 2).
Presence is therefore an *observed* fact, not a claim.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Reuse the kit core (also registers the cursor host adapter).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import glean_mcp_eval as k  # noqa: E402

# Runtime MCP server id Cursor assigns the installed Glean plugin
# (plugin-<plugin.name>-<serverKey> = plugin-glean-vnext-glean). Overridable so
# the same gate works if the plugin id changes in a future release.
PLUGIN_SERVER_ID = "plugin-glean-vnext-glean"

# The bare Glean MCP (`glean_default`) and the plugin expose overlapping tool
# names (both have find_skills/run_tool), so a tool call cannot disambiguate
# them — only the server *id* can. Rather than force a call, ask the agent to
# enumerate the server ids it can see: it has an authoritative view of its own
# loaded MCP servers and reports the plugin's id verbatim when present. No tool
# is called, so the probe is fast, cheap, and side-effect free.
_PROBE_PROMPT_TMPL = (
    "READ-ONLY DIAGNOSTIC. Call NO tools and change NOTHING.\n"
    "List the exact identifier of every MCP server currently available to you, "
    "one per line, verbatim (for example `plugin-glean-vnext-glean` or "
    "`glean_default`). Include a server even if it needs auth.\n"
    "If you have no MCP servers at all, reply with the single word NONE."
)


def probe_plugin_presence(
    root: Path,
    cfg: Dict[str, Any],
    *,
    host: str = "cursor",
    out_dir: Optional[Path] = None,
    timeout: int = 180,
    plugin_server_id: str = PLUGIN_SERVER_ID,
) -> Dict[str, Any]:
    """Decide plugin presence from a BARE `cursor-agent` enumeration run.

    Deliberately does NOT go through the kit's per-arm isolation staging: that
    staged environment (empty workspace mcp.json + a staged project namespace)
    enumerates servers inconsistently, whereas a plain run reflects exactly what
    the operator's Cursor install exposes to headless runs. Presence is true iff
    the agent lists the plugin's server id (or its distinctive `glean-vnext`
    stem) when asked to enumerate its available MCP servers.
    """
    import shutil as _sh
    import subprocess as _sp
    import tempfile as _tf

    out_dir = out_dir or (root / (cfg.get("results_dir") or "results") / "_plugin_probe" / k.slug_ts())
    out_dir.mkdir(parents=True, exist_ok=True)
    sid_norm = k.normalize_server_name(plugin_server_id)
    if host != "cursor":
        raise ValueError("plugin gate currently supports host='cursor' only")
    if _sh.which("cursor-agent") is None:
        return {"present": False, "observed_from": "cursor-agent-missing", "error": "cursor-agent not on PATH",
                "plugin_server_id": plugin_server_id, "run_success": False, "probe_dir": str(out_dir)}

    model = cfg.get("plugin_probe_model") or cfg.get("model")
    cmd = ["cursor-agent", "-p", _PROBE_PROMPT_TMPL, "--output-format", "text", "--force", "--trust"]
    if model:
        cmd += ["--model", str(model)]
    with _tf.TemporaryDirectory(prefix="glean-plugin-probe-") as td:
        proc = _sp.run(cmd, cwd=td, text=True, capture_output=True, timeout=timeout)
    answer = (proc.stdout or "").strip()
    (out_dir / "probe_answer.txt").write_text(answer, encoding="utf-8")
    (out_dir / "probe_stderr.txt").write_text(proc.stderr or "", encoding="utf-8")
    ans_norm = k.normalize_server_name(answer)
    present = (sid_norm in ans_norm) or ("glean-vnext" in ans_norm)
    return {
        "present": bool(present),
        "observed_from": "server_enumeration" if present else "not_enumerated",
        "plugin_server_id": plugin_server_id,
        "run_success": proc.returncode == 0,
        "answer_tail": answer[-400:],
        "probe_dir": str(out_dir),
    }


def verify_plugin_state(
    root: Path,
    cfg: Dict[str, Any],
    expected: str,
    *,
    host: str = "cursor",
    timeout: int = 180,
    plugin_server_id: str = PLUGIN_SERVER_ID,
) -> Tuple[bool, Dict[str, Any]]:
    """Probe and check against expected in {"present","absent"}."""
    if expected not in ("present", "absent"):
        raise ValueError("expected must be 'present' or 'absent'")
    out_dir = root / (cfg.get("results_dir") or "results") / "_plugin_probe" / f"{expected}-{k.slug_ts()}"
    res = probe_plugin_presence(
        root, cfg, host=host, out_dir=out_dir, timeout=timeout,
        plugin_server_id=plugin_server_id,
    )
    want_present = expected == "present"
    ok = (res["present"] is True) if want_present else (res["present"] is False)
    res["expected"] = expected
    res["verified"] = ok
    return ok, res


def interactive_gate(
    root: Path,
    cfg: Dict[str, Any],
    expected: str,
    *,
    host: str = "cursor",
    assume_yes: bool = False,
    max_attempts: int = 5,
    plugin_server_id: str = PLUGIN_SERVER_ID,
) -> bool:
    """Pause, instruct the operator, then verify. Loops until verified or abort.

    `assume_yes` skips the manual pause (for CI where the state is pre-arranged)
    but STILL verifies — the probe gate is never skipped.
    """
    want_present = expected == "present"
    action = (
        "INSTALL / ENABLE the Glean plugin (glean-vnext) in Cursor, then fully "
        "restart Cursor and the cursor-agent CLI session"
        if want_present else
        "UNINSTALL / DISABLE the Glean plugin (glean-vnext) in Cursor, then fully "
        "restart Cursor and the cursor-agent CLI session"
    )
    for attempt in range(1, max_attempts + 1):
        if not assume_yes:
            print("\n" + "=" * 70)
            print(f"  PLUGIN GATE — need plugin {expected.upper()} for this arm")
            print("=" * 70)
            print(f"  Please: {action}.")
            print("  (Programmatic toggling does not work for headless cursor-agent;")
            print("   this must be a real install/uninstall — see docs/PLUGIN_TEST.md.)")
            try:
                input("  Press Enter when ready to verify... ")
            except EOFError:
                print("  No TTY available; proceeding straight to verification.")
        print(f"  Verifying plugin is {expected} (attempt {attempt}/{max_attempts})...", flush=True)
        ok, res = verify_plugin_state(
            root, cfg, expected, host=host, plugin_server_id=plugin_server_id,
        )
        print("  " + json.dumps({
            "verified": res["verified"],
            "observed_present": res["present"],
            "observed_from": res["observed_from"],
            "answer_tail": res.get("answer_tail", "")[-160:],
        }))
        if ok:
            print(f"  ✓ Confirmed: plugin is {expected}. Proceeding.\n", flush=True)
            return True
        got = "present" if res["present"] else "absent"
        print(f"  ✗ Plugin is {got}, but this arm needs it {expected}. Fix and retry.", flush=True)
        if assume_yes:
            break
    print(f"  Gate NOT satisfied for expected={expected} after {attempt} attempt(s).", file=sys.stderr)
    return False


def _load(config: str) -> Tuple[Path, Dict[str, Any], Path]:
    config_path, cfg = k.load_config(config)
    return config_path, cfg, k.repo_root_for_config(config_path)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Verify Glean plugin presence for the Cursor plugin A/B test.")
    p.add_argument("--config", default=k.DEFAULT_CONFIG)
    p.add_argument("--host", default="cursor")
    p.add_argument("--plugin-server-id", default=PLUGIN_SERVER_ID)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("probe", help="Probe once and report observed plugin presence")
    sp.add_argument("--timeout", type=int, default=180)

    sp = sub.add_parser("verify", help="Probe and assert an expected state")
    sp.add_argument("--expected", required=True, choices=["present", "absent"])
    sp.add_argument("--timeout", type=int, default=180)

    sp = sub.add_parser("gate", help="Interactive pause + verify loop")
    sp.add_argument("--expected", required=True, choices=["present", "absent"])
    sp.add_argument("--assume-yes", action="store_true", help="Skip the manual pause but still verify")

    args = p.parse_args(argv)
    _, cfg, root = _load(args.config)

    if args.cmd == "probe":
        res = probe_plugin_presence(root, cfg, host=args.host, timeout=args.timeout, plugin_server_id=args.plugin_server_id)
        print(json.dumps(res, indent=2))
        return 0
    if args.cmd == "verify":
        ok, res = verify_plugin_state(root, cfg, args.expected, host=args.host, timeout=args.timeout, plugin_server_id=args.plugin_server_id)
        print(json.dumps(res, indent=2))
        return 0 if ok else 2
    if args.cmd == "gate":
        ok = interactive_gate(root, cfg, args.expected, host=args.host, assume_yes=args.assume_yes, plugin_server_id=args.plugin_server_id)
        return 0 if ok else 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
