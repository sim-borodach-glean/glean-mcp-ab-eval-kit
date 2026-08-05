"""Cursor host adapter.

Runs one prompt headless via Cursor's `cursor-agent` CLI and harvests per-run
metrics. It also supports the Cursor Glean plugin variant: treatment can load a
verified plugin directory, control can require a manual deactivate/uninstall
checkpoint, and stream-json routing evidence is recorded. See
`docs/hosts/cursor.md` and `docs/hosts/cursor-glean-plugin.md`.

Key differences from Claude Code (from Cursor docs, 2026-07):
  - Command: `cursor-agent -p "<prompt>" --output-format stream-json --force --trust`.
  - Isolation: no `--strict-mcp-config` flag. With global MCP management enabled,
    the kit swaps the global config per arm and reuses the repository workspace so
    Cursor's project-scoped OAuth state is preserved; a temporary `.cursor/cli.json`
    permissions block provides read-only gating (deny wins). A per-run workspace is
    used only when global MCP management is disabled.
  - Cost: NOT exposed by Cursor — `reported_cost_usd` is always None; the kit's
    list-price-normalized `computed_cost_usd` is the cross-host cost metric.
  - Token usage: available via the TypeScript SDK for certain; via CLI JSON it is
    version-dependent and not yet in the published schema. We parse `stream-json`
    best-effort and leave a TODO to confirm the field on the pinned CLI version.
  - Structured output: no JSON-schema enforcement, so run the JUDGE on a
    structured-output-capable host (config `judge_host`, default claude-code).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import HostAdapter, HostSetupError, register


def _cursor_bin() -> str:
    return shutil.which("cursor-agent") or "cursor-agent"


def _run_mcp(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a `cursor-agent mcp ...` subcommand, capturing text output."""
    return subprocess.run(
        [_cursor_bin(), "mcp", *args],
        text=True, capture_output=True, timeout=timeout, check=False,
    )


def _parse_mcp_list() -> Dict[str, str]:
    """Return {server_identifier: status} from `cursor-agent mcp list`.

    Lines look like `glean_default: ready` or `slack: requires_authentication`.
    Status is lowercased; server identifiers are kept verbatim (they are the
    handles `mcp enable/disable/login` expect)."""
    out: Dict[str, str] = {}
    try:
        proc = _run_mcp("list", timeout=60)
    except Exception:
        return out
    for line in (proc.stdout or "").splitlines():
        if ":" not in line:
            continue
        name, _, status = line.partition(":")
        name = name.strip()
        status = status.strip().lower()
        if name and status:
            out[name] = status
    return out


def _normalize_server_name(name: Optional[str]) -> Optional[str]:
    """Match glean_mcp_eval.normalize_server_name so observed server names line
    up with the normalized expected/forbidden names the core compares against.
    Cursor emits canonical provider identifiers (e.g. "Atlassian-MCP-Server")
    that would otherwise never match a normalized config entry."""
    if not name:
        return name
    # Cursor plugin identifiers may contain spaces (for example
    # "plugin-Glean vNext-glean"); preserve word boundaries so they match the
    # configured canonical identifier "plugin-glean-vnext-glean".
    return re.sub(r"[^a-z0-9_-]+", "-", name.lower().strip()).strip("-")


def _plugin_settings(cfg: Dict[str, Any], arm_cfg: Dict[str, Any]) -> Dict[str, Any]:
    settings = dict(cfg.get("cursor_plugin") or {})
    settings.update(arm_cfg.get("cursor_plugin") or {})
    return settings


def _plugin_state(arm_cfg: Dict[str, Any], settings: Dict[str, Any]) -> Optional[str]:
    state = arm_cfg.get("plugin_state")
    if state is None:
        state = settings.get("required_state")
    if state is None:
        return None
    state = str(state).strip().lower()
    if state not in {"enabled", "disabled"}:
        raise HostSetupError(f"Cursor plugin_state must be 'enabled' or 'disabled', got {state!r}")
    return state


