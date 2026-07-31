#!/usr/bin/env python3
"""Glean MCP A/B Eval Kit.

Dependency-free CLI for running a crossover evaluation of Glean MCP vs direct
vendor MCPs in Claude Code.
"""

from __future__ import annotations

import argparse
import copy
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

DEFAULT_DISALLOWED_BUILTINS = [
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "WebSearch",
    "WebFetch",
    "NotebookEdit",
]

PLACEHOLDER_PATTERNS = [
    "<your-glean-subdomain>",
    "<replace_with",
    "example.com",
    "todo",
    "changeme",
]

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


def sensitive_mcp_paths(value: Any, path: str = "") -> List[str]:
    """Return paths that look like they may contain credentials, never values."""
    found: List[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            key_lower = str(key).lower()
            if any(marker in key_lower for marker in ("authorization", "token", "secret", "password", "api_key", "apikey")):
                if child not in (None, "", [], {}):
                    found.append(child_path)
            found.extend(sensitive_mcp_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(sensitive_mcp_paths(child, f"{path}[{index}]"))
    return found


def command_setup_direct(args: argparse.Namespace) -> int:
    """Materialize only the selected Claude Code MCP servers for the direct arm."""
    config_path, cfg = load_config(args.config)
    root = repo_root_for_config(config_path)
    acfg = arm_config(cfg, "direct")

    requested_raw = args.servers or acfg.get("expected_mcp_servers") or []
    if isinstance(requested_raw, str):
        requested = [item.strip() for item in requested_raw.split(",") if item.strip()]
    else:
        requested = [str(item).strip() for item in requested_raw if str(item).strip()]
    if not requested:
        raise EvalError(
            "No direct servers selected. Add arms.direct.expected_mcp_servers to the eval config "
            "or pass --servers slack,atlassian,..."
        )

    source = Path(args.source).expanduser().resolve()
    if not source.exists():
        raise EvalError(f"Claude Code MCP source config not found: {source}")
    source_data = read_json(source)
    source_servers = source_data.get("mcpServers") if isinstance(source_data, dict) else None
    if not isinstance(source_servers, dict):
        raise EvalError(
            f"No top-level mcpServers object found in {source}. "
            "Use Claude Code's user config (~/.claude.json), not Claude Desktop's "
            "new Connectors state file."
        )

    by_normalized = {normalize_server_name(name): name for name in source_servers}
    missing = [name for name in requested if normalize_server_name(name) not in by_normalized]
    if missing:
        available = sorted(str(name) for name in source_servers)
        raise EvalError(
            f"Direct MCP servers are not configured in {source}: {', '.join(missing)}. "
            f"Available servers: {', '.join(available) if available else '(none)'}. "
            "Authenticate/add the missing servers with Claude Code, then rerun setup-direct."
        )

    selected = {}
    for requested_name in requested:
        actual_name = by_normalized[normalize_server_name(requested_name)]
        selected[actual_name] = copy.deepcopy(source_servers[actual_name])

    output = Path(args.output).expanduser()
    if not output.is_absolute():
        output = root / output
    payload = {"mcpServers": selected}
    if args.dry_run:
        print(json.dumps({
            "source": str(source),
            "output": str(output.resolve()),
            "servers": list(selected),
            "sensitive_fields_copied": sensitive_mcp_paths(payload),
            "dry_run": True,
        }, indent=2))
        return 0

    write_json(output, payload)
    print(json.dumps({
        "source": str(source),
        "output": str(output.resolve()),
        "servers": list(selected),
        "sensitive_fields_copied": sensitive_mcp_paths(payload),
        "warning": "Keep the generated MCP config local; it may contain auth headers or client secrets.",
    }, indent=2))
    return 0


def repo_root_for_config(config_path: Path) -> Path:
    return config_path.parent


def load_server_profile(path: str, profile_name: str) -> Dict[str, Any]:
    profile_path = Path(path).expanduser().resolve()
    if not profile_path.exists():
        raise EvalError(f"Server profile file not found: {profile_path}")
    data = read_json(profile_path)
    profiles = data.get("profiles") if isinstance(data, dict) else None
    profile = profiles.get(profile_name) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict):
        available = sorted(profiles) if isinstance(profiles, dict) else []
        raise EvalError(
            f"Server profile {profile_name!r} not found in {profile_path}. "
            f"Available profiles: {', '.join(available) or '(none)'}"
        )
    servers = profile.get("direct_servers")
    if not isinstance(servers, list) or not all(isinstance(s, str) and s.strip() for s in servers):
        raise EvalError(f"Profile {profile_name!r} must define a non-empty direct_servers list")
    return profile


def command_setup(args: argparse.Namespace) -> int:
    """Create local config files and apply a shareable direct-server profile."""
    config_path = Path(args.config).expanduser().resolve()
    root = config_path.parent
    examples = {
        config_path: root / "config" / "eval.config.strict.example.json",
        root / "golden_prompts.tsv": root / "prompts" / "golden_prompts.example.tsv",
        root / "mcp" / "glean.mcp.json": root / "config" / "mcp.glean.example.json",
        root / "mcp" / "direct.mcp.json": root / "config" / "mcp.direct.example.json",
    }
    for destination, source in examples.items():
        if destination.exists() and not args.force:
            continue
        if not source.exists():
            raise EvalError(f"Example file not found: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    cfg = read_json(config_path)
    profile = load_server_profile(args.profile_file, args.profile)
    direct = arm_config(cfg, "direct")
    direct["label"] = profile.get("label") or f"{args.profile} (direct)"
    direct["expected_mcp_servers"] = list(profile["direct_servers"])
    direct["preflight_prompt"] = profile.get(
        "preflight_prompt",
        "Use each direct MCP server for one harmless read-only lookup or search and confirm which servers retrieved data.",
    )
    write_json(config_path, cfg)
    print(json.dumps({
        "config": str(config_path),
        "profile": args.profile,
        "direct_servers": direct["expected_mcp_servers"],
        "created_or_preserved": [str(p) for p in examples],
        "next": [
            f"claude mcp list",
            f"python3 scripts/glean_mcp_eval.py setup-direct --config {config_path.name}",
            f"python3 scripts/glean_mcp_eval.py doctor --config {config_path.name}",
        ],
    }, indent=2))
    return 0


def results_dir(config_path: Path, cfg: Dict[str, Any]) -> Path:
    root = repo_root_for_config(config_path)
    return (root / cfg.get("results_dir", "results")).resolve()


def prompt_file_path(config_path: Path, cfg: Dict[str, Any]) -> Path:
    root = repo_root_for_config(config_path)
    prompt_file = Path(cfg.get("prompts_file", "golden_prompts.tsv"))
    if not prompt_file.is_absolute():
        prompt_file = root / prompt_file
    return prompt_file


def load_prompts(config_path: Path, cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    prompt_file = prompt_file_path(config_path, cfg)
    if not prompt_file.exists():
        raise EvalError(f"Prompts file not found: {prompt_file}")
    rows: List[Dict[str, str]] = []
    errors: List[str] = []
    with prompt_file.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        try:
            header = next(reader)
        except StopIteration:
            raise EvalError(f"Prompt TSV is empty: {prompt_file}")
        required = {"ID", "Prompt"}
        if not required.issubset(set(header)):
            raise EvalError(f"Prompt TSV must include columns {sorted(required)}; found {header}")
        for line_no, raw in enumerate(reader, 2):
            if not raw or all(not (c or "").strip() for c in raw):
                continue
            if len(raw) != len(header):
                errors.append(
                    f"line {line_no}: expected {len(header)} tab-separated columns ({header}), found {len(raw)}: {raw}"
                )
                continue
            row = {k: (v or "") for k, v in zip(header, raw)}
            if not row.get("ID", "").strip() or not row.get("Prompt", "").strip():
                errors.append(f"line {line_no}: ID and Prompt must be non-empty")
                continue
            rows.append(row)
    if errors:
        preview = "\n".join(errors[:10])
        more = f"\n... and {len(errors) - 10} more prompt TSV errors" if len(errors) > 10 else ""
        raise EvalError(
            f"Prompt file validation failed: {prompt_file}\n{preview}{more}\n\n"
            "Expected format: ID<TAB>Dept<TAB>Prompt (additional columns are okay if every row has the same column count)."
        )
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



def mcp_mode_for_arm(cfg: Dict[str, Any], acfg: Dict[str, Any]) -> str:
    explicit = str(cfg.get("mcp_mode", "")).strip().lower()
    if explicit in {"strict", "ambient"}:
        return explicit
    return "strict" if acfg.get("mcp_config") else "ambient"


def find_placeholder_values(obj: Any, path: str = "$") -> List[Dict[str, str]]:
    findings: List[Dict[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            findings.extend(find_placeholder_values(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            findings.extend(find_placeholder_values(v, f"{path}[{i}]"))
    elif isinstance(obj, str):
        low = obj.lower()
        for pat in PLACEHOLDER_PATTERNS:
            if pat in low:
                findings.append({"path": path, "pattern": pat, "value": obj})
    return findings


def inspect_mcp_config(path: Path, expected_servers: List[str]) -> Dict[str, Any]:
    diagnostics: Dict[str, Any] = {"path": str(path), "errors": [], "warnings": [], "suggestions": []}
    if not path.exists():
        diagnostics["errors"].append("mcp_config file not found")
        return diagnostics
    try:
        data = read_json(path)
    except Exception as e:  # noqa: BLE001
        diagnostics["errors"].append(f"could not parse JSON: {e}")
        return diagnostics
    placeholders = find_placeholder_values(data)
    for ph in placeholders:
        diagnostics["errors"].append(f"placeholder value found at {ph['path']}: {ph['value']}")
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        diagnostics["errors"].append("missing top-level mcpServers object")
        return diagnostics
    normalized_to_name = {normalize_server_name(k): k for k in servers.keys()}
    for exp in expected_servers:
        if exp not in normalized_to_name:
            diagnostics["errors"].append(f"expected server {exp!r} is not present in mcp_config")
    for norm, original in normalized_to_name.items():
        scfg = servers.get(original) or {}
        url = str(scfg.get("url") or "") if isinstance(scfg, dict) else ""
        headers = scfg.get("headers") if isinstance(scfg, dict) else None
        if "glean.com/mcp" in url and isinstance(headers, dict) and "Authorization" in headers:
            diagnostics["warnings"].append(
                f"server {original!r} uses an inline Authorization header; avoid committing MCP tokens/secrets"
            )
        if "glean.com/mcp" in url and not isinstance(headers, dict):
            diagnostics["warnings"].append(
                f"server {original!r} is a Glean MCP endpoint with no headers; some enterprise environments require environment-specific headers"
            )
            diagnostics["suggestions"].append(
                f"If live preflight exposes no tools, run `claude mcp get {original}` and mirror any required URL/type/headers into this strict MCP config."
            )
    return diagnostics


def dedupe_preserve(items: Iterable[Any]) -> List[Any]:
    out = []
    seen = set()
    for item in items:
        key = str(item)
        if key in seen:
            continue
        out.append(item)
        seen.add(key)
    return out


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
    mode = mcp_mode_for_arm(cfg, acfg)
    mcp_config_path = resolve_mcp_config(root, acfg, cfg)
    expected = [normalize_server_name(x) for x in acfg.get("expected_mcp_servers", [])]
    forbidden = [normalize_server_name(x) for x in acfg.get("forbidden_mcp_servers", [])]
    strict_diagnostics: Dict[str, Any] = {"errors": [], "warnings": [], "suggestions": []}
    if mode == "strict" and mcp_config_path is None:
        inventory = {"servers": [], "files": [], "errors": [{"path": "", "error": "strict MCP mode requires arm.mcp_config"}]}
        mcp_list = {"available": None, "strict_mode": True, "mcp_config": None, "servers_hint": [], "raw": None}
        strict_diagnostics["errors"].append("strict MCP mode requires this arm to set mcp_config")
    elif mcp_config_path is not None:
        # Strict per-arm isolation: at runtime the arm uses --strict-mcp-config,
        # so the only servers that exist are those in this file. Validate against
        # the file, not the ambient config (which would include the other arm's).
        inventory = inventory_from_file(mcp_config_path)
        strict_diagnostics = inspect_mcp_config(mcp_config_path, expected)
        mcp_list = {"available": None, "strict_mode": True, "mcp_config": str(mcp_config_path), "servers_hint": [], "raw": None}
    else:
        inventory = static_mcp_inventory(root)
        mcp_list = claude_mcp_list(root)
    missing = [s for s in expected if not server_present(s, inventory, mcp_list)]
    forbidden_found = [s for s in forbidden if server_present(s, inventory, mcp_list)]
    return {
        "arm": arm,
        "mcp_mode": mode,
        "expected_mcp_servers": expected,
        "forbidden_mcp_servers": forbidden,
        "configured_inventory": inventory,
        "claude_mcp_list": mcp_list,
        "strict_config_diagnostics": strict_diagnostics,
        "missing_expected": missing,
        "forbidden_found": forbidden_found,
        "static_pass": not missing and not forbidden_found and not strict_diagnostics.get("errors"),
    }

def resolve_mcp_config(root: Path, acfg: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> Optional[Path]:
    if cfg is not None and str(cfg.get("mcp_mode", "")).strip().lower() == "ambient" and not acfg.get("_force_mcp_config"):
        return None
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
    mcp_config_path = resolve_mcp_config(root, acfg, cfg)
    if mcp_config_path is not None:
        # Load ONLY this arm's servers and ignore any ambient/global MCP config,
        # so the arms are provably isolated regardless of what else is installed.
        cmd.extend(["--mcp-config", str(mcp_config_path), "--strict-mcp-config"])
    allowed_tools = acfg.get("allowed_tools") or []
    if allowed_tools:
        # Claude Code accepts a comma-separated allow list in current releases.
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])
    disallowed_tools = list(acfg.get("disallowed_tools") or [])
    if cfg.get("default_disallow_builtin_tools", True):
        disallowed_tools.extend(DEFAULT_DISALLOWED_BUILTINS)
    disallowed_tools = dedupe_preserve(disallowed_tools)
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
        missing_live_required = sorted(required - observed)
        # For live preflight, require successful run and tool use from every required/expected server.
        live_pass = bool(live_record.get("success")) and (not required or not missing_live_required)
    else:
        missing_live_required = []
    overall_pass = bool(static.get("static_pass")) and (live_pass is not False)
    record = {
        "created_at": now_iso(),
        "eval_name": cfg.get("eval_name"),
        "host": host,
        "arm": args.arm,
        "static": static,
        "live_enabled": bool(args.live),
        "live_pass": live_pass,
        "live_missing_required_servers": missing_live_required,
        "live_record": live_record,
        "pass": overall_pass,
    }
    summary = {
        "pass": overall_pass,
        "mcp_mode": static.get("mcp_mode"),
        "mcp_source": (static.get("claude_mcp_list") or {}).get("mcp_config") or "ambient claude mcp list",
        "missing_expected": static.get("missing_expected"),
        "forbidden_found": static.get("forbidden_found"),
        "strict_config_errors": (static.get("strict_config_diagnostics") or {}).get("errors", []),
        "strict_config_warnings": (static.get("strict_config_diagnostics") or {}).get("warnings", []),
        "live_pass": live_pass,
        "live_missing_required_servers": missing_live_required,
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, "arm": args.arm, **summary}, indent=2))
        return 0
    write_json(out / "preflight.json", record)
    latest = results_dir(config_path, cfg) / "_preflight" / args.arm / "latest.json"
    write_json(latest, record)
    print(json.dumps({"preflight_path": str(out / "preflight.json"), **summary}, indent=2))
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



def latest_preflight_path(config_path: Path, cfg: Dict[str, Any], arm: str) -> Path:
    return results_dir(config_path, cfg) / "_preflight" / arm / "latest.json"


def require_passing_preflight(config_path: Path, cfg: Dict[str, Any], arm: str) -> None:
    if not cfg.get("require_preflight_before_run", True):
        return
    p = latest_preflight_path(config_path, cfg, arm)
    if not p.exists():
        raise EvalError(
            f"Cannot run arm {arm!r}: no latest preflight found at {p}. "
            f"Run: python3 scripts/glean_mcp_eval.py preflight --config {config_path.name} --arm {arm} --live "
            "or rerun with --force."
        )
    rec = read_json(p)
    if rec.get("pass") is not True:
        static = rec.get("static") or {}
        reasons = []
        if static.get("missing_expected"):
            reasons.append(f"missing expected MCP servers: {static.get('missing_expected')}")
        if static.get("forbidden_found"):
            reasons.append(f"forbidden MCP servers present: {static.get('forbidden_found')}")
        diag = static.get("strict_config_diagnostics") or {}
        if diag.get("errors"):
            reasons.extend(diag.get("errors"))
        if rec.get("live_pass") is False:
            reasons.append("live preflight failed")
        if rec.get("live_missing_required_servers"):
            reasons.append(f"live preflight did not use required servers: {rec.get('live_missing_required_servers')}")
        reason_text = "; ".join(reasons) if reasons else "preflight pass=false"
        raise EvalError(
            f"Cannot run arm {arm!r}: latest preflight failed ({reason_text}). "
            "Fix setup and rerun preflight, or rerun run with --force."
        )
    if cfg.get("require_live_preflight_before_run", True) and not rec.get("live_enabled"):
        raise EvalError(
            f"Cannot run arm {arm!r}: latest preflight did not include --live. "
            "Rerun preflight with --live, or rerun run with --force."
        )


def command_run(args: argparse.Namespace) -> int:
    config_path, cfg = load_config(args.config)
    root = repo_root_for_config(config_path)
    acfg = arm_config(cfg, args.arm)
    host = getattr(args, "host", None) or cfg.get("host") or "claude-code"
    prompts = load_prompts(config_path, cfg)
    prompt_ids = set(getattr(args, "prompt_ids", []) or [])
    if prompt_ids:
        unknown = sorted(prompt_ids - {safe_prompt_id(row["ID"]) for row in prompts})
        if unknown:
            raise EvalError(f"Unknown prompt IDs: {', '.join(unknown)}")
        prompts = [row for row in prompts if safe_prompt_id(row["ID"]) in prompt_ids]
    if not prompts:
        raise EvalError("No prompts selected")
    if not args.force and not args.dry_run:
        require_passing_preflight(config_path, cfg, args.arm)
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
        run_dir = out_root / pid
        existing_run = run_dir / "run.json"
        if not args.dry_run and not getattr(args, "rerun_existing", False) and existing_run.exists():
            existing = read_json(existing_run)
            if existing.get("success") is True:
                manifest["runs"].append({"id": row.get("ID"), "dir": str(run_dir), "success": True, "skipped": True})
                print(f"[{i}/{len(prompts)}] {args.arm} {pid}: skipped existing success", flush=True)
                continue
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
    executed = sum(1 for run in manifest["runs"] if not run.get("skipped"))
    print(json.dumps({"participant_id": args.participant_id, "arm": args.arm, "runs": len(prompts), "executed": executed, "failures": failures, "results_dir": str(out_root)}, indent=2))
    return 0 if failures == 0 else 1


def command_smoke_test(args: argparse.Namespace) -> int:
    """Run a cheap three-prompt validation for both arms."""
    config_path, cfg = load_config(args.config)
    prompts = load_prompts(config_path, cfg)
    prompt_ids = [safe_prompt_id(row["ID"]) for row in prompts[:args.prompt_count]]
    if len(prompt_ids) < args.prompt_count:
        raise EvalError(f"Only {len(prompt_ids)} prompts are available; cannot run a {args.prompt_count}-prompt smoke test")
    arms = [args.arm] if args.arm != "both" else ["glean", "direct"]
    for arm in arms:
        preflight_args = argparse.Namespace(config=args.config, host=args.host, arm=arm, live=True, dry_run=args.dry_run)
        rc = command_preflight(preflight_args)
        if rc != 0:
            return rc
    for arm in arms:
        run_args = argparse.Namespace(
            config=args.config, host=args.host, arm=arm, participant_id=args.participant_id,
            dry_run=args.dry_run, force=args.force, prompt_ids=prompt_ids, rerun_existing=args.rerun_existing,
        )
        rc = command_run(run_args)
        if rc != 0:
            return rc
    print(json.dumps({"smoke_test": True, "participant_id": args.participant_id, "prompt_ids": prompt_ids, "arms": arms}, indent=2))
    return 0


def command_run_cli(args: argparse.Namespace) -> int:
    args.prompt_ids = [safe_prompt_id(x.strip()) for x in args.prompt_ids.split(",") if x.strip()] if args.prompt_ids else []
    return command_run(args)


def command_run_all(args: argparse.Namespace) -> int:
    """Run both arms, then grade, report, and optionally package."""
    config_path, cfg = load_config(args.config)
    arms = list((cfg.get("arms") or {}).keys())
    if not {"glean", "direct"}.issubset(arms):
        raise EvalError("run-all requires both glean and direct arms in the config")
    if args.dry_run:
        print(json.dumps({
            "dry_run": True,
            "participant_id": args.participant_id,
            "steps": ["preflight glean", "preflight direct", "run glean", "run direct", "grade", "report", "package" if not args.no_package else "skip package"],
        }, indent=2))
        return 0
    if args.smoke:
        smoke_args = argparse.Namespace(
            config=args.config, host=args.host, arm="both", participant_id=args.participant_id,
            prompt_count=3, dry_run=args.dry_run, force=args.force, rerun_existing=args.rerun_existing,
        )
        return command_smoke_test(smoke_args)
    for arm in ("glean", "direct"):
        run_args = argparse.Namespace(
            config=args.config, host=args.host, arm=arm, participant_id=args.participant_id,
            dry_run=args.dry_run, force=args.force, prompt_ids=[], rerun_existing=args.rerun_existing,
        )
        rc = command_run(run_args)
        if rc != 0:
            return rc
    grade_args = argparse.Namespace(config=args.config, participant_id=args.participant_id, force=args.force)
    rc = command_grade(grade_args)
    if rc != 0:
        return rc
    rc = command_report(argparse.Namespace(config=args.config))
    if rc != 0:
        return rc
    if not args.no_package:
        rc = command_package(argparse.Namespace(config=args.config))
        if rc != 0:
            return rc
    print(json.dumps({"run_all": True, "participant_id": args.participant_id, "packaged": not args.no_package}, indent=2))
    return 0


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
        "_force_mcp_config": True,
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
            blind_grade = raw.get("structured_output")
            if not blind_grade and isinstance(raw.get("result"), str):
                try:
                    blind_grade = json.loads(raw["result"])
                except Exception:
                    blind_grade = None
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


def validity_flags(glean: Dict[str, Any], direct: Dict[str, Any]) -> List[str]:
    flags = []
    if not glean.get("success"):
        flags.append("glean_run_failed")
    if not direct.get("success"):
        flags.append("direct_run_failed")
    if not (glean.get("transcript") or {}).get("mcp_servers_used"):
        flags.append("glean_no_mcp_retrieval")
    if not (direct.get("transcript") or {}).get("mcp_servers_used"):
        flags.append("direct_no_mcp_retrieval")
    gm = set((glean.get("transcript") or {}).get("models", {}).keys())
    dm = set((direct.get("transcript") or {}).get("models", {}).keys())
    if gm and dm and gm != dm:
        flags.append("model_mismatch")
    return flags


def collect_aggregate_rows(res: Path) -> List[Dict[str, Any]]:
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
            flags = validity_flags(glean, direct)
            gt = int(glean.get("total_tokens") or 0)
            dtok = int(direct.get("total_tokens") or 0)
            gcost = float(glean.get("computed_cost_usd") or 0.0)
            dcost = float(direct.get("computed_cost_usd") or 0.0)
            g_usage = glean.get("usage") or {}
            d_usage = direct.get("usage") or {}
            g_marginal = int(g_usage.get("input_tokens", 0)) + int(g_usage.get("output_tokens", 0))
            d_marginal = int(d_usage.get("input_tokens", 0)) + int(d_usage.get("output_tokens", 0))
            g_rcost = float(glean.get("total_cost_usd_reported_by_claude") or 0.0)
            d_rcost = float(direct.get("total_cost_usd_reported_by_claude") or 0.0)
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
                "glean_reported_cost_usd": round(g_rcost, 6),
                "direct_reported_cost_usd": round(d_rcost, 6),
                "reported_cost_savings_pct": round((d_rcost - g_rcost) / d_rcost * 100.0, 2) if d_rcost else "",
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


def format_delta(savings_pct: float, *, positive_word: str = "lower", negative_word: str = "higher") -> str:
    if savings_pct >= 0:
        return f"{savings_pct:.1f}% {positive_word} for Glean"
    return f"{abs(savings_pct):.1f}% {negative_word} for Glean"


def preflight_report_lines(config_path: Path, cfg: Dict[str, Any]) -> Tuple[List[str], bool]:
    lines = []
    all_pass = True
    for arm in sorted((cfg.get("arms") or {}).keys()):
        p = latest_preflight_path(config_path, cfg, arm)
        if not p.exists():
            all_pass = False
            lines.append(f"❌ {arm}: no latest preflight found")
            continue
        try:
            rec = read_json(p)
        except Exception as e:  # noqa: BLE001
            all_pass = False
            lines.append(f"❌ {arm}: could not read latest preflight ({e})")
            continue
        static = rec.get("static") or {}
        mode = static.get("mcp_mode") or "unknown"
        source = ((static.get("claude_mcp_list") or {}).get("mcp_config") or "ambient claude mcp list")
        if rec.get("pass") is True:
            lines.append(f"✅ {arm}: preflight passed ({mode}; {source})")
        else:
            all_pass = False
            bits = []
            if static.get("missing_expected"):
                bits.append(f"missing expected {static.get('missing_expected')}")
            if static.get("forbidden_found"):
                bits.append(f"forbidden found {static.get('forbidden_found')}")
            if rec.get("live_pass") is False:
                bits.append("live failed")
            if rec.get("live_missing_required_servers"):
                bits.append(f"live missing {rec.get('live_missing_required_servers')}")
            diag = static.get("strict_config_diagnostics") or {}
            if diag.get("errors"):
                bits.append(f"strict config errors {diag.get('errors')}")
            suffix = "; ".join(bits) if bits else "pass=false"
            lines.append(f"❌ {arm}: preflight failed ({mode}; {suffix})")
    return lines, all_pass


def command_report(args: argparse.Namespace) -> int:
    config_path, cfg = load_config(args.config)
    res = results_dir(config_path, cfg)
    res.mkdir(parents=True, exist_ok=True)
    rows = collect_aggregate_rows(res)
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

    g_marg_avg = col_mean("glean_marginal_tokens")
    d_marg_avg = col_mean("direct_marginal_tokens")
    g_fixed_avg = col_mean("glean_cache_write_tokens")
    d_fixed_avg = col_mean("direct_cache_write_tokens")
    gt_avg = col_mean("glean_total_tokens")
    dt_avg = col_mean("direct_total_tokens")
    g_rc_avg = col_mean("glean_reported_cost_usd")
    d_rc_avg = col_mean("direct_reported_cost_usd")
    gc_avg = col_mean("glean_cost_usd")
    dc_avg = col_mean("direct_cost_usd")
    g_lat_avg = col_mean("glean_latency_ms")
    d_lat_avg = col_mean("direct_latency_ms")

    winner_counts = Counter(r.get("winner") or "ungraded" for r in denom_rows)
    quality_ran = bool(denom_rows) and any((r.get("winner") or "") not in ("", "ungraded") for r in denom_rows)
    comp_g = col_mean("completeness_glean") if quality_ran else 0.0
    comp_d = col_mean("completeness_direct") if quality_ran else 0.0
    gr_g = col_mean("groundedness_glean") if quality_ran else 0.0
    gr_d = col_mean("groundedness_direct") if quality_ran else 0.0

    marginal_savings = pct_lower(d_marg_avg, g_marg_avg)
    total_token_savings = pct_lower(dt_avg, gt_avg)
    reported_cost_savings = pct_lower(d_rc_avg, g_rc_avg)
    list_cost_savings = pct_lower(dc_avg, gc_avg)
    latency_savings = pct_lower(d_lat_avg, g_lat_avg)
    invalid_count = len(rows) - len(valid)

    def ci_str(ci: Optional[Tuple[float, float]]) -> str:
        if ci:
            return f" (95% CI {ci[0]:.1f} to {ci[1]:.1f}%, n={len(denom_rows)}, bootstrap)"
        return f" (n={len(denom_rows)}; need ≥2 rows for a CI)"

    reported_cost_ci = bootstrap_savings_ci([(r["glean_reported_cost_usd"], r["direct_reported_cost_usd"]) for r in denom_rows])
    marginal_ci = bootstrap_savings_ci([(r["glean_marginal_tokens"], r["direct_marginal_tokens"]) for r in denom_rows])
    preflight_lines, preflights_pass = preflight_report_lines(config_path, cfg)
    invalid_run = (not rows) or invalid_count > 0 or not preflights_pass
    validity_status = "FAIL — do not use headline metrics" if invalid_run else "PASS"
    if not invalid_run and not quality_ran:
        validity_status = "PASS with warning — quality grading not run"

    mcp_usage_lines = ["| Prompt | Glean MCP usage | Direct MCP usage | Valid |", "|---|---|---|---|"]
    for r in rows:
        mcp_usage_lines.append(
            f"| {r.get('query_id')} | `{r.get('glean_mcp_servers_used')}` | `{r.get('direct_mcp_servers_used')}` | {'✅' if r.get('valid') else '❌ ' + str(r.get('validity_flags'))} |"
        )
    mcp_usage_md = "\n".join(mcp_usage_lines) if rows else "No paired rows found."

    quality_md = (
        "## Quality judge summary\n\n"
        "Quality judging: **NOT RUN**\n\n"
        "Run:\n\n"
        "```bash\n"
        "python3 scripts/glean_mcp_eval.py grade --config eval.config.json\n"
        "python3 scripts/glean_mcp_eval.py report --config eval.config.json\n"
        "```\n"
    )
    if quality_ran:
        quality_md = f"""## Quality judge summary

| Metric | Glean MCP | Direct MCP |
|---|---:|---:|
| Avg completeness | {comp_g:.2f} | {comp_d:.2f} |
| Avg groundedness | {gr_g:.2f} | {gr_d:.2f} |

Winner counts: `{dict(winner_counts)}`
"""

    warning_md = ""
    if invalid_run:
        warning_md = (
            "\n> ⚠️ Headline metrics should not be used for executive/customer claims until validity issues are fixed. "
            "The tables below are included for debugging only.\n"
        )

    md = f"""# Aggregate summary

Generated: {now_iso()}

Eval: `{cfg.get('eval_name', '')}`

## Run validity

**{validity_status}**

""" + "\n".join(f"- {line}" for line in preflight_lines) + f"""
- {'✅' if rows else '❌'} Paired rows found: {len(rows)}
- {'✅' if invalid_count == 0 and rows else '❌'} Invalid / flagged rows: {invalid_count}
- {'✅' if quality_ran else '⚠️'} Quality grading: {'run' if quality_ran else 'not run'}
{warning_md}
## Dataset

- Paired rows: {len(rows)}
- Valid rows: {len(valid)}
- Invalid / flagged rows: {invalid_count}
- Summary basis: {'valid rows' if valid else 'all rows (no fully valid rows found)'}

## MCP usage by row

{mcp_usage_md}

## Cost

Primary metric is the cost Claude Code reports per run. List-price-normalized cost applies the configurable `pricing_per_million` rates uniformly across both arms.

| Metric | Glean MCP | Direct MCP | Delta |
|---|---:|---:|---:|
| Avg reported cost / task | ${g_rc_avg:,.4f} | ${d_rc_avg:,.4f} | {format_delta(reported_cost_savings)} |
| Avg list-price-normalized cost / task | ${gc_avg:,.4f} | ${dc_avg:,.4f} | {format_delta(list_cost_savings)} |

> List-price-normalized cost is a rate-card comparison, not billed spend. Verify `pricing_per_million` against current model list prices; it can diverge sharply from reported cost when cache-creation tokens dominate.
>
> Reported-cost savings for Glean: **{reported_cost_savings:.1f}%**{ci_str(reported_cost_ci)}.

## Tokens

Marginal = per-prompt work (input + output). Fixed = per-session cache creation (schema/context loaded on each fresh session, largely identical across arms and so mostly cancelling in the delta). Cache-read tokens are in the per-row CSV.

| Metric | Glean MCP | Direct MCP | Delta |
|---|---:|---:|---:|
| Avg marginal tokens / task | {g_marg_avg:,.0f} | {d_marg_avg:,.0f} | {format_delta(marginal_savings)} |
| Avg fixed (cache-creation) tokens / task | {g_fixed_avg:,.0f} | {d_fixed_avg:,.0f} | — |
| Avg total tokens / task | {gt_avg:,.0f} | {dt_avg:,.0f} | {format_delta(total_token_savings)} |

> Prefer marginal tokens + reported cost for headline claims. Raw totals are dominated by per-session cache creation and can mislead.
>
> Marginal-token savings for Glean: **{marginal_savings:.1f}%**{ci_str(marginal_ci)}.

## Latency

| Metric | Glean MCP | Direct MCP | Delta |
|---|---:|---:|---:|
| Avg wall-clock / task | {g_lat_avg / 1000:,.1f}s | {d_lat_avg / 1000:,.1f}s | {format_delta(latency_savings, positive_word='faster', negative_word='slower')} |

{quality_md}
## Validity notes

Rows with any of these flags should be reviewed/excluded before executive claims:

- `glean_run_failed`
- `direct_run_failed`
- `glean_no_mcp_retrieval`
- `direct_no_mcp_retrieval`
- `model_mismatch`

Detailed rows: [`aggregate_rows.csv`](aggregate_rows.csv)
"""
    summary_path = res / "aggregate_summary.md"
    write_text(summary_path, md)
    print(json.dumps({
        "rows": len(rows),
        "valid_rows": len(valid),
        "aggregate_rows_csv": str(csv_path),
        "aggregate_summary_md": str(summary_path),
        "run_validity": validity_status,
        "avg_marginal_token_savings_pct": round(marginal_savings, 2),
        "avg_total_token_savings_pct": round(total_token_savings, 2),
        "avg_reported_cost_savings_pct": round(reported_cost_savings, 2),
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
        if p == zip_path:
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
        "mcp_mode_by_arm": {arm: mcp_mode_for_arm(cfg, acfg) for arm, acfg in (cfg.get("arms") or {}).items()},
        "arm_static_checks": {arm: validate_static_setup(root, cfg, arm) for arm in (cfg.get("arms") or {}).keys()},
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

    sp = sub.add_parser("setup", help="Create local eval files and apply a direct-server profile")
    add_config(sp)
    sp.add_argument("--profile", default="current-reference", help="Server profile name")
    sp.add_argument("--profile-file", default="config/server-profiles.example.json", help="Server profile JSON")
    sp.add_argument("--force", action="store_true", help="Overwrite generated local files")
    sp.set_defaults(func=command_setup)

    sp = sub.add_parser("setup-direct", help="Copy selected Claude Code MCP definitions into the strict direct-arm config")
    add_config(sp)
    sp.add_argument(
        "--source",
        default=str(Path.home() / ".claude.json"),
        help="Claude Code MCP config to read (default: ~/.claude.json)",
    )
    sp.add_argument(
        "--servers",
        help="Comma-separated direct server names; defaults to arms.direct.expected_mcp_servers",
    )
    sp.add_argument(
        "--output",
        default="mcp/direct.mcp.json",
        help="Generated strict MCP config path, relative to the eval config directory",
    )
    sp.add_argument("--dry-run", action="store_true", help="Show the selected servers without writing the output")
    sp.set_defaults(func=command_setup_direct)

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

    sp = sub.add_parser("run", help="Run selected or all prompts for one participant/arm")
    add_config(sp)
    add_host(sp)
    sp.add_argument("--arm", required=True, help="Arm name from config, e.g. glean or direct")
    sp.add_argument("--participant-id", required=True, help="Stable anonymous participant ID")
    sp.add_argument("--prompt-ids", help="Comma-separated prompt IDs to run")
    sp.add_argument("--dry-run", action="store_true", help="Print the exact host commands without executing")
    sp.add_argument("--force", action="store_true", help="Run even if latest live preflight is missing or failed")
    sp.add_argument("--rerun-existing", action="store_true", help="Rerun prompts with an existing successful run")
    sp.set_defaults(func=command_run_cli)

    sp = sub.add_parser("smoke-test", help="Preflight both arms and run a small prompt subset")
    add_config(sp)
    add_host(sp)
    sp.add_argument("--arm", default="both", choices=["both", "glean", "direct"])
    sp.add_argument("--participant-id", required=True)
    sp.add_argument("--prompt-count", type=int, default=3)
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--rerun-existing", action="store_true")
    sp.set_defaults(func=command_smoke_test)

    sp = sub.add_parser("run-all", help="Run both arms, grade, report, and package")
    add_config(sp)
    add_host(sp)
    sp.add_argument("--participant-id", required=True)
    sp.add_argument("--smoke", action="store_true", help="Run the three-prompt smoke test instead of the full eval")
    sp.add_argument("--no-package", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--rerun-existing", action="store_true")
    sp.set_defaults(func=command_run_all)

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
