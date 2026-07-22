#!/usr/bin/env python3
"""Glean MCP A/B Eval Kit.

Dependency-free CLI for running a crossover evaluation of Glean MCP vs direct
vendor MCPs in Claude Code.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from hosts.base import HostAdapter, get_adapter, register
from hosts import cursor as _cursor  # noqa: F401  importing registers the "cursor" adapter

USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

DEFAULT_CONFIG = "eval.config.json"

# CLI flags this kit depends on. `doctor` probes `claude --help` for these so
# flag drift in a newer/older Claude Code is caught before a customer hits it.
KIT_CLI_FLAGS = [
    "--output-format",
    "--model",
    "--max-turns",
    "--max-budget-usd",
    "--permission-mode",
    "--mcp-config",
    "--strict-mcp-config",
    "--allowedTools",
    "--disallowedTools",
    "--json-schema",
]


class EvalError(Exception):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def slug_ts() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def load_config(path: str) -> Tuple[Path, Dict[str, Any]]:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise EvalError(f"Config not found: {p}")
    cfg = read_json(p)
    if "arms" not in cfg or not isinstance(cfg["arms"], dict):
        raise EvalError("Config must contain an arms object")
    return p, cfg


def repo_root_for_config(config_path: Path) -> Path:
    return config_path.parent


def results_dir(config_path: Path, cfg: Dict[str, Any]) -> Path:
    root = repo_root_for_config(config_path)
    return (root / cfg.get("results_dir", "results")).resolve()


def load_prompts(config_path: Path, cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    root = repo_root_for_config(config_path)
    prompt_file = Path(cfg.get("prompts_file", "golden_prompts.tsv"))
    if not prompt_file.is_absolute():
        prompt_file = root / prompt_file
    if not prompt_file.exists():
        raise EvalError(f"Prompts file not found: {prompt_file}")
    with prompt_file.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        required = {"ID", "Prompt"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise EvalError(f"Prompt TSV must include columns {sorted(required)}")
        rows = []
        for row in reader:
            if not row.get("ID") or not row.get("Prompt"):
                continue
            rows.append({k: (v or "") for k, v in row.items()})
    if not rows:
        raise EvalError(f"No prompts found in {prompt_file}")
    return rows


def arm_config(cfg: Dict[str, Any], arm: str) -> Dict[str, Any]:
    arms = cfg.get("arms", {})
    if arm not in arms:
        raise EvalError(f"Unknown arm {arm!r}; expected one of {', '.join(sorted(arms))}")
    return arms[arm]


def normalize_server_name(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "", name.lower().strip())


def recursively_find_mcp_servers(obj: Any, out: Counter) -> None:
    if isinstance(obj, dict):
        if isinstance(obj.get("mcpServers"), dict):
            for name in obj["mcpServers"].keys():
                out[normalize_server_name(name)] += 1
        for v in obj.values():
            recursively_find_mcp_servers(v, out)
    elif isinstance(obj, list):
        for v in obj:
            recursively_find_mcp_servers(v, out)


def claude_config_paths(root: Path) -> List[Path]:
    home = Path.home()
    candidates = [
        root / ".mcp.json",
        root / ".claude" / "settings.json",
        root / ".claude" / "settings.local.json",
        home / ".claude" / "settings.json",
        home / ".claude.json",
    ]
    if sys.platform == "darwin":
        candidates.append(home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
    else:
        candidates.append(home / ".config" / "Claude" / "claude_desktop_config.json")
    # Preserve order and uniqueness.
    seen = set()
    unique = []
    for p in candidates:
        rp = p.expanduser()
        if str(rp) not in seen:
            unique.append(rp)
            seen.add(str(rp))
    return unique


def static_mcp_inventory(root: Path) -> Dict[str, Any]:
    counts: Counter = Counter()
    files = []
    errors = []
    for p in claude_config_paths(root):
        if not p.exists():
            continue
        try:
            data = read_json(p)
            before = counts.copy()
            recursively_find_mcp_servers(data, counts)
            discovered = sorted((counts - before).elements())
            files.append({"path": str(p), "servers_found": sorted(set(discovered))})
        except Exception as e:  # noqa: BLE001 - report, don't fail.
            errors.append({"path": str(p), "error": str(e)})
    return {"servers": sorted(counts.keys()), "files": files, "errors": errors}


def run_subprocess(cmd: List[str], cwd: Path, timeout: Optional[int] = None) -> Dict[str, Any]:
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return {
            "cmd": cmd,
            "cwd": str(cwd),
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "duration_seconds": round(time.time() - started, 3),
        }
    except FileNotFoundError as e:
        return {
            "cmd": cmd,
            "cwd": str(cwd),
            "returncode": 127,
            "stdout": "",
            "stderr": str(e),
            "duration_seconds": round(time.time() - started, 3),
        }
    except subprocess.TimeoutExpired as e:
        return {
            "cmd": cmd,
            "cwd": str(cwd),
            "returncode": 124,
            "stdout": e.stdout or "",
            "stderr": (e.stderr or "") + f"\nTimed out after {timeout}s",
            "duration_seconds": round(time.time() - started, 3),
        }


def claude_mcp_list(root: Path) -> Dict[str, Any]:
    if shutil.which("claude") is None:
        return {"available": False, "error": "claude CLI not found on PATH", "raw": None, "servers_hint": []}
    res = run_subprocess(["claude", "mcp", "list"], cwd=root, timeout=60)
    raw = (res.get("stdout") or "") + "\n" + (res.get("stderr") or "")
    hints = set()
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        # Common forms include bullets/status glyphs followed by the server name.
        s = re.sub(r"^[✓✔✗!⏸\-•*\s]+", "", s)
        m = re.match(r"([A-Za-z0-9_.-]+)", s)
        if m and m.group(1).lower() not in {"no", "error", "failed", "name", "mcp"}:
            hints.add(normalize_server_name(m.group(1)))
    return {"available": True, "raw": res, "servers_hint": sorted(hints)}


def claude_cli_check(root: Path) -> Dict[str, Any]:
    if shutil.which("claude") is None:
        return {"available": False, "version": None, "flags_supported": {}, "missing_flags": list(KIT_CLI_FLAGS)}
    ver = run_subprocess(["claude", "--version"], cwd=root, timeout=30)
    version_lines = ((ver.get("stdout") or "") + (ver.get("stderr") or "")).strip().splitlines()
    help_res = run_subprocess(["claude", "--help"], cwd=root, timeout=30)
    helptext = (help_res.get("stdout") or "") + "\n" + (help_res.get("stderr") or "")
    flags = {f: (f in helptext) for f in KIT_CLI_FLAGS}
    return {
        "available": True,
        "version": version_lines[0] if version_lines else "",
        "flags_supported": flags,
        "missing_flags": [f for f, ok in flags.items() if not ok],
    }


def server_present(server: str, inventory: Dict[str, Any], mcp_list: Dict[str, Any]) -> bool:
    s = normalize_server_name(server)
    if not s:
        return False
    if s in set(inventory.get("servers", [])) or s in set(mcp_list.get("servers_hint", [])):
        return True
    # Fallback: word-boundary match against the raw `claude mcp list` text so a
    # short forbidden name (e.g. "teams", "zoom") cannot false-match an unrelated
    # substring elsewhere in the output. Boundaries use the normalized charset.
    raw = json.dumps(mcp_list.get("raw") or {}).lower()
    return re.search(r"(?<![a-z0-9_-])" + re.escape(s) + r"(?![a-z0-9_-])", raw) is not None


def inventory_from_file(path: Path) -> Dict[str, Any]:
    counts: Counter = Counter()
    errors = []
    if not path.exists():
        errors.append({"path": str(path), "error": "mcp_config file not found"})
        return {"servers": [], "files": [], "errors": errors}
    try:
        recursively_find_mcp_servers(read_json(path), counts)
    except Exception as e:  # noqa: BLE001 - report, don't fail.
        errors.append({"path": str(path), "error": str(e)})
    servers = sorted(counts.keys())
    return {"servers": servers, "files": [{"path": str(path), "servers_found": servers}], "errors": errors}


def validate_static_setup(root: Path, cfg: Dict[str, Any], arm: str) -> Dict[str, Any]:
    acfg = arm_config(cfg, arm)
    mcp_config_path = resolve_mcp_config(root, acfg)
    if mcp_config_path is not None:
        # Strict per-arm isolation: at runtime the arm uses --strict-mcp-config,
        # so the only servers that exist are those in this file. Validate against
        # the file, not the ambient config (which would include the other arm's).
        inventory = inventory_from_file(mcp_config_path)
        mcp_list = {"available": None, "strict_mode": True, "mcp_config": str(mcp_config_path), "servers_hint": [], "raw": None}
    else:
        inventory = static_mcp_inventory(root)
        mcp_list = claude_mcp_list(root)
    expected = [normalize_server_name(x) for x in acfg.get("expected_mcp_servers", [])]
    forbidden = [normalize_server_name(x) for x in acfg.get("forbidden_mcp_servers", [])]
    missing = [s for s in expected if not server_present(s, inventory, mcp_list)]
    forbidden_found = [s for s in forbidden if server_present(s, inventory, mcp_list)]
    return {
        "arm": arm,
        "expected_mcp_servers": expected,
        "forbidden_mcp_servers": forbidden,
        "configured_inventory": inventory,
        "claude_mcp_list": mcp_list,
        "missing_expected": missing,
        "forbidden_found": forbidden_found,
        "static_pass": not missing and not forbidden_found,
    }


def resolve_mcp_config(root: Path, acfg: Dict[str, Any]) -> Optional[Path]:
    mcp_config = acfg.get("mcp_config")
    if not mcp_config:
        return None
    p = Path(mcp_config)
    if not p.is_absolute():
        p = root / p
    return p


def build_claude_command(
    root: Path,
    cfg: Dict[str, Any],
    acfg: Dict[str, Any],
    prompt: str,
    *,
    model_key: str = "model",
    max_turns: Optional[int] = None,
    json_schema: Optional[Dict[str, Any]] = None,
    bare: bool = False,
) -> List[str]:
    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    model = cfg.get(model_key) or cfg.get("model")
    if model:
        cmd.extend(["--model", str(model)])
    mt = max_turns if max_turns is not None else cfg.get("max_turns")
    if mt:
        cmd.extend(["--max-turns", str(mt)])
    max_budget = cfg.get("max_budget_usd")
    if max_budget is not None:
        cmd.extend(["--max-budget-usd", str(max_budget)])
    permission_mode = cfg.get("permission_mode")
    if permission_mode:
        cmd.extend(["--permission-mode", str(permission_mode)])
    mcp_config_path = resolve_mcp_config(root, acfg)
    if mcp_config_path is not None:
        # Load ONLY this arm's servers and ignore any ambient/global MCP config,
        # so the arms are provably isolated regardless of what else is installed.
        cmd.extend(["--mcp-config", str(mcp_config_path), "--strict-mcp-config"])
    allowed_tools = acfg.get("allowed_tools") or []
    if allowed_tools:
        # Claude Code accepts a comma-separated allow list in current releases.
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])
    disallowed_tools = acfg.get("disallowed_tools") or []
    if disallowed_tools:
        # Deny rules take precedence over allow rules — a hard block for
        # write-capable and arbitrary-dispatch tools (e.g. Glean's run_tool)
        # even if the allow-list is later widened or a server-level grant is used.
        cmd.extend(["--disallowedTools", ",".join(disallowed_tools)])
    if json_schema:
        cmd.extend(["--json-schema", json.dumps(json_schema, separators=(",", ":"))])
    if bare:
        cmd.append("--bare")
    extra_args = cfg.get("extra_claude_args") or []
    if extra_args:
        cmd.extend([str(x) for x in extra_args])
    return cmd


def parse_claude_output(stdout: str) -> Dict[str, Any]:
    try:
        return json.loads(stdout)
    except Exception:
        # Some failures produce non-JSON text. Keep it.
        return {"type": "raw", "result": stdout, "parse_error": True}


def claude_projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


def find_transcript(session_id: Optional[str], cwd: Optional[Path] = None) -> Optional[Path]:
    if not session_id:
        return None
    root = claude_projects_root()
    if not root.exists():
        return None
    candidates = list(root.glob(f"**/{session_id}.jsonl"))
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    # Fallback: scan recently modified files for sessionId.
    all_jsonl = sorted(root.glob("**/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]
    needle = f'"sessionId":"{session_id}"'
    needle_spaced = f'"sessionId": "{session_id}"'
    for p in all_jsonl:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
            if needle in text or needle_spaced in text:
                return p
        except Exception:
            continue
    return None


def iter_content_blocks(content: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block
    elif isinstance(content, dict):
        yield content


def server_from_tool_name(name: str) -> Optional[str]:
    if not name:
        return None
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3:
            return normalize_server_name(parts[1])
        if len(parts) >= 2:
            return normalize_server_name(parts[1])
    return None


def parse_transcript(path: Optional[Path]) -> Dict[str, Any]:
    totals: Dict[str, int] = defaultdict(int)
    unknown_usage: Dict[str, int] = defaultdict(int)
    models: Counter = Counter()
    tool_calls: List[Dict[str, Any]] = []
    assistant_turns = 0
    user_turns = 0
    errors = []
    if path is None or not path.exists():
        return {
            "transcript_path": str(path) if path else None,
            "found": False,
            "usage": dict(totals),
            "unknown_usage": dict(unknown_usage),
            "models": {},
            "tool_calls": [],
            "tool_call_count": 0,
            "mcp_tool_call_count": 0,
            "mcp_servers_used": {},
            "retrieval_attempted": False,
            "assistant_turns": 0,
            "user_turns": 0,
            "errors": ["transcript not found"],
        }
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception as e:
                errors.append(f"line {line_no}: JSON parse error: {e}")
                continue
            typ = obj.get("type")
            if typ == "assistant":
                assistant_turns += 1
                msg = obj.get("message") or {}
                model = msg.get("model") or obj.get("model")
                if model:
                    models[str(model)] += 1
                usage = msg.get("usage") or obj.get("usage") or {}
                if isinstance(usage, dict):
                    for k, v in usage.items():
                        if isinstance(v, bool):
                            continue
                        if isinstance(v, (int, float)):
                            if k in USAGE_KEYS:
                                totals[k] += int(v)
                            elif k.endswith("tokens") or "token" in k:
                                unknown_usage[k] += int(v)
                for block in iter_content_blocks(msg.get("content")):
                    if block.get("type") == "tool_use":
                        name = str(block.get("name") or "")
                        server = server_from_tool_name(name)
                        tool_calls.append({
                            "line": line_no,
                            "id": block.get("id"),
                            "name": name,
                            "server": server,
                            "input_keys": sorted((block.get("input") or {}).keys()) if isinstance(block.get("input"), dict) else [],
                        })
            elif typ == "user":
                user_turns += 1
    mcp_servers = Counter(tc["server"] for tc in tool_calls if tc.get("server"))
    return {
        "transcript_path": str(path),
        "found": True,
        "usage": {k: int(totals.get(k, 0)) for k in USAGE_KEYS},
        "unknown_usage": dict(unknown_usage),
        "models": dict(models),
        "tool_calls": tool_calls,
        "tool_call_count": len(tool_calls),
        "mcp_tool_call_count": sum(1 for tc in tool_calls if tc.get("server")),
        "mcp_servers_used": dict(mcp_servers),
        "retrieval_attempted": bool(tool_calls),
        "assistant_turns": assistant_turns,
        "user_turns": user_turns,
        "errors": errors,
    }


def usage_total_tokens(usage: Dict[str, int]) -> int:
    return sum(int(usage.get(k, 0)) for k in USAGE_KEYS)


def cost_for_usage(cfg: Dict[str, Any], usage: Dict[str, int]) -> float:
    rates = cfg.get("pricing_per_million") or {}
    return sum((float(rates.get(k, 0.0)) * int(usage.get(k, 0)) / 1_000_000.0) for k in USAGE_KEYS)


class ClaudeCodeAdapter(HostAdapter):
    """Claude Code host: `claude -p`, harvest usage from the local JSONL transcript."""

    name = "claude-code"
    caps = {
        "per_arm_isolation": True,      # --mcp-config <file> --strict-mcp-config
        "readonly_gating": True,        # --allowedTools / --disallowedTools
        "per_run_token_usage": True,    # harvested from ~/.claude/projects/**.jsonl
        "reported_cost": True,          # total_cost_usd in the -p JSON result
        "structured_output": True,      # --json-schema
    }

    def executable_present(self) -> bool:
        return shutil.which("claude") is not None

    def build_command(self, root, cfg, arm_cfg, prompt, out_dir, *, model_key="model", max_turns=None, json_schema=None):
        cmd = build_claude_command(root, cfg, arm_cfg, prompt, model_key=model_key, max_turns=max_turns, json_schema=json_schema)
        return cmd, {}

    def harvest(self, proc, root, out_dir, cfg, ctx):
        parsed = parse_claude_output(proc.get("stdout") or "")
        write_json(out_dir / "claude_output.json", parsed)
        answer = parsed.get("result") or parsed.get("structured_output") or ""
        answer_text = json.dumps(answer, indent=2, sort_keys=True) if isinstance(answer, (dict, list)) else str(answer)
        session_id = parsed.get("session_id") or parsed.get("sessionId")
        transcript = parse_transcript(find_transcript(session_id, root))
        return {
            "ok": proc.get("returncode") == 0 and not parsed.get("is_error", False),
            "session_id": session_id,
            "transcript": transcript,
            "usage": transcript.get("usage", {}),
            "reported_cost_usd": parsed.get("total_cost_usd") or parsed.get("cost_usd"),
            "duration_ms": parsed.get("duration_ms"),
            "num_turns": parsed.get("num_turns"),
            "answer_text": answer_text,
            "raw_output_path": str(out_dir / "claude_output.json"),
            "output_type": parsed.get("type"),
            "output_subtype": parsed.get("subtype"),
        }

    def doctor(self, root):
        return {
            "claude_cli": claude_cli_check(root),
            "static_mcp_inventory": static_mcp_inventory(root),
            "claude_mcp_list": claude_mcp_list(root),
        }


register(ClaudeCodeAdapter())


def run_claude_and_record(
    root: Path,
    cfg: Dict[str, Any],
    acfg: Dict[str, Any],
    prompt: str,
    out_dir: Path,
    *,
    timeout: int,
    model_key: str = "model",
    max_turns: Optional[int] = None,
    json_schema: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
    host: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one prompt via the selected host adapter and write a normalized run.json.

    Host-specific work (build command, harvest usage) is delegated to the adapter;
    everything else here — file layout, computed cost, run.json shape — is shared,
    so `report`/`grade`/`package` are identical across hosts.
    """
    adapter = get_adapter(host or cfg.get("host"))
    cmd, ctx = adapter.build_command(
        root, cfg, acfg, prompt, out_dir,
        model_key=model_key, max_turns=max_turns, json_schema=json_schema,
    )
    if dry_run:
        print("DRY-RUN " + " ".join(shlex.quote(c) for c in cmd), flush=True)
        return {"dry_run": True, "cmd": cmd, "success": None, "transcript": parse_transcript(None), "usage": {}, "total_tokens": 0}
    out_dir.mkdir(parents=True, exist_ok=True)
    write_text(out_dir / "prompt.txt", prompt)
    if not adapter.executable_present():
        result = {
            "started_at": now_iso(),
            "host": adapter.name,
            "returncode": 127,
            "success": False,
            "error": f"{adapter.name} executable not found on PATH",
            "transcript": parse_transcript(None),
            "usage": {},
            "total_tokens": 0,
        }
        write_json(out_dir / "run.json", result)
        return result
    adapter.prepare(ctx)
    started_at = now_iso()
    cwd = Path(ctx["cwd"]) if ctx.get("cwd") else root
    proc = run_subprocess(cmd, cwd=cwd, timeout=timeout)
    write_text(out_dir / "stdout.txt", proc.get("stdout") or "")
    write_text(out_dir / "stderr.txt", proc.get("stderr") or "")
    write_json(out_dir / "command.json", {"cmd": cmd, "cwd": str(cwd), "started_at": started_at, "subprocess": proc})
    h = adapter.harvest(proc, root, out_dir, cfg, ctx)
    transcript = h.get("transcript") or parse_transcript(None)
    usage = h.get("usage") or transcript.get("usage", {}) or {}
    write_text(out_dir / "answer.md", h.get("answer_text", ""))
    record = {
        "started_at": started_at,
        "completed_at": now_iso(),
        "host": adapter.name,
        "returncode": proc.get("returncode"),
        "success": bool(h.get("ok")),
        "session_id": h.get("session_id"),
        "claude_output_type": h.get("output_type"),
        "claude_output_subtype": h.get("output_subtype"),
        # "reported_by_claude" keys are kept for stable downstream columns; they hold
        # the host-reported figures (None for hosts that do not expose them, e.g. Cursor cost).
        "total_cost_usd_reported_by_claude": h.get("reported_cost_usd"),
        "duration_ms_reported_by_claude": h.get("duration_ms"),
        "num_turns_reported_by_claude": h.get("num_turns"),
        "transcript": transcript,
        "usage": usage,
        "total_tokens": usage_total_tokens(usage),
        "computed_cost_usd": round(cost_for_usage(cfg, usage), 6),
        "answer_path": str(out_dir / "answer.md"),
        "raw_output_path": h.get("raw_output_path"),
        "stderr_path": str(out_dir / "stderr.txt"),
    }
    write_json(out_dir / "run.json", record)
    return record