def _plugin_manifest(path: Path) -> Dict[str, Any]:
    manifest = path / ".cursor-plugin" / "plugin.json"
    if not manifest.exists():
        return {}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _discover_plugin_dir(settings: Dict[str, Any]) -> Optional[Path]:
    """Find an installed Cursor plugin without touching activation state."""
    configured = settings.get("plugin_dir")
    env_name = settings.get("plugin_dir_env")
    if not configured and env_name:
        configured = os.environ.get(str(env_name))
    if configured:
        path = Path(os.path.expandvars(os.path.expanduser(str(configured))))
        return path if path.is_dir() else None
    if not settings.get("auto_discover", False):
        return None
    wanted = str(settings.get("plugin_id") or settings.get("id") or "").strip().lower()
    cache_root = Path.home() / ".cursor" / "plugins" / "cache"
    candidates: List[Tuple[Tuple[int, ...], float, Path]] = []
    if not cache_root.exists():
        return None
    for manifest_path in cache_root.glob("**/.cursor-plugin/plugin.json"):
        plugin_dir = manifest_path.parent.parent
        manifest = _plugin_manifest(plugin_dir)
        name = str(manifest.get("name") or "").lower()
        if wanted and name != wanted:
            continue
        version = tuple(int(x) for x in re.findall(r"\d+", str(manifest.get("version") or "0")))
        candidates.append((version, manifest_path.stat().st_mtime, plugin_dir))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item[0], item[1]), reverse=True)[0][2]


def _plugin_server_ids(settings: Dict[str, Any]) -> set:
    values = settings.get("server_identifiers") or settings.get("server_identifier") or []
    if isinstance(values, str):
        values = [values]
    return {_normalize_server_name(str(value)) for value in values if value}


def _manual_plugin_checkpoint(arm_name: str, state: str, settings: Dict[str, Any]) -> None:
    if not settings.get("manual_confirmation", True):
        return
    action = settings.get("enable_instruction") if state == "enabled" else settings.get("disable_instruction")
    if not action:
        action = (
            "install/enable the Cursor plugin"
            if state == "enabled"
            else "deactivate or uninstall the Cursor plugin"
        )
    expected = "PLUGIN_ON" if state == "enabled" else "PLUGIN_OFF"
    print(
        f"\n⚠ Cursor plugin checkpoint for arm '{arm_name}': {action}.\n"
        f"When the application is ready, type {expected} and press Enter. "
        "The evaluator will not retry or toggle the plugin in a loop.",
        flush=True,
    )
    if not sys.stdin.isatty():
        raise HostSetupError(
            f"Arm '{arm_name}' requires manual Cursor plugin state '{state}', but stdin is not interactive. "
            f"{action.capitalize()}, then rerun this command from a terminal and type {expected}."
        )
    response = input(f"Confirm {expected}: ").strip().upper()
    if response != expected:
        raise HostSetupError(
            f"Plugin checkpoint cancelled for arm '{arm_name}'. Expected {expected}; no run was started."
        )

# Cursor usage field names -> our canonical USAGE_KEYS. TODO(verify) against the
# pinned cursor-agent version; the CLI JSON usage shape is not yet documented.
_USAGE_ALIASES = {
    "input_tokens": ("inputTokens", "input", "input_tokens", "prompt_tokens"),
    "output_tokens": ("outputTokens", "output", "output_tokens", "completion_tokens"),
    "cache_creation_input_tokens": ("cacheWriteTokens", "cache_write", "cacheCreationInputTokens"),
    "cache_read_input_tokens": ("cacheReadTokens", "cache_read", "cacheReadInputTokens"),
}


