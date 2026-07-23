#!/usr/bin/env python3
"""Orchestrate the Cursor Glean-PLUGIN A/B eval end to end.

One CLI session runs both arms, pausing to gate each on a live plugin-presence
probe (arm1 requires the plugin ABSENT, arm2 requires it PRESENT). Because the
Glean plugin cannot be toggled programmatically for headless cursor-agent (see
docs/PLUGIN_TEST.md), the operator physically uninstalls/installs it at each
pause; the gate refuses to proceed until a probe confirms the required state.

    python3 scripts/plugin_ab.py run    --config eval.config.json --participant-id user01
    python3 scripts/plugin_ab.py report --config eval.config.json

Every run is labeled with observed plugin usage (from the transcript), so a
plugin leak into the baseline arm is flagged even if a gate were bypassed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import glean_mcp_eval as k  # noqa: E402
import plugin_gate as pg  # noqa: E402


def _arm_pair(cfg: Dict[str, Any]) -> List[str]:
    pair = cfg.get("arm_pair") or ["arm1_baseline", "arm2_plugin"]
    if len(pair) != 2:
        raise k.EvalError("arm_pair must list exactly two arm names")
    return list(pair)


def _plugin_id(cfg: Dict[str, Any]) -> str:
    return cfg.get("plugin_server_id") or pg.PLUGIN_SERVER_ID


def _sandbox_ok(cfg: Dict[str, Any]) -> bool:
    sb = cfg.get("sandbox") or {}
    return any(str(sb.get(key) or "").strip() for key in ("jira_issue_key", "jira_project_key", "confluence_space_key"))


def _fill_sandbox(prompt: str, cfg: Dict[str, Any]) -> str:
    sb = cfg.get("sandbox") or {}
    return (
        prompt
        .replace("[SANDBOX_ISSUE]", str(sb.get("jira_issue_key") or "[SANDBOX_ISSUE]"))
        .replace("[SANDBOX_PROJECT]", str(sb.get("jira_project_key") or "[SANDBOX_PROJECT]"))
        .replace("[SANDBOX_SPACE]", str(sb.get("confluence_space_key") or "[SANDBOX_SPACE]"))
    )


def _run_arm(config_path: Path, cfg: Dict[str, Any], arm: str, participant: str, *, dry_run: bool, host: str) -> Dict[str, Any]:
    root = k.repo_root_for_config(config_path)
    acfg = k.arm_config(cfg, arm)
    prompts = k.load_prompts(config_path, cfg)
    out_root = k.results_dir(config_path, cfg) / participant / arm
    wrapper = cfg.get("prompt_wrapper") or "{prompt}"
    runs = []
    for i, row in enumerate(prompts, 1):
        pid = k.safe_prompt_id(row["ID"])
        text = k.render_wrapper(wrapper, row)
        text = k.apply_prompt_prefix(acfg, text)
        text = _fill_sandbox(text, cfg)
        run_dir = out_root / pid
        if not dry_run:
            k.write_json(run_dir / "metadata.json", {
                "id": row.get("ID"), "dept": row.get("Dept", ""), "prompt": row.get("Prompt", ""),
                "arm": arm, "participant_id": participant, "ordinal": i,
            })
        print(f"  [{i}/{len(prompts)}] {arm} {pid}: running", flush=True)
        rec = k.run_claude_and_record(
            root, cfg, acfg, text, run_dir,
            timeout=int(cfg.get("run_timeout_seconds", 1800)), dry_run=dry_run, host=host,
        )
        if dry_run:
            continue
        used = list((rec.get("transcript") or {}).get("mcp_servers_used", {}).keys())
        runs.append({"id": row.get("ID"), "success": rec.get("success"), "mcp_servers_used": used})
    return {"arm": arm, "runs": runs}


def command_run(args: argparse.Namespace) -> int:
    config_path, cfg = k.load_config(args.config)
    cfg["__config_path__"] = str(config_path)
    root = k.repo_root_for_config(config_path)
    host = args.host or cfg.get("host") or "cursor"
    pair = _arm_pair(cfg)
    plugin_id = _plugin_id(cfg)

    if not _sandbox_ok(cfg) and not args.dry_run:
        print("ERROR: config 'sandbox' has no jira_issue_key/jira_project_key/confluence_space_key set. "
              "Writes need an explicit sandbox target. Set one or use --dry-run.", file=sys.stderr)
        return 2

    for arm in pair:
        acfg = k.arm_config(cfg, arm)
        need = acfg.get("requires_plugin")  # "present" | "absent" | None
        if need in ("present", "absent") and not args.dry_run:
            ok = pg.interactive_gate(root, cfg, need, host=host, assume_yes=args.assume_yes, plugin_server_id=plugin_id)
            if not ok:
                print(f"ABORT: plugin gate for arm '{arm}' (needs plugin {need}) not satisfied.", file=sys.stderr)
                return 2
        print(f"\n=== running arm '{arm}' (plugin {need}) ===", flush=True)
        _run_arm(config_path, cfg, arm, args.participant_id, dry_run=args.dry_run, host=host)

    if args.dry_run:
        print(json.dumps({"dry_run": True, "arms": pair}, indent=2))
        return 0
    print(json.dumps({"participant_id": args.participant_id, "arms": pair, "next": "python3 scripts/plugin_ab.py report --config " + args.config}, indent=2))
    return 0


def _read_arm_runs(res: Path, participant: str, arm: str) -> Dict[str, Dict[str, Any]]:
    base = res / participant / arm
    out: Dict[str, Dict[str, Any]] = {}
    if not base.exists():
        return out
    for d in sorted(base.iterdir()):
        rec = k.read_run(d) if d.is_dir() else None
        if rec:
            out[d.name] = rec
    return out


def _avg(vals: List[float]) -> float:
    vals = [float(v) for v in vals if isinstance(v, (int, float))]
    return sum(vals) / len(vals) if vals else 0.0


def command_report(args: argparse.Namespace) -> int:
    config_path, cfg = k.load_config(args.config)
    res = k.results_dir(config_path, cfg)
    pair = _arm_pair(cfg)
    plugin_norm = k.normalize_server_name(_plugin_id(cfg))
    participant = args.participant_id

    participants = [participant] if participant else [
        p.name for p in (res.iterdir() if res.exists() else []) if p.is_dir() and not p.name.startswith("_")
    ]

    rows: List[Dict[str, Any]] = []
    for part in participants:
        a1 = _read_arm_runs(res, part, pair[0])
        a2 = _read_arm_runs(res, part, pair[1])
        for pid in sorted(set(a1) & set(a2)):
            r1, r2 = a1[pid], a2[pid]
            def used(r): return {k.normalize_server_name(s) for s in (r.get("transcript") or {}).get("mcp_servers_used", {})}
            u1, u2 = used(r1), used(r2)
            flags = []
            if plugin_norm in u1:
                flags.append("plugin_leak_in_baseline")   # arm1 must NOT touch the plugin
            if plugin_norm not in u2:
                flags.append("plugin_unused_in_plugin_arm")  # arm2 had it available but didn't use it
            if not r1.get("success"):
                flags.append("baseline_run_failed")
            if not r2.get("success"):
                flags.append("plugin_run_failed")
            rows.append({
                "participant": part, "prompt_id": pid,
                "baseline_tokens": r1.get("total_tokens"), "plugin_tokens": r2.get("total_tokens"),
                "baseline_cost": r1.get("computed_cost_usd"), "plugin_cost": r2.get("computed_cost_usd"),
                "baseline_latency_ms": r1.get("duration_ms_reported_by_claude"),
                "plugin_latency_ms": r2.get("duration_ms_reported_by_claude"),
                "baseline_servers": sorted(u1), "plugin_servers": sorted(u2),
                "validity_flags": flags, "valid": not flags,
            })

    valid = [r for r in rows if r["valid"]] or rows
    def pct(a, b): return ((a - b) / a * 100.0) if a else 0.0
    b_tok, p_tok = _avg([r["baseline_tokens"] for r in valid]), _avg([r["plugin_tokens"] for r in valid])
    b_cost, p_cost = _avg([r["baseline_cost"] for r in valid]), _avg([r["plugin_cost"] for r in valid])
    b_lat, p_lat = _avg([r["baseline_latency_ms"] for r in valid]), _avg([r["plugin_latency_ms"] for r in valid])
    leaks = [r for r in rows if "plugin_leak_in_baseline" in r["validity_flags"]]

    md = f"""# Cursor Glean-plugin A/B — summary