def command_preflight(args: argparse.Namespace) -> int:
    config_path, cfg = load_config(args.config)
    root = repo_root_for_config(config_path)
    acfg = arm_config(cfg, args.arm)
    host = getattr(args, "host", None) or cfg.get("host") or "claude-code"
    out = results_dir(config_path, cfg) / "_preflight" / args.arm / slug_ts()
    static = validate_static_setup(root, cfg, args.arm)
    live_record = None
    live_pass = None
    if args.live:
        prompt = acfg.get("preflight_prompt") or f"Use the {args.arm} retrieval tools once and report whether setup works."
        live_record = run_claude_and_record(
            root,
            cfg,
            acfg,
            prompt,
            out / "live",
            timeout=int(cfg.get("preflight_timeout_seconds", 300)),
            max_turns=int(cfg.get("preflight_max_turns", 6)),
            dry_run=args.dry_run,
            host=host,
        )
        observed = set((live_record.get("transcript") or {}).get("mcp_servers_used", {}).keys())
        required = set(normalize_server_name(x) for x in acfg.get("require_live_tool_servers", acfg.get("expected_mcp_servers", [])))
        # For live preflight, require successful run and at least some required retrieval evidence.
        live_pass = bool(live_record.get("success")) and (not required or bool(observed & required))
    overall_pass = bool(static.get("static_pass")) and (live_pass is not False)
    record = {
        "created_at": now_iso(),
        "eval_name": cfg.get("eval_name"),
        "host": host,
        "arm": args.arm,
        "static": static,
        "live_enabled": bool(args.live),
        "live_pass": live_pass,
        "live_record": live_record,
        "pass": overall_pass,
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, "arm": args.arm, "static_pass": static.get("static_pass")}, indent=2))
        return 0
    write_json(out / "preflight.json", record)
    latest = results_dir(config_path, cfg) / "_preflight" / args.arm / "latest.json"
    write_json(latest, record)
    print(json.dumps({
        "pass": overall_pass,
        "preflight_path": str(out / "preflight.json"),
        "missing_expected": static.get("missing_expected"),
        "forbidden_found": static.get("forbidden_found"),
        "live_pass": live_pass,
    }, indent=2))
    return 0 if overall_pass else 2