def _load_arm_servers(root: Path, arm_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Read this arm's mcp_config file and return its mcpServers mapping.

    TODO(verify): Cursor's `.cursor/mcp.json` server-entry shape may differ from
    Claude's (Claude: {"type","url","headers"}; Cursor remote: {"url": ...} or
    {"command","args"}). For a Cursor arm, point mcp_config at a Cursor-shaped
    file (see config/mcp.cursor.example.json). We stage the entries as-is.
    """
    mcp_config = arm_cfg.get("mcp_config")
    if not mcp_config:
        return {}
    p = Path(mcp_config)
    if not p.is_absolute():
        p = root / p
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    servers = data.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def _permissions_from_arm(
    arm_cfg: Dict[str, Any],
    plugin_settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[str]]:
    """Translate the kit's allowed_tools/disallowed_tools into a Cursor
    `.cursor/cli.json` permissions block. Deny wins over allow.

    TODO(verify): confirm the exact rule grammar Cursor accepts. Docs show
    `Mcp(server:tool)`, `Write(...)`, `Shell(...)`, `Read(...)` with `*`. We
    translate `mcp__<server>__<tool>` -> `Mcp(<server>:<tool>)` and always deny
    Write/Shell for a read-only eval.
    """
    plugin_settings = plugin_settings or {}
    plugin_ids = _plugin_server_ids(plugin_settings)
    plugin_raw_id = str(plugin_settings.get("server_identifier") or "").strip()

    def to_rule(tool: str) -> Optional[str]:
        if tool.startswith("mcp__"):
            parts = tool.split("__")
            if len(parts) >= 3:
                server = parts[1]
                if plugin_raw_id and _normalize_server_name(server) in plugin_ids:
                    # Cursor may expose a plugin server with spaces/case in
                    # runtime events. Permission rules must use that raw name,
                    # while observed metrics use the normalized name.
                    server = plugin_raw_id
                return f"Mcp({server}:{parts[2]})"
            if len(parts) == 2:
                return f"Mcp({parts[1]}:*)"
        return tool  # pass through non-mcp rules verbatim

    allow = [r for r in (to_rule(t) for t in (arm_cfg.get("allowed_tools") or [])) if r]
    deny = [r for r in (to_rule(t) for t in (arm_cfg.get("disallowed_tools") or [])) if r]
    # Read-only floor: never allow writes or shell during an eval run.
    for hard in ("Write(**)", "Shell(**)"):
        if hard not in deny:
            deny.append(hard)
    return {"allow": allow, "deny": deny}


def _extract_usage(obj: Any) -> Dict[str, int]:
    """Best-effort pull of the four canonical token counts from a Cursor result
    object. Handles a flat `usage` map and a nested `tokens.cache.{read,write}`
    shape. TODO(verify) on the pinned CLI version."""
    usage = {k: 0 for k in _USAGE_ALIASES}
    if not isinstance(obj, dict):
        return usage
    src = obj.get("usage") or obj.get("tokens") or {}
    if not isinstance(src, dict):
        return usage
    cache = src.get("cache") if isinstance(src.get("cache"), dict) else {}
    for canonical, aliases in _USAGE_ALIASES.items():
        for a in aliases:
            if isinstance(src.get(a), (int, float)):
                usage[canonical] = int(src[a])
                break
        else:
            if canonical == "cache_read_input_tokens" and isinstance(cache.get("read"), (int, float)):
                usage[canonical] = int(cache["read"])
            elif canonical == "cache_creation_input_tokens" and isinstance(cache.get("write"), (int, float)):
                usage[canonical] = int(cache["write"])
    return usage


def _parse_tool_call(ev: Dict[str, Any]) -> Optional[Dict[str, Optional[str]]]:
    """Extract {name, server} from a Cursor `tool_call` event.

    Cursor nests the payload under ev["tool_call"] as a single-key dict whose key
    names the tool kind, e.g. {"mcpToolCall": {"args": {...}}} for MCP calls or
    {"readToolCall": {...}} for built-ins. MCP args carry providerIdentifier /
    serverIdentifier and toolName. Older/flat shapes (top-level name/tool) are
    still handled as a fallback.
    """
    tc = ev.get("tool_call")
    if not isinstance(tc, dict):
        name = str(ev.get("name") or ev.get("tool") or "")
        parts = name.split("__")
        server = parts[1] if name.startswith("mcp__") and len(parts) >= 2 else None
        return {"name": name, "server": _normalize_server_name(server)} if name else None
    kind, payload = next(iter(tc.items()), (None, None))
    if kind == "mcpToolCall" and isinstance(payload, dict):
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        server = args.get("providerIdentifier") or args.get("serverIdentifier")
        tool = args.get("toolName")
        name = f"mcp__{server}__{tool}" if server and tool else str(args.get("name") or "")
        return {"name": name, "server": _normalize_server_name(server)}
    # Non-MCP built-in tool (read/edit/shell/etc.): keep the call, no MCP server.
    name = str(kind or ev.get("name") or "")
    return {"name": name, "server": None} if name else None


class CursorAdapter(HostAdapter):
    name = "cursor"
    caps = {
        "per_arm_isolation": True,       # via per-arm --workspace + staged .cursor/mcp.json
        "readonly_gating": True,         # via .cursor/cli.json permissions (deny wins)
        "per_run_token_usage": True,     # via stream-json / SDK — TODO(verify) on CLI version
        "reported_cost": False,          # Cursor does not expose per-run cost
        "structured_output": False,      # no JSON-schema enforcement; judge elsewhere
    }

    def executable_present(self) -> bool:
        return shutil.which("cursor-agent") is not None

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
        use_global_mcp = bool(cfg.get("cursor_manage_global_mcp", True))
        # When global MCP management is enabled, setup_arm has already swapped
        # ~/.cursor/mcp.json to this arm's server set. Reusing the repository
        # workspace preserves Cursor's project-scoped OAuth/approval state;
        # creating a fresh workspace per prompt would make every MCP look
        # unauthenticated again.
        ws = root if use_global_mcp else out_dir / "_cursor_ws"
        disable_mcp = bool(arm_cfg.get("disable_mcp_for_answer"))
        cmd: List[str] = [
            "cursor-agent", "-p", prompt,
            "--output-format", "stream-json",  # per-event; lets us capture model + usage + tool calls
            "--force", "--trust",              # fully unattended (no approval / trust prompts)
        ]
        if not disable_mcp:
            cmd.append("--approve-mcps")       # load the arm-only MCP servers without interactive approval
        cmd.extend(["--workspace", str(ws)])
        settings = _plugin_settings(cfg, arm_cfg)
        required_plugin_state = _plugin_state(arm_cfg, settings)
        plugin_dir = None
        if required_plugin_state == "enabled" and str(settings.get("activation_mode", "manual")) == "plugin-dir":
            plugin_dir = _discover_plugin_dir(settings)
            if plugin_dir is None:
                raise HostSetupError(
                    "Glean Cursor plugin is required for this arm, but no plugin directory was found. "
                    f"Set {settings.get('plugin_dir_env', 'cursor_plugin.plugin_dir')} or install the plugin, then rerun."
                )
            cmd.extend(["--plugin-dir", str(plugin_dir)])
        model = cfg.get(model_key) or cfg.get("model")
        if model:
            cmd.extend(["--model", str(model)])
        # NOTE: cursor-agent has no documented --max-turns / step-limit flag; the
        # `max_turns` arg is intentionally ignored. json_schema is NOT enforced by
        # Cursor — the judge should run on a structured-output host (judge_host).
        extra = cfg.get("extra_cursor_args") or []
        cmd.extend([str(x) for x in extra])
        ctx = {
            "cwd": str(ws),
            "ws": str(ws),
            "use_global_mcp": use_global_mcp,
            "servers": {} if disable_mcp else _load_arm_servers(root, arm_cfg),
            "permissions": _permissions_from_arm(arm_cfg, settings),
            "disable_global_mcp": disable_mcp,
            "raw_output_path": str(out_dir / "cursor_output.json"),
            "plugin_state": required_plugin_state,
            "plugin_server_ids": sorted(_plugin_server_ids(settings)),
            "plugin_dir": str(plugin_dir) if plugin_dir else None,
            "plugin_settings": settings,
        }
        return cmd, ctx

    def prepare(self, ctx: Dict[str, Any]) -> None:
        ws = Path(ctx["ws"])
        cdir = ws / ".cursor"
        cdir.mkdir(parents=True, exist_ok=True)
        restore: Dict[str, Optional[str]] = {}

        # With global MCP management, the global file is the authoritative,
        # already-authenticated arm config. A duplicate local mcp.json would be
        # treated as a new Cursor project and require OAuth again.
        mcp_path = cdir / "mcp.json"
        if ctx.get("use_global_mcp"):
            restore[str(mcp_path)] = mcp_path.read_text(encoding="utf-8") if mcp_path.exists() else None
            if mcp_path.exists():
                mcp_path.unlink()
        else:
            mcp_path.write_text(
                json.dumps({"mcpServers": ctx.get("servers", {})}, indent=2), encoding="utf-8"
            )

        cli_path = cdir / "cli.json"
        restore[str(cli_path)] = cli_path.read_text(encoding="utf-8") if cli_path.exists() else None
        cli_path.write_text(
            json.dumps({"permissions": ctx.get("permissions", {})}, indent=2), encoding="utf-8"
        )
        ctx["_restore_cursor_files"] = restore

    def cleanup(self, ctx: Dict[str, Any]) -> None:
        for raw_path, original in (ctx.pop("_restore_cursor_files", {}) or {}).items():
            path = Path(raw_path)
            if original is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(original, encoding="utf-8")

    # --- Per-arm isolation + auth -------------------------------------------
    # cursor-agent MERGES the global ~/.cursor/mcp.json into every run AND, under
    # `--force`/`--approve-mcps`, will load servers from it regardless of the
    # approved-list (enable/disable) state. So neither `--workspace` nor
    # `mcp disable` actually isolates an arm — the other arm's servers leak in
    # and the model uses them, invalidating the A/B.
    #
    # The only reliable isolation is to control what the global config contains:
    # for the duration of an arm we REPLACE ~/.cursor/mcp.json with an arm-only
    # version (reusing each kept server's original entry so stored OAuth still
    # works, since tokens are keyed by identifier, not by file contents), then
    # restore the original in teardown (finally). Gated by cursor_manage_global_mcp.
    _GLOBAL_MCP = Path.home() / ".cursor" / "mcp.json"
    _mcp_managed: bool = False
    _mcp_backup: Optional[str] = None
    _answer_mcp_backup: Optional[str] = None
    # Reuse one manual confirmation across preflight + run when `run-all` or
    # `smoke-test` invokes both in the same process. A separate CLI invocation
    # asks again, which is intentional because Desktop state may have changed.
    _plugin_checkpoints: Dict[Tuple[str, str], bool] = {}

    def suspend_mcp(self) -> None:
        """Hide the arm's global MCPs during a synthesis-only subprocess."""
        if self._answer_mcp_backup is not None or not self._GLOBAL_MCP.exists():
            return
        self._answer_mcp_backup = self._GLOBAL_MCP.read_text(encoding="utf-8")
        self._GLOBAL_MCP.write_text(json.dumps({"mcpServers": {}}, indent=2), encoding="utf-8")

    def restore_mcp(self) -> None:
        """Restore the arm MCPs after a synthesis-only subprocess."""
        if self._answer_mcp_backup is None:
            return
        self._GLOBAL_MCP.write_text(self._answer_mcp_backup, encoding="utf-8")
        self._answer_mcp_backup = None

    def _arm_server_ids(self, arm_cfg: Dict[str, Any]) -> List[str]:
        """Server identifiers this arm needs, in the exact case cursor-agent
        uses (they double as `mcp enable/login` handles)."""
        ids: List[str] = []
        for s in arm_cfg.get("expected_mcp_servers", []) or []:
            if s and s not in ids:
                ids.append(str(s))
        for name in (_load_arm_servers(Path("."), arm_cfg) or {}).keys():
            if name not in ids:
                ids.append(name)
        return ids

    def setup_arm(self, root: Path, cfg: Dict[str, Any], arm_cfg: Dict[str, Any], arm_name: str) -> None:
        settings = _plugin_settings(cfg, arm_cfg)
        required_plugin_state = _plugin_state(arm_cfg, settings)
        if required_plugin_state:
            activation_mode = str(settings.get("activation_mode", "manual"))
            if required_plugin_state == "enabled" and activation_mode == "plugin-dir":
                plugin_dir = _discover_plugin_dir(settings)
                if plugin_dir is None:
                    raise HostSetupError(
                        "Glean Cursor plugin is required but its local plugin directory could not be found. "
                        f"Install it or set {settings.get('plugin_dir_env', 'CURSOR_GLEAN_PLUGIN_DIR')}, then rerun."
                    )
                manifest = _plugin_manifest(plugin_dir)
                expected_version = str(settings.get("version") or "").strip()
                actual_version = str(manifest.get("version") or "").strip()
                if expected_version and actual_version != expected_version:
                    raise HostSetupError(
                        f"Cursor plugin version mismatch: config expects {expected_version}, "
                        f"but discovered {actual_version or 'unknown'} at {plugin_dir}. "
                        "Update the config or install the expected plugin version."
                    )
                print(
                    f"✓ Cursor plugin ready for '{arm_name}': {manifest.get('name', 'unknown')} "
                    f"{actual_version or 'unknown'} at {plugin_dir}",
                    flush=True,
                )
            else:
                checkpoint_key = (arm_name, required_plugin_state)
                if self._plugin_checkpoints.get(checkpoint_key):
                    print(
                        f"✓ Reusing confirmed Cursor plugin state '{required_plugin_state}' for arm '{arm_name}'.",
                        flush=True,
                    )
                else:
                    _manual_plugin_checkpoint(arm_name, required_plugin_state, settings)
                    self._plugin_checkpoints[checkpoint_key] = True
        if not cfg.get("cursor_manage_global_mcp", True):
            return
        keep = self._arm_server_ids(arm_cfg)
        keep_norm = {_normalize_server_name(k) for k in keep}
        # Snapshot the original global config so teardown can restore it exactly.
        self._mcp_backup = self._GLOBAL_MCP.read_text(encoding="utf-8") if self._GLOBAL_MCP.exists() else None
        self._mcp_managed = True
        orig_servers: Dict[str, Any] = {}
        if self._mcp_backup:
            try:
                orig_servers = (json.loads(self._mcp_backup).get("mcpServers") or {})
            except Exception:
                orig_servers = {}
        arm_file_servers = _load_arm_servers(root, arm_cfg)
        # Keep each arm server's ORIGINAL global entry (preserves auth/url); fall
        # back to the arm's own mcp_config entry if it isn't in the global file.
        new_servers: Dict[str, Any] = {}
        for name, entry in orig_servers.items():
            if _normalize_server_name(name) in keep_norm:
                new_servers[name] = entry
        for name in keep:
            if not any(_normalize_server_name(n) == _normalize_server_name(name) for n in new_servers):
                if name in arm_file_servers:
                    new_servers[name] = arm_file_servers[name]
        self._GLOBAL_MCP.parent.mkdir(parents=True, exist_ok=True)
        self._GLOBAL_MCP.write_text(json.dumps({"mcpServers": new_servers}, indent=2), encoding="utf-8")
        removed = [n for n in orig_servers if _normalize_server_name(n) not in keep_norm]
        print(
            f"🔒 Isolated '{arm_name}' arm MCP (global config swapped): "
            f"kept [{', '.join(new_servers) or '—'}]; removed [{', '.join(removed) or '—'}]",
            flush=True,
        )
        if cfg.get("cursor_ensure_auth", True):
            for name in new_servers:
                _run_mcp("enable", name)
                if _parse_mcp_list().get(name) == "ready":
                    print(f"✓ {name}: already authenticated", flush=True)
                    continue
                print(f"🔐 Authenticating {name} — a browser window may open for approval…", flush=True)
                try:
                    _run_mcp("login", name, timeout=180)
                except Exception as e:
                    print(f"✗ {name}: auth attempt errored ({e})", flush=True)
                    continue
                ok = _parse_mcp_list().get(name) == "ready"
                print(f"{'✓' if ok else '✗'} Authentication {'complete' if ok else 'FAILED'}: {name}", flush=True)

    def teardown_arm(self, root: Path, cfg: Dict[str, Any], arm_cfg: Dict[str, Any], arm_name: str) -> None:
        if not self._mcp_managed:
            return
        try:
            if self._mcp_backup is None:
                if self._GLOBAL_MCP.exists():
                    self._GLOBAL_MCP.unlink()
            else:
                self._GLOBAL_MCP.write_text(self._mcp_backup, encoding="utf-8")
            print(f"↩ Restored global MCP config after '{arm_name}' arm.", flush=True)
        finally:
            self._mcp_managed = False
            self._mcp_backup = None

    def harvest(
        self,
        proc: Dict[str, Any],
        root: Path,
        out_dir: Path,
        cfg: Dict[str, Any],
        ctx: Dict[str, Any],
    ) -> Dict[str, Any]:
        events: List[Dict[str, Any]] = []
        for line in (proc.get("stdout") or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                continue

        raw_path = ctx.get("raw_output_path") or str(out_dir / "cursor_output.json")
        Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
        Path(raw_path).write_text(json.dumps(events, indent=2), encoding="utf-8")

        model = None
        answer_text = ""
        duration_ms = None
        session_id = None
        is_error = False
        tool_calls: List[Dict[str, Any]] = []
        seen_tool_call_ids: set = set()
        usage = {k: 0 for k in _USAGE_ALIASES}

        for ev in events:
            etype = ev.get("type")
            if etype == "system":
                model = ev.get("model") or model
                session_id = ev.get("session_id") or ev.get("sessionId") or session_id
            elif etype in ("tool_call", "tool_use"):
                # Cursor emits a started + completed event per call; dedupe by call_id
                # so one tool call is counted once.
                call_id = ev.get("call_id") or ev.get("callId") or ev.get("id")
                if call_id is not None and call_id in seen_tool_call_ids:
                    continue
                parsed = _parse_tool_call(ev)
                if parsed is None:
                    continue
                if call_id is not None:
                    seen_tool_call_ids.add(call_id)
                tool_calls.append(parsed)
            elif etype == "result":
                answer_text = ev.get("result") or answer_text
                duration_ms = ev.get("duration_ms") or duration_ms
                session_id = ev.get("session_id") or session_id
                is_error = bool(ev.get("is_error"))
                got = _extract_usage(ev)
                if any(got.values()):
                    usage = got

        mcp_servers_used: Dict[str, int] = {}
        for tc in tool_calls:
            if tc.get("server"):
                mcp_servers_used[tc["server"]] = mcp_servers_used.get(tc["server"], 0) + 1

        plugin_server_ids = set(ctx.get("plugin_server_ids") or [])
        plugin_calls = [
            tc for tc in tool_calls
            if tc.get("server") in plugin_server_ids
            or str(tc.get("name") or "").lower().endswith("__glean_run")
        ]
        plugin_servers_used: Dict[str, int] = {}
        for tc in plugin_calls:
            server = tc.get("server") or "plugin_skill"
            plugin_servers_used[server] = plugin_servers_used.get(server, 0) + 1
        glean_mcp_ids = {
            _normalize_server_name(str(value))
            for value in (ctx.get("plugin_settings") or {}).get(
                "glean_mcp_server_identifiers", ["glean_default", "glean"]
            )
            if value
        }
        plugin_observed = bool(plugin_calls)
        glean_mcp_observed = any(server in glean_mcp_ids for server in mcp_servers_used)
        if plugin_observed and glean_mcp_observed:
            routing_outcome = "mixed"
        elif plugin_observed:
            routing_outcome = "plugin"
        elif glean_mcp_observed:
            routing_outcome = "mcp"
        elif mcp_servers_used:
            routing_outcome = "other_mcp"
        else:
            routing_outcome = "none"
        required_plugin_state = ctx.get("plugin_state")

        transcript = {
            "found": bool(events),
            "usage": usage,
            "unknown_usage": {},
            "models": {model: 1} if model else {},
            "tool_calls": tool_calls,
            "tool_call_count": len(tool_calls),
            "mcp_tool_call_count": sum(1 for tc in tool_calls if tc.get("server")),
            "mcp_servers_used": mcp_servers_used,
            "plugin_tool_call_count": len(plugin_calls),
            "plugin_servers_used": plugin_servers_used,
            "plugin_loaded": bool(ctx.get("plugin_dir")),
            "plugin_tool_names": sorted(str(tc.get("name") or "") for tc in plugin_calls),
            "plugin_state_expected": required_plugin_state,
            "plugin_present_when_disabled": required_plugin_state == "disabled" and plugin_observed,
            "plugin_required_but_unobserved": required_plugin_state == "enabled"
            and bool((ctx.get("plugin_settings") or {}).get("require_plugin_route", False))
            and not plugin_observed,
            "routing_outcome": routing_outcome,
            "routing_confounded": routing_outcome == "mixed",
            "retrieval_attempted": bool(tool_calls),
            "errors": [] if events else ["no stream-json events parsed"],
        }
        return {
            "ok": proc.get("returncode") == 0 and not is_error,
            "session_id": session_id,
            "transcript": transcript,
            "usage": usage,
            "reported_cost_usd": None,  # Cursor does not expose per-run cost
            "duration_ms": duration_ms,
            "num_turns": None,
            "answer_text": answer_text,
            "raw_output_path": raw_path,
            "output_type": "cursor-stream-json",
            "output_subtype": "error" if is_error else "success",
        }

    def doctor(self, root: Path) -> Dict[str, Any]:
        present = self.executable_present()
        plugin_dir = (
            _discover_plugin_dir({"plugin_id": "glean", "auto_discover": True})
            or _discover_plugin_dir({"plugin_id": "glean-vnext", "auto_discover": True})
        )
        manifest = _plugin_manifest(plugin_dir) if plugin_dir else {}
        return {
            "cursor_agent_on_path": shutil.which("cursor-agent"),
            "caps": self.caps,
            "plugin_inventory": {
                "plugin_id": manifest.get("name") if manifest else "glean",
                "version": manifest.get("version") if manifest else None,
                "path": str(plugin_dir) if plugin_dir else None,
                "installed": bool(plugin_dir),
                "note": "Installed is not the same as active; live preflight checks runtime routing.",
            },
            "notes": [
                "Cursor does not expose per-run $ cost; list-price-normalized cost is used.",
                "TODO(verify): confirm token usage appears in stream-json on your cursor-agent version.",
                "Run the judge on a structured-output host via config 'judge_host' (default claude-code).",
                "Plugin arms fail closed on a missing manual checkpoint and never auto-uninstall or retry indefinitely.",
            ] if present else ["cursor-agent not found on PATH"],
        }


register(CursorAdapter())
