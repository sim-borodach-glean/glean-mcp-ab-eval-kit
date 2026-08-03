"""Cursor host adapter (SKELETON).

Runs one prompt headless via Cursor's `cursor-agent` CLI and harvests per-run
metrics. The design is settled; the spots that need a live Cursor to confirm are
marked `TODO(verify)`. See docs/hosts/cursor.md.

Key differences from Claude Code (from Cursor docs, 2026-07):
  - Command: `cursor-agent -p "<prompt>" --output-format stream-json --force --trust`.
  - Isolation: no `--strict-mcp-config` flag. We isolate by pointing `--workspace`
    at a per-run dir that contains only this arm's `.cursor/mcp.json`, plus a
    `.cursor/cli.json` permissions block for read-only gating (deny wins).
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
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import HostAdapter, register


def _normalize_server_name(name: Optional[str]) -> Optional[str]:
    """Match glean_mcp_eval.normalize_server_name so observed server names line
    up with the normalized expected/forbidden names the core compares against.
    Cursor emits canonical provider identifiers (e.g. "Atlassian-MCP-Server")
    that would otherwise never match a normalized config entry."""
    if not name:
        return name
    return re.sub(r"[^a-z0-9_-]+", "", name.lower().strip())

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


def _permissions_from_arm(arm_cfg: Dict[str, Any]) -> Dict[str, List[str]]:
    """Translate the kit's allowed_tools/disallowed_tools into a Cursor
    `.cursor/cli.json` permissions block. Deny wins over allow.

    TODO(verify): confirm the exact rule grammar Cursor accepts. Docs show
    `Mcp(server:tool)`, `Write(...)`, `Shell(...)`, `Read(...)` with `*`. We
    translate `mcp__<server>__<tool>` -> `Mcp(<server>:<tool>)` and always deny
    Write/Shell for a read-only eval.
    """
    def to_rule(tool: str) -> Optional[str]:
        if tool.startswith("mcp__"):
            parts = tool.split("__")
            if len(parts) >= 3:
                return f"Mcp({parts[1]}:{parts[2]})"
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
        ws = out_dir / "_cursor_ws"
        cmd: List[str] = [
            "cursor-agent", "-p", prompt,
            "--output-format", "stream-json",  # per-event; lets us capture model + usage + tool calls
            "--force", "--trust",              # fully unattended (no approval / trust prompts)
            "--workspace", str(ws),
        ]
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
            "servers": _load_arm_servers(root, arm_cfg),
            "permissions": _permissions_from_arm(arm_cfg),
            "raw_output_path": str(out_dir / "cursor_output.json"),
        }
        return cmd, ctx

    def prepare(self, ctx: Dict[str, Any]) -> None:
        ws = Path(ctx["ws"])
        cdir = ws / ".cursor"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "mcp.json").write_text(
            json.dumps({"mcpServers": ctx.get("servers", {})}, indent=2), encoding="utf-8"
        )
        (cdir / "cli.json").write_text(
            json.dumps({"permissions": ctx.get("permissions", {})}, indent=2), encoding="utf-8"
        )

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

        transcript = {
            "found": bool(events),
            "usage": usage,
            "unknown_usage": {},
            "models": {model: 1} if model else {},
            "tool_calls": tool_calls,
            "tool_call_count": len(tool_calls),
            "mcp_tool_call_count": sum(1 for tc in tool_calls if tc.get("server")),
            "mcp_servers_used": mcp_servers_used,
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
        return {
            "cursor_agent_on_path": shutil.which("cursor-agent"),
            "caps": self.caps,
            "notes": [
                "Cursor does not expose per-run $ cost; list-price-normalized cost is used.",
                "TODO(verify): confirm token usage appears in stream-json on your cursor-agent version.",
                "Run the judge on a structured-output host via config 'judge_host' (default claude-code).",
            ] if present else ["cursor-agent not found on PATH"],
        }


register(CursorAdapter())