Generated: {k.now_iso()}
Eval: `{cfg.get('eval_name','')}`  ·  Arms: `{pair[0]}` (no plugin) vs `{pair[1]}` (plugin)

## Integrity (100%-observed plugin usage)

- Paired prompts: {len(rows)}  ·  valid: {len([r for r in rows if r['valid']])}  ·  flagged: {len([r for r in rows if not r['valid']])}
- **Baseline plugin leaks** (plugin server called in the no-plugin arm — should be 0): **{len(leaks)}**
- Plugin arm did not use the plugin on {len([r for r in rows if 'plugin_unused_in_plugin_arm' in r['validity_flags']])} prompt(s).

> Plugin usage is read from each run transcript's `mcp_servers_used`, so "the plugin was used" is an observed fact. A non-empty leak count means the baseline arm was contaminated (plugin was not actually uninstalled) — exclude those rows.

## Efficiency (plugin OFF vs ON)

| Metric | Baseline (no plugin) | Plugin | Delta |
|---|---:|---:|---:|
| Avg total tokens / task | {b_tok:,.0f} | {p_tok:,.0f} | {pct(p_tok, b_tok):+.1f}% vs baseline |
| Avg list-price cost / task | ${b_cost:,.4f} | ${p_cost:,.4f} | {pct(p_cost, b_cost):+.1f}% vs baseline |
| Avg wall-clock / task | {b_lat/1000:,.1f}s | {p_lat/1000:,.1f}s | {pct(p_lat, b_lat):+.1f}% vs baseline |