def safe_prompt_id(prompt_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", prompt_id).strip("_") or "prompt"


def render_wrapper(wrapper: str, row: Dict[str, str]) -> str:
    # Literal substitution, NOT str.format(): a golden prompt may legitimately
    # contain { or } (JSON, code, table names), which would crash .format().
    out = wrapper
    for placeholder, value in (
        ("{prompt}", row.get("Prompt", "")),
        ("{id}", row.get("ID", "")),
        ("{dept}", row.get("Dept", "")),
    ):
        out = out.replace(placeholder, value)
    return out


def apply_prompt_prefix(acfg: Dict[str, Any], prompt_text: str) -> str:
    """Prepend an arm's optional `prompt_prefix` steering instruction.

    Used to name the retrieval tool an arm should use (e.g. "Use the Atlassian
    MCP..."). Symmetric across arms and orthogonal to isolation: the
    server-isolation guardrail still flags any run that reaches another server,
    so steering guides but never hides contamination.
    """
    prefix = str((acfg or {}).get("prompt_prefix") or "").strip()
    return prefix + "\n\n" + prompt_text if prefix else prompt_text


def command_run(args: argparse.Namespace) -> int:
    config_path, cfg = load_config(args.config)
    root = repo_root_for_config(config_path)
    acfg = arm_config(cfg, args.arm)
    host = getattr(args, "host", None) or cfg.get("host") or "claude-code"
    prompts = load_prompts(config_path, cfg)
    out_root = results_dir(config_path, cfg) / args.participant_id / args.arm
    wrapper = cfg.get("prompt_wrapper") or "{prompt}"
    manifest = {
        "eval_name": cfg.get("eval_name"),
        "participant_id": args.participant_id,
        "arm": args.arm,
        "started_at": now_iso(),
        "prompt_count": len(prompts),
        "runs": [],
    }
    failures = 0
    for i, row in enumerate(prompts, 1):
        pid = safe_prompt_id(row["ID"])
        prompt_text = render_wrapper(wrapper, row)
        # Optional per-arm steering: prepend an instruction naming the retrieval
        # tool the arm is meant to use (e.g. "Use the Atlassian MCP..."). This
        # reduces tool-selection noise between arms; the server-isolation
        # guardrail still flags any run that ignores it and reaches another
        # server, so steering never masks contamination.
        prompt_text = apply_prompt_prefix(acfg, prompt_text)
        run_dir = out_root / pid
        metadata = {
            "id": row.get("ID"),
            "dept": row.get("Dept", ""),
            "prompt": row.get("Prompt", ""),
            "arm": args.arm,
            "participant_id": args.participant_id,
            "ordinal": i,
        }
        if not args.dry_run:
            write_json(run_dir / "metadata.json", metadata)
        print(f"[{i}/{len(prompts)}] {args.arm} {pid}: running", flush=True)
        rec = run_claude_and_record(
            root,
            cfg,
            acfg,
            prompt_text,
            run_dir,
            timeout=int(cfg.get("run_timeout_seconds", 1800)),
            dry_run=args.dry_run,
            host=host,
        )
        if args.dry_run:
            continue
        if not rec.get("success"):
            failures += 1
        manifest["runs"].append({
            "id": row.get("ID"),
            "dir": str(run_dir),
            "success": rec.get("success"),
            "session_id": rec.get("session_id"),
            "total_tokens": rec.get("total_tokens"),
            "computed_cost_usd": rec.get("computed_cost_usd"),
            "retrieval_attempted": (rec.get("transcript") or {}).get("retrieval_attempted"),
        })
        print(
            f"[{i}/{len(prompts)}] {args.arm} {pid}: "
            f"success={rec.get('success')} tokens={rec.get('total_tokens')} "
            f"retrieval={(rec.get('transcript') or {}).get('retrieval_attempted')}",
            flush=True,
        )
    if args.dry_run:
        print(json.dumps({"dry_run": True, "arm": args.arm, "prompts": len(prompts)}, indent=2))
        return 0
    manifest["completed_at"] = now_iso()
    manifest["failure_count"] = failures
    write_json(out_root / "arm_manifest.json", manifest)
    print(json.dumps({"participant_id": args.participant_id, "arm": args.arm, "runs": len(prompts), "failures": failures, "results_dir": str(out_root)}, indent=2))
    return 0 if failures == 0 else 1


def grade_schema() -> Dict[str, Any]:
    # Blind schema: the judge sees "Answer A" / "Answer B" and is never told which
    # arm is Glean. Results are de-blinded back to glean/direct after grading.
    return {
        "type": "object",
        "properties": {
            "winner": {"type": "string", "enum": ["A", "B", "tie"]},
            "completeness_winner": {"type": "string", "enum": ["A", "B", "tie"]},
            "groundedness_winner": {"type": "string", "enum": ["A", "B", "tie"]},
            "usefulness_winner": {"type": "string", "enum": ["A", "B", "tie"]},
            "efficiency_winner": {"type": "string", "enum": ["A", "B", "tie"]},
            "completeness_a": {"type": "number", "minimum": 1, "maximum": 5},
            "completeness_b": {"type": "number", "minimum": 1, "maximum": 5},
            "groundedness_a": {"type": "number", "minimum": 1, "maximum": 5},
            "groundedness_b": {"type": "number", "minimum": 1, "maximum": 5},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reasoning": {"type": "string"},
            "watchouts": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "winner",
            "completeness_winner",
            "groundedness_winner",
            "usefulness_winner",
            "efficiency_winner",
            "completeness_a",
            "completeness_b",
            "groundedness_a",
            "groundedness_b",
            "confidence",
            "reasoning",
            "watchouts",
        ],
        "additionalProperties": True,
    }


def blind_assignment(participant_id: str, prompt_id: str) -> bool:
    # Deterministic, auditable A/B coin flip. Returns True when Glean is presented
    # as "Answer A". Stable across reruns so a regrade reproduces the same layout.
    h = hashlib.sha256(f"{participant_id}/{prompt_id}".encode("utf-8")).hexdigest()
    return int(h[:8], 16) % 2 == 0

def _pick_alias(bg: Dict[str, Any], *keys: str) -> Any:
    """Return the first present, non-empty value among keys.

    Schema-free hosts vary key names and sometimes emit an explicit null for the
    canonical key, so callers treat any falsy value as "missing".
    """
    for k in keys:
        v = bg.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _normalize_judge_aliases(bg: Dict[str, Any]) -> None:
    """In-place: map a schema-free judge's output onto the kit's canonical keys.

    Claude Code fills winner/efficiency_winner/reasoning/confidence from the
    enforced JSON schema. Cursor ignores the schema and emits prose-driven
    variants (e.g. winner_overall, winner_quality, winner_tokens) and may set
    winner: null outright, so we fall back on any falsy value rather than only a
    missing key. Numeric confidence is bucketed to high/medium/low.
    """
    if not isinstance(bg, dict):
        return
    if not bg.get("winner"):
        bg["winner"] = _pick_alias(bg, "overall_winner", "winner_overall", "quality_winner", "winner_quality")
    if not bg.get("efficiency_winner"):
        bg["efficiency_winner"] = _pick_alias(bg, "token_efficiency_winner", "winner_tokens", "token_winner", "efficiency")
    if not bg.get("usefulness_winner"):
        bg["usefulness_winner"] = _pick_alias(bg, "usefulness", "quality_winner", "winner_quality", "winner")
    if not bg.get("reasoning"):
        bg["reasoning"] = _pick_alias(bg, "quality_reasoning", "rationale", "explanation", "justification")
    conf = bg.get("confidence")
    if not isinstance(conf, bool) and isinstance(conf, (int, float)):
        bg["confidence"] = "high" if float(conf) >= 0.8 else "medium" if float(conf) >= 0.5 else "low"


def _coerce_score(value: Any) -> Optional[int]:
    """Coerce a judge score to an int in [1, 5], or None if it isn't usable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = int(round(value))
    elif isinstance(value, str):
        m = re.search(r"-?\d+(?:\.\d+)?", value)
        if not m:
            return None
        n = int(round(float(m.group())))
    else:
        return None
    return max(1, min(5, n))


def _normalize_numeric_scores(bg: Dict[str, Any]) -> None:
    """In-place: populate blind completeness_{a,b}/groundedness_{a,b} integers.

    Claude Code fills these from the enforced JSON schema. Schema-free hosts such
    as Cursor emit only what the prose asked for and may nest the scores (e.g.
    {"scores": {"completeness": {"a": 4, "b": 5}}}) or vary casing. Map those
    shapes to the kit's flat keys and clamp to integers in [1, 5]; leave the
    field absent when no usable value is found so it renders as blank, not 0.
    """
    if not isinstance(bg, dict):
        return
    nested: Dict[str, Any] = {}
    for container_key in ("scores", "quality_scores", "ratings"):
        val = bg.get(container_key)
        if isinstance(val, dict):
            nested = val
            break
    for dim in ("completeness", "groundedness"):
        dim_obj = nested.get(dim) if isinstance(nested.get(dim), dict) else {}
        for side in ("a", "b"):
            target = f"{dim}_{side}"
            existing = _coerce_score(bg.get(target))
            if existing is not None:
                bg[target] = existing
                continue
            candidate = (
                bg.get(f"{dim}_{side.upper()}")
                or bg.get(f"{dim}_answer_{side}")
                or dim_obj.get(side)
                or dim_obj.get(side.upper())
                or dim_obj.get(f"answer_{side}")
            )
            coerced = _coerce_score(candidate)
            if coerced is not None:
                bg[target] = coerced


def deblind_grade(bg: Dict[str, Any], glean_is_a: bool) -> Dict[str, Any]:
    # Translate an A/B judge result back into glean/direct keys so downstream
    # aggregation (collect_aggregate_rows) is unchanged.
    if not isinstance(bg, dict):
        return bg

    def ab_to_arm(v: Any) -> Any:
        if v == "A":
            return "glean" if glean_is_a else "direct"
        if v == "B":
            return "direct" if glean_is_a else "glean"
        return v

    out: Dict[str, Any] = {}
    for wk in ("winner", "completeness_winner", "groundedness_winner", "usefulness_winner", "efficiency_winner"):
        if wk in bg:
            out[wk] = ab_to_arm(bg.get(wk))
    if "completeness_a" in bg or "completeness_b" in bg:
        out["completeness_glean"] = bg.get("completeness_a") if glean_is_a else bg.get("completeness_b")
        out["completeness_direct"] = bg.get("completeness_b") if glean_is_a else bg.get("completeness_a")
    if "groundedness_a" in bg or "groundedness_b" in bg:
        out["groundedness_glean"] = bg.get("groundedness_a") if glean_is_a else bg.get("groundedness_b")
        out["groundedness_direct"] = bg.get("groundedness_b") if glean_is_a else bg.get("groundedness_a")
    for pk in ("confidence", "reasoning", "watchouts"):
        if pk in bg:
            out[pk] = bg.get(pk)
    return out


def read_run(run_dir: Path) -> Optional[Dict[str, Any]]:
    p = run_dir / "run.json"
    if not p.exists():
        return None
    data = read_json(p)
    meta = read_json(run_dir / "metadata.json") if (run_dir / "metadata.json").exists() else {}
    answer = (run_dir / "answer.md").read_text(encoding="utf-8", errors="ignore") if (run_dir / "answer.md").exists() else ""
    data["_dir"] = str(run_dir)
    data["_metadata"] = meta
    data["_answer"] = answer
    return data


def participant_dirs(res_dir: Path, participant_id: Optional[str]) -> List[Path]:
    if participant_id:
        p = res_dir / participant_id
        return [p] if p.exists() else []
    return sorted([p for p in res_dir.iterdir() if p.is_dir() and not p.name.startswith("_")]) if res_dir.exists() else []


def paired_prompt_dirs(participant_dir: Path) -> List[Tuple[str, Path, Path]]:
    glean = participant_dir / "glean"
    direct = participant_dir / "direct"
    if not glean.exists() or not direct.exists():
        return []
    ids = sorted({p.name for p in glean.iterdir() if p.is_dir()} & {p.name for p in direct.iterdir() if p.is_dir()})
    return [(pid, glean / pid, direct / pid) for pid in ids]


def marginal_tokens(run: Dict[str, Any]) -> int:
    u = run.get("usage") or {}
    return int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0))


def judge_prompt(meta: Dict[str, Any], run_a: Dict[str, Any], run_b: Dict[str, Any], hide_tokens: bool = False) -> str:
    if hide_tokens:
        # Pure-quality pass: token counts are withheld so they cannot anchor the
        # quality scores (see docs/METHODOLOGY.md).
        guidance = "Judge purely on quality: completeness, groundedness, and usefulness."
        a_tok = b_tok = ""
    else:
        guidance = (
            "Judge quality first (completeness, groundedness, usefulness). Prefer lower token "
            "usage only when quality is materially similar. Do not reward a shorter answer if it "
            "is incomplete, vague, or unsupported."
        )
        a_tok = f"Answer A work tokens (input+output): {marginal_tokens(run_a)}\n"
        b_tok = f"Answer B work tokens (input+output): {marginal_tokens(run_b)}\n"
    return (
        "You are an impartial evaluation judge for an A/B test comparing two assistant "
        "configurations on enterprise knowledge tasks. You are NOT told which system produced "
        "which answer; judge only on the merits.\n\n"
        f"{guidance}\n\n"
        f"Query ID: {meta.get('id')}\n"
        f"Department: {meta.get('dept')}\n"
        f"Query: {meta.get('prompt')}\n\n"
        f"{a_tok}Answer A:\n{run_a.get('_answer', '')}\n\n"
        f"{b_tok}Answer B:\n{run_b.get('_answer', '')}\n\n"
        # Hosts with JSON-schema enforcement (e.g. Claude Code) fill the numeric
        # score fields from the schema, but schema-free hosts (e.g. Cursor) only
        # emit what the prose asks for. Spell the integer scores out explicitly so
        # completeness/groundedness are populated regardless of host.
        "Also rate each answer on two dimensions using an integer from 1 (worst) "
        "to 5 (best):\n"
        "- completeness: how fully the answer addresses the query.\n"
        "- groundedness: how well claims are supported by cited/verifiable sources.\n"
        "Report them as the JSON fields completeness_a, completeness_b, "
        "groundedness_a, and groundedness_b (integers 1-5), where _a is Answer A "
        "and _b is Answer B.\n\n"
        'Return the required JSON object only, using "A", "B", or "tie" for the winner fields.'
    )


def command_grade(args: argparse.Namespace) -> int:
    config_path, cfg = load_config(args.config)
    root = repo_root_for_config(config_path)
    res = results_dir(config_path, cfg)
    participants = participant_dirs(res, args.participant_id)
    if not participants:
        print("No participant result directories found", file=sys.stderr)
        return 1
    failures = 0
    # Lock the judge down: no MCP servers (empty strict config) and no write/exec
    # built-ins. It only needs to read the two answers and emit structured JSON.
    judge_acfg = {
        "allowed_tools": [],
        "disallowed_tools": cfg.get("judge_disallowed_tools", ["Bash", "Write", "Edit", "NotebookEdit"]),
        "mcp_config": cfg.get("judge_mcp_config", "config/mcp.none.json"),
    }
    for pdir in participants:
        for pid, glean_dir, direct_dir in paired_prompt_dirs(pdir):
            grade_path = pdir / "grades" / pid / "grade.json"
            if grade_path.exists() and not args.force:
                print(f"skip existing grade {pdir.name}/{pid}")
                continue
            glean_run = read_run(glean_dir)
            direct_run = read_run(direct_dir)
            if not glean_run or not direct_run:
                continue
            meta = glean_run.get("_metadata") or direct_run.get("_metadata") or {"id": pid}
            glean_is_a = blind_assignment(pdir.name, pid)
            run_a, run_b = (glean_run, direct_run) if glean_is_a else (direct_run, glean_run)
            prompt = judge_prompt(meta, run_a, run_b, hide_tokens=bool(cfg.get("judge_hide_tokens", False)))
            print(f"grading {pdir.name}/{pid} (glean shown as {'A' if glean_is_a else 'B'})", flush=True)
            rec = run_claude_and_record(
                root,
                cfg,
                judge_acfg,
                prompt,
                grade_path.parent / "judge_run",
                timeout=int(cfg.get("judge_timeout_seconds", 900)),
                model_key="judge_model",
                max_turns=int(cfg.get("judge_max_turns", 3)),
                json_schema=grade_schema(),
                # Judge runs on a structured-output-capable host (default claude-code),
                # regardless of which host the arms ran on — keeps quality scores
                # comparable across hosts and enforces the JSON-schema grade.
                host=cfg.get("judge_host") or "claude-code",
            )
            raw = read_json(Path(rec["raw_output_path"])) if rec.get("raw_output_path") and Path(rec["raw_output_path"]).exists() else {}
            # Claude returns one JSON object; Cursor's stream-json adapter stores
            # a list of events, with the final answer in the result event. Cursor
            # also does not enforce --json-schema, so accept its equivalent
            # quality_* fields and normalize them to the kit schema below.
            parse_obj = raw
            if isinstance(raw, list):
                result_events = [e for e in raw if isinstance(e, dict) and e.get("type") == "result"]
                parse_obj = result_events[-1] if result_events else {}
            blind_grade = parse_obj.get("structured_output") if isinstance(parse_obj, dict) else None
            if not blind_grade and isinstance(parse_obj, dict) and isinstance(parse_obj.get("result"), str):
                result_text = parse_obj["result"]
                candidates = re.findall(r"```(?:json)?\\s*(\\{.*?\\})\\s*```", result_text, flags=re.DOTALL | re.IGNORECASE)
                # Cursor may omit fences and prepend analysis. Try every opening
                # brace from the end, using raw_decode to tolerate trailing prose.
                candidates += [result_text[i:] for i, ch in enumerate(result_text) if ch == "{"]
                for candidate in reversed(candidates):
                    try:
                        parsed, _ = json.JSONDecoder().raw_decode(candidate.strip())
                        if isinstance(parsed, dict):
                            blind_grade = parsed
                            break
                    except Exception:
                        continue
            if isinstance(blind_grade, dict):
                _normalize_judge_aliases(blind_grade)
                # Numeric quality scores. Schema-free hosts (Cursor) may nest the
                # scores or vary the key casing; normalize to the kit's flat
                # completeness_{a,b}/groundedness_{a,b} integers so the aggregate
                # quality table is populated instead of showing 0.00.
                _normalize_numeric_scores(blind_grade)
            if not isinstance(blind_grade, dict):
                failures += 1
                blind_grade = None
                grade = {"error": "judge did not return parseable structured output", "raw": raw}
            else:
                grade = deblind_grade(blind_grade, glean_is_a)
            grade_record = {
                "created_at": now_iso(),
                "participant_id": pdir.name,
                "prompt_id": pid,
                "query_id": meta.get("id", pid),
                "judge_run_dir": str(grade_path.parent / "judge_run"),
                "blind_assignment": {"glean_label": "A" if glean_is_a else "B", "direct_label": "B" if glean_is_a else "A"},
                "blind_grade": blind_grade,
                "grade": grade,
            }
            write_json(grade_path, grade_record)
    print(json.dumps({"participants": [p.name for p in participants], "grade_failures": failures}, indent=2))
    return 0 if failures == 0 else 1


def _server_isolation_flags(cfg: Optional[Dict[str, Any]], arm: str, rec: Dict[str, Any]) -> List[str]:
    """Flag a run whose MCP server usage escaped its arm's isolation.

    Cursor merges the global ~/.cursor/mcp.json and installed plugins into every
    workspace and cursor-agent has no --strict-mcp-config flag, so an arm can
    call a server it was never meant to (e.g. the 'direct' arm reaching
    glean_default, or the 'glean' arm reaching an Atlassian plugin). Those rows
    are not a valid A/B comparison and must be excluded from headline stats.

    Sanctioned servers are expected_mcp_servers UNION require_live_tool_servers,
    so reaching the target via the workspace mcp.json server or an equivalent
    plugin both count as clean.
    """
    out: List[str] = []
    if not cfg:
        return out
    acfg = (cfg.get("arms") or {}).get(arm) or {}
    used = {
        normalize_server_name(k)
        for k in ((rec.get("transcript") or {}).get("mcp_servers_used") or {})
    }
    if not used:
        return out
    forbidden = {normalize_server_name(x) for x in (acfg.get("forbidden_mcp_servers") or [])}
    # Sanctioned = expected_mcp_servers UNION require_live_tool_servers: an arm may
    # legitimately reach its target via the workspace mcp.json server or an
    # equivalent plugin (e.g. 'atlassian' vs 'plugin-atlassian-atlassian').
    expected = {
        normalize_server_name(x)
        for x in (
            list(acfg.get("expected_mcp_servers") or [])
            + list(acfg.get("require_live_tool_servers") or [])
        )
    }
    if used & forbidden:
        out.append(f"{arm}_forbidden_server")
    if expected and (used - expected):
        out.append(f"{arm}_unexpected_server")
    return out


def validity_flags(
    glean: Dict[str, Any], direct: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None
) -> List[str]:
    flags = []
    if not glean.get("success"):
        flags.append("glean_run_failed")
    if not direct.get("success"):
        flags.append("direct_run_failed")
    if not (glean.get("transcript") or {}).get("retrieval_attempted"):
        flags.append("glean_no_retrieval")
    if not (direct.get("transcript") or {}).get("retrieval_attempted"):
        flags.append("direct_no_retrieval")
    gm = set((glean.get("transcript") or {}).get("models", {}).keys())
    dm = set((direct.get("transcript") or {}).get("models", {}).keys())
    if gm and dm and gm != dm:
        flags.append("model_mismatch")
    flags.extend(_server_isolation_flags(cfg, "glean", glean))
    flags.extend(_server_isolation_flags(cfg, "direct", direct))
    return flags


def collect_aggregate_rows(res: Path, cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    rows = []
    for pdir in participant_dirs(res, None):
        for pid, glean_dir, direct_dir in paired_prompt_dirs(pdir):
            glean = read_run(glean_dir)
            direct = read_run(direct_dir)
            if not glean or not direct:
                continue
            meta = glean.get("_metadata") or direct.get("_metadata") or {}
            grade_path = pdir / "grades" / pid / "grade.json"
            grade = read_json(grade_path).get("grade") if grade_path.exists() else {}
            flags = validity_flags(glean, direct, cfg)
            gt = int(glean.get("total_tokens") or 0)
            dtok = int(direct.get("total_tokens") or 0)
            gcost = float(glean.get("computed_cost_usd") or 0.0)
            dcost = float(direct.get("computed_cost_usd") or 0.0)
            g_usage = glean.get("usage") or {}
            d_usage = direct.get("usage") or {}
            g_marginal = int(g_usage.get("input_tokens", 0)) + int(g_usage.get("output_tokens", 0))
            d_marginal = int(d_usage.get("input_tokens", 0)) + int(d_usage.get("output_tokens", 0))
            g_rcost_raw = glean.get("total_cost_usd_reported_by_claude")
            d_rcost_raw = direct.get("total_cost_usd_reported_by_claude")
            g_rcost = float(g_rcost_raw) if isinstance(g_rcost_raw, (int, float)) else ""
            d_rcost = float(d_rcost_raw) if isinstance(d_rcost_raw, (int, float)) else ""
            g_reported_savings = (
                round((d_rcost - g_rcost) / d_rcost * 100.0, 2)
                if isinstance(g_rcost, (int, float)) and isinstance(d_rcost, (int, float)) and d_rcost
                else ""
            )
            g_lat = glean.get("duration_ms_reported_by_claude")
            d_lat = direct.get("duration_ms_reported_by_claude")
            rows.append({
                "participant_id": pdir.name,
                "prompt_dir_id": pid,
                "query_id": meta.get("id", pid),
                "dept": meta.get("dept", ""),
                "prompt": meta.get("prompt", ""),
                "glean_total_tokens": gt,
                "direct_total_tokens": dtok,
                "token_savings_pct": round((dtok - gt) / dtok * 100.0, 2) if dtok else "",
                "glean_cost_usd": gcost,
                "direct_cost_usd": dcost,
                "cost_savings_pct": round((dcost - gcost) / dcost * 100.0, 2) if dcost else "",
                "glean_reported_cost_usd": round(g_rcost, 6) if isinstance(g_rcost, (int, float)) else "",
                "direct_reported_cost_usd": round(d_rcost, 6) if isinstance(d_rcost, (int, float)) else "",
                "reported_cost_savings_pct": g_reported_savings,
                "glean_marginal_tokens": g_marginal,
                "direct_marginal_tokens": d_marginal,
                "marginal_token_savings_pct": round((d_marginal - g_marginal) / d_marginal * 100.0, 2) if d_marginal else "",
                "glean_latency_ms": g_lat if isinstance(g_lat, (int, float)) else "",
                "direct_latency_ms": d_lat if isinstance(d_lat, (int, float)) else "",
                "latency_savings_pct": round((d_lat - g_lat) / d_lat * 100.0, 2) if (isinstance(g_lat, (int, float)) and isinstance(d_lat, (int, float)) and d_lat) else "",
                "glean_input_tokens": (glean.get("usage") or {}).get("input_tokens", 0),
                "direct_input_tokens": (direct.get("usage") or {}).get("input_tokens", 0),
                "glean_output_tokens": (glean.get("usage") or {}).get("output_tokens", 0),
                "direct_output_tokens": (direct.get("usage") or {}).get("output_tokens", 0),
                "glean_cache_write_tokens": (glean.get("usage") or {}).get("cache_creation_input_tokens", 0),
                "direct_cache_write_tokens": (direct.get("usage") or {}).get("cache_creation_input_tokens", 0),
                "glean_cache_read_tokens": (glean.get("usage") or {}).get("cache_read_input_tokens", 0),
                "direct_cache_read_tokens": (direct.get("usage") or {}).get("cache_read_input_tokens", 0),
                "glean_mcp_servers_used": json.dumps((glean.get("transcript") or {}).get("mcp_servers_used", {}), sort_keys=True),
                "direct_mcp_servers_used": json.dumps((direct.get("transcript") or {}).get("mcp_servers_used", {}), sort_keys=True),
                "validity_flags": ";".join(flags),
                "valid": not flags,
                "winner": grade.get("winner", "") if isinstance(grade, dict) else "",
                "completeness_winner": grade.get("completeness_winner", "") if isinstance(grade, dict) else "",
                "groundedness_winner": grade.get("groundedness_winner", "") if isinstance(grade, dict) else "",
                "usefulness_winner": grade.get("usefulness_winner", "") if isinstance(grade, dict) else "",
                "completeness_glean": grade.get("completeness_glean", "") if isinstance(grade, dict) else "",
                "completeness_direct": grade.get("completeness_direct", "") if isinstance(grade, dict) else "",
                "groundedness_glean": grade.get("groundedness_glean", "") if isinstance(grade, dict) else "",
                "groundedness_direct": grade.get("groundedness_direct", "") if isinstance(grade, dict) else "",
                "judge_confidence": grade.get("confidence", "") if isinstance(grade, dict) else "",
                "judge_reasoning": grade.get("reasoning", "") if isinstance(grade, dict) else "",
            })
    return rows


def mean(nums: Iterable[float]) -> float:
    vals = [float(x) for x in nums]
    return sum(vals) / len(vals) if vals else 0.0


def bootstrap_savings_ci(pairs: List[Tuple[Any, Any]], n_boot: int = 2000, seed: int = 1234) -> Optional[Tuple[float, float]]:
    # Bootstrap a 95% CI for the ratio-of-means savings %:
    # (mean(direct) - mean(glean)) / mean(direct) * 100. Seeded for reproducibility.
    usable = [(float(g), float(d)) for g, d in pairs if d not in ("", None) and float(d) != 0.0]
    if len(usable) < 2:
        return None
    rnd = random.Random(seed)
    k = len(usable)
    ests = []
    for _ in range(n_boot):
        sample = [usable[rnd.randrange(k)] for _ in range(k)]
        mg = sum(g for g, _ in sample) / k
        md = sum(d for _, d in sample) / k
        if md:
            ests.append((md - mg) / md * 100.0)
    if not ests:
        return None
    ests.sort()
    return (round(ests[int(0.025 * (len(ests) - 1))], 1), round(ests[int(0.975 * (len(ests) - 1))], 1))


def command_report(args: argparse.Namespace) -> int:
    config_path, cfg = load_config(args.config)
    res = results_dir(config_path, cfg)
    res.mkdir(parents=True, exist_ok=True)
    rows = collect_aggregate_rows(res, cfg)
    csv_path = res / "aggregate_rows.csv"
    if rows:
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    else:
        write_text(csv_path, "")
    valid = [r for r in rows if r.get("valid")]
    denom_rows = valid or rows

    def col_mean(key: str) -> float:
        return mean(float(r[key]) for r in denom_rows if r.get(key) not in ("", None))

    def pct_lower(direct_val: float, glean_val: float) -> float:
        return ((direct_val - glean_val) / direct_val * 100.0) if direct_val else 0.0

    # Tokens: marginal = per-prompt work (input+output); fixed = per-session cache creation.
    g_marg_avg = col_mean("glean_marginal_tokens")
    d_marg_avg = col_mean("direct_marginal_tokens")
    g_fixed_avg = col_mean("glean_cache_write_tokens")
    d_fixed_avg = col_mean("direct_cache_write_tokens")
    gt_avg = col_mean("glean_total_tokens")
    dt_avg = col_mean("direct_total_tokens")
    # Cost: reported = host-emitted billed/per-run figure when available; list = normalized rate card.
    reported_cost_rows = [
        r for r in denom_rows
        if r.get("glean_reported_cost_usd") not in ("", None) and r.get("direct_reported_cost_usd") not in ("", None)
    ]
    reported_cost_available = bool(reported_cost_rows)
    g_rc_avg = col_mean("glean_reported_cost_usd")
    d_rc_avg = col_mean("direct_reported_cost_usd")
    gc_avg = col_mean("glean_cost_usd")
    dc_avg = col_mean("direct_cost_usd")
    # Latency: wall-clock milliseconds reported by the host adapter, when available.
    g_lat_avg = col_mean("glean_latency_ms")
    d_lat_avg = col_mean("direct_latency_ms")

    winner_counts = Counter(r.get("winner") or "ungraded" for r in denom_rows)
    comp_g = col_mean("completeness_glean")
    comp_d = col_mean("completeness_direct")
    gr_g = col_mean("groundedness_glean")
    gr_d = col_mean("groundedness_direct")

    marginal_savings = pct_lower(d_marg_avg, g_marg_avg)
    total_token_savings = pct_lower(dt_avg, gt_avg)
    reported_cost_savings = pct_lower(d_rc_avg, g_rc_avg) if reported_cost_available else None
    list_cost_savings = pct_lower(dc_avg, gc_avg)
    latency_savings = pct_lower(d_lat_avg, g_lat_avg)
    invalid_count = len(rows) - len(valid)

    def ci_str(ci: Optional[Tuple[float, float]]) -> str:
        if ci:
            return f" (95% CI {ci[0]:.1f} to {ci[1]:.1f}%, n={len(denom_rows)}, bootstrap)"
        return f" (n={len(denom_rows)}; need ≥2 rows for a CI)"

    reported_cost_ci = bootstrap_savings_ci([(r["glean_reported_cost_usd"], r["direct_reported_cost_usd"]) for r in reported_cost_rows]) if reported_cost_available else None
    marginal_ci = bootstrap_savings_ci([(r["glean_marginal_tokens"], r["direct_marginal_tokens"]) for r in denom_rows])
    host_name = str(cfg.get("host") or "configured host")
    host_label = host_name.replace("-", " ").title()
    reported_cost_row = (
        f"| Avg host-reported cost / task | ${g_rc_avg:,.4f} | ${d_rc_avg:,.4f} | {reported_cost_savings:.1f}% lower for Glean |"
        if reported_cost_available and reported_cost_savings is not None
        else "| Avg host-reported cost / task | N/A | N/A | N/A |"
    )
    reported_cost_note = (
        f"> Host-reported cost savings for Glean: **{reported_cost_savings:.1f}%**{ci_str(reported_cost_ci)}."
        if reported_cost_available and reported_cost_savings is not None
        else f"> Host-reported billed cost is unavailable for these `{host_name}` runs because the {host_label} adapter did not emit per-run cost. Use list-price-normalized cost for cost comparison."
    )
    token_guidance = (
        "Prefer marginal tokens + host-reported cost for headline claims."
        if reported_cost_available
        else "Prefer marginal tokens + list-price-normalized cost for headline claims because host-reported cost is unavailable."
    )
    md = f"""# Aggregate summary

Generated: {now_iso()}

Eval: `{cfg.get('eval_name', '')}`

## Dataset

- Paired rows: {len(rows)}
- Valid rows: {len(valid)}
- Invalid / flagged rows: {invalid_count}
- Summary basis: {'valid rows' if valid else 'all rows (no fully valid rows found)'}

## Cost

Host-reported cost is shown only when the selected run host emits a per-run billed-cost field. List-price-normalized cost applies the configurable `pricing_per_million` rates uniformly across both arms.

| Metric | Glean MCP | Direct MCP | Delta |
|---|---:|---:|---:|
{reported_cost_row}
| Avg list-price-normalized cost / task | ${gc_avg:,.4f} | ${dc_avg:,.4f} | {list_cost_savings:.1f}% lower for Glean |

> List-price-normalized cost is a rate-card comparison, not billed spend. Verify `pricing_per_million` against current model list prices; it can diverge from host-reported cost when cache-creation tokens dominate or when the host does not expose billed cost.
>
{reported_cost_note}

## Tokens

Marginal = per-prompt work (input + output). Fixed = per-session cache creation (schema/context loaded on each fresh session, largely identical across arms and so mostly cancelling in the delta). Cache-read tokens are in the per-row CSV.

| Metric | Glean MCP | Direct MCP | Delta |
|---|---:|---:|---:|
| Avg marginal tokens / task | {g_marg_avg:,.0f} | {d_marg_avg:,.0f} | {marginal_savings:.1f}% lower for Glean |
| Avg fixed (cache-creation) tokens / task | {g_fixed_avg:,.0f} | {d_fixed_avg:,.0f} | — |
| Avg total tokens / task | {gt_avg:,.0f} | {dt_avg:,.0f} | {total_token_savings:.1f}% lower for Glean |

> {token_guidance} Raw totals are dominated by per-session cache creation and can mislead.
>
> Marginal-token savings for Glean: **{marginal_savings:.1f}%**{ci_str(marginal_ci)}.

## Latency

| Metric | Glean MCP | Direct MCP | Delta |
|---|---:|---:|---:|
| Avg wall-clock / task | {g_lat_avg / 1000:,.1f}s | {d_lat_avg / 1000:,.1f}s | {latency_savings:.1f}% faster for Glean |

## Quality judge summary

| Metric | Glean MCP | Direct MCP |
|---|---:|---:|
| Avg completeness | {comp_g:.2f} | {comp_d:.2f} |
| Avg groundedness | {gr_g:.2f} | {gr_d:.2f} |

Winner counts: `{dict(winner_counts)}`

## Validity notes

Rows with any of these flags should be reviewed/excluded before executive claims:

- `glean_run_failed`
- `direct_run_failed`
- `glean_no_retrieval`
- `direct_no_retrieval`
- `model_mismatch`
- `glean_forbidden_server` / `direct_forbidden_server` (arm called a server on its `forbidden_mcp_servers` list)
- `glean_unexpected_server` / `direct_unexpected_server` (arm called a server outside `require_live_tool_servers`/`expected_mcp_servers` — cross-arm contamination)

Detailed rows: [`aggregate_rows.csv`](aggregate_rows.csv)
"""
    summary_path = res / "aggregate_summary.md"
    write_text(summary_path, md)
    print(json.dumps({
        "rows": len(rows),
        "valid_rows": len(valid),
        "aggregate_rows_csv": str(csv_path),
        "aggregate_summary_md": str(summary_path),
        "avg_marginal_token_savings_pct": round(marginal_savings, 2),
        "avg_total_token_savings_pct": round(total_token_savings, 2),
        "avg_reported_cost_savings_pct": round(reported_cost_savings, 2) if reported_cost_savings is not None else None,
        "avg_list_cost_savings_pct": round(list_cost_savings, 2),
        "avg_latency_savings_pct": round(latency_savings, 2),
        "reported_cost_savings_ci_pct": reported_cost_ci,
        "marginal_token_savings_ci_pct": marginal_ci,
    }, indent=2))
    return 0


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def command_package(args: argparse.Namespace) -> int:
    config_path, cfg = load_config(args.config)
    res = results_dir(config_path, cfg)
    if not res.exists():
        raise EvalError(f"Results dir not found: {res}")
    manifest_path = res / "submission_manifest.json"
    zip_path = res / "eval_submission.zip"
    files = []
    for p in sorted(res.rglob("*")):
        if not p.is_file():
            continue
        if p in (zip_path, manifest_path):
            continue
        rel = p.relative_to(res).as_posix()
        files.append({"path": rel, "sha256": sha256_file(p), "bytes": p.stat().st_size})
    manifest = {
        "created_at": now_iso(),
        "eval_name": cfg.get("eval_name"),
        "results_dir": str(res),
        "file_count": len(files),
        "files": files,
    }
    write_json(manifest_path, manifest)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for entry in files:
            p = res / entry["path"]
            zf.write(p, arcname=entry["path"])
        zf.write(manifest_path, arcname="submission_manifest.json")
    print(json.dumps({"manifest": str(manifest_path), "zip": str(zip_path), "file_count": len(files)}, indent=2))
    return 0


def safe_extract_zip(zip_path: Path, dest: Path) -> None:
    """Extract zip_path to dest, rejecting path traversal entries."""
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            target = (dest / member.filename).resolve()
            if not str(target).startswith(str(dest_resolved) + os.sep) and target != dest_resolved:
                raise EvalError(f"Unsafe zip entry path: {member.filename}")
        zf.extractall(dest)


def command_import(args: argparse.Namespace) -> int:
    config_path, cfg = load_config(args.config)
    res = results_dir(config_path, cfg)
    res.mkdir(parents=True, exist_ok=True)
    imported = []
    skipped = []
    for zip_arg in args.zips:
        zip_path = Path(zip_arg).expanduser().resolve()
        if not zip_path.exists():
            raise EvalError(f"Submission zip not found: {zip_path}")
        with tempfile.TemporaryDirectory(prefix="glean-mcp-eval-import-") as td:
            tmp = Path(td)
            safe_extract_zip(zip_path, tmp)
            top_dirs = [p for p in tmp.iterdir() if p.is_dir() and not p.name.startswith("_")]
            # Participant dirs contain at least one arm directory. Ignore docs/metadata-only dirs.
            participant_dirs_found = [
                p for p in top_dirs
                if (p / "glean").exists() or (p / "direct").exists() or (p / "grades").exists()
            ]
            if args.participant_id:
                participant_dirs_found = [p for p in participant_dirs_found if p.name == args.participant_id]
            if not participant_dirs_found:
                skipped.append({"zip": str(zip_path), "reason": "no participant result directories found"})
                continue
            for src in participant_dirs_found:
                dst = res / src.name
                if dst.exists() and not args.replace:
                    skipped.append({"zip": str(zip_path), "participant_id": src.name, "reason": "already exists; use --replace to overwrite"})
                    continue
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
                imported.append({"zip": str(zip_path), "participant_id": src.name, "destination": str(dst)})
    print(json.dumps({"imported": imported, "skipped": skipped, "results_dir": str(res)}, indent=2))
    return 0 if imported or not skipped else 1


def command_doctor(args: argparse.Namespace) -> int:
    config_path, cfg = load_config(args.config)
    root = repo_root_for_config(config_path)
    host = getattr(args, "host", None) or cfg.get("host") or "claude-code"
    adapter = get_adapter(host)
    data = {
        "created_at": now_iso(),
        "root": str(root),
        "host": host,
        "host_executable_present": adapter.executable_present(),
        "caps": adapter.caps,
        "prompts_count": len(load_prompts(config_path, cfg)),
        "results_dir": str(results_dir(config_path, cfg)),
    }
    data.update(adapter.doctor(root))
    print(json.dumps(data, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run and analyze Glean MCP A/B evaluations in Claude Code.")
    p.add_argument("--version", action="version", version="glean-mcp-eval 0.1.0")
    sub = p.add_subparsers(dest="command", required=True)

    def add_config(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--config", default=DEFAULT_CONFIG, help="Path to eval config JSON")

    def add_host(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--host", help="Host adapter: 'claude-code' (default) or 'cursor'; overrides config 'host'")

    sp = sub.add_parser("doctor", help="Inspect local config and host/MCP availability")
    add_config(sp)
    add_host(sp)
    sp.set_defaults(func=command_doctor)

    sp = sub.add_parser("preflight", help="Validate setup for one arm")
    add_config(sp)
    add_host(sp)
    sp.add_argument("--arm", required=True, help="Arm name from config, e.g. glean or direct")
    sp.add_argument("--live", action="store_true", help="Run a live headless preflight probe")
    sp.add_argument("--dry-run", action="store_true", help="Print the exact command without executing")
    sp.set_defaults(func=command_preflight)

    sp = sub.add_parser("run", help="Run all prompts for one participant/arm")
    add_config(sp)
    add_host(sp)
    sp.add_argument("--arm", required=True, help="Arm name from config, e.g. glean or direct")
    sp.add_argument("--participant-id", required=True, help="Stable anonymous participant ID")
    sp.add_argument("--dry-run", action="store_true", help="Print the exact commands without executing")
    sp.set_defaults(func=command_run)

    sp = sub.add_parser("grade", help="Judge paired Glean/direct answers")
    add_config(sp)
    sp.add_argument("--participant-id", help="Participant ID to grade; omit for all")
    sp.add_argument("--force", action="store_true", help="Regenerate existing grades")
    sp.set_defaults(func=command_grade)

    sp = sub.add_parser("report", help="Aggregate paired results into CSV and Markdown")
    add_config(sp)
    sp.set_defaults(func=command_report)

    sp = sub.add_parser("package", help="Create checksum manifest and submission zip")
    add_config(sp)
    sp.set_defaults(func=command_package)

    sp = sub.add_parser("import", help="Import participant submission zip(s) into this results directory")
    add_config(sp)
    sp.add_argument("zips", nargs="+", help="One or more eval_submission.zip files from participants")
    sp.add_argument("--participant-id", help="Only import this participant ID from each zip")
    sp.add_argument("--replace", action="store_true", help="Replace existing participant results with imported results")
    sp.set_defaults(func=command_import)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except EvalError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