> Cursor exposes no per-run billed cost; cost is list-price-normalized via `pricing_per_million`. A positive delta means the plugin arm spent MORE.

## Grounding / quality

Grounding and answer quality are graded by the blind judge. Run it after this report:
`python3 scripts/glean_mcp_eval.py grade` adapted to arms `{pair[0]}`/`{pair[1]}` (see docs/PLUGIN_TEST.md).

Detailed per-prompt rows: [`plugin_ab_rows.json`](plugin_ab_rows.json)
"""
    res.mkdir(parents=True, exist_ok=True)
    (res / "plugin_ab_rows.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    (res / "plugin_ab_summary.md").write_text(md, encoding="utf-8")
    print(json.dumps({
        "paired_prompts": len(rows), "valid": len([r for r in rows if r["valid"]]),
        "baseline_plugin_leaks": len(leaks),
        "summary_md": str(res / "plugin_ab_summary.md"),
        "rows_json": str(res / "plugin_ab_rows.json"),
    }, indent=2))
    return 0


def command_detect(args: argparse.Namespace) -> int:
    """Report which of the A/B servers this Cursor install exposes to headless
    runs, and which arm the operator is currently ready to run."""
    config_path, cfg = k.load_config(args.config)
    root = k.repo_root_for_config(config_path)
    res = pg.probe_plugin_presence(root, cfg, host=args.host or cfg.get("host") or "cursor")
    ans = (res.get("answer_tail") or "").lower()
    present = {
        "glean_default": "glean_default" in ans,
        "plugin-atlassian-atlassian": "atlassian" in ans,
        _plugin_id(cfg): res.get("present", False),
    }
    ready = "arm2_plugin (plugin present)" if res.get("present") else "arm1_baseline (plugin absent)"
    out = {
        "kit": "cursor-glean-plugin-ab",
        "servers_detected": present,
        "glean_plugin_present": res.get("present"),
        "ready_for_arm": ready,
        "note": ("Uninstall the Glean plugin + restart to run arm1_baseline."
                 if res.get("present") else
                 "Install the Glean plugin + restart to run arm2_plugin."),
        "sandbox_configured": _sandbox_ok(cfg),
    }
    print(json.dumps(out, indent=2))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Orchestrate the Cursor Glean-plugin A/B eval (gate + run + report).")
    p.add_argument("--config", default=k.DEFAULT_CONFIG)
    p.add_argument("--host", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("detect", help="Report detected servers and which arm you're ready to run")
    sp.set_defaults(func=command_detect)

    sp = sub.add_parser("run", help="Gate each arm on plugin presence, then run all prompts")
    sp.add_argument("--participant-id", required=True)
    sp.add_argument("--assume-yes", action="store_true", help="Skip manual pauses but still verify via probe")
    sp.add_argument("--dry-run", action="store_true", help="Print planned commands; no gate, no execution")
    sp.set_defaults(func=command_run)

    sp = sub.add_parser("report", help="Aggregate the two arms into plugin_ab_summary.md")
    sp.add_argument("--participant-id", help="Report one participant; omit for all")
    sp.set_defaults(func=command_report)

    args = p.parse_args(argv)
    try:
        return int(args.func(args))
    except k.EvalError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
