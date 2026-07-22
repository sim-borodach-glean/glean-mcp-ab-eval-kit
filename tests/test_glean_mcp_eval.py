import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import glean_mcp_eval as gme  # noqa: E402
from hosts import cursor as cursor_host  # noqa: E402


class TranscriptParserTest(unittest.TestCase):
    def test_parse_usage_models_and_mcp_tools(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "session.jsonl"
            rows = [
                {
                    "type": "assistant",
                    "message": {
                        "model": "claude-opus-test",
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "cache_creation_input_tokens": 300,
                            "cache_read_input_tokens": 400,
                        },
                        "content": [
                            {"type": "tool_use", "id": "toolu_1", "name": "mcp__glean__search", "input": {"query": "x"}},
                            {"type": "text", "text": "done"},
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "model": "claude-opus-test",
                        "usage": {"input_tokens": 7, "output_tokens": 3, "server_tool_use_tokens": 11},
                        "content": [{"type": "tool_use", "id": "toolu_2", "name": "mcp__slack__search", "input": {}}],
                    },
                },
            ]
            p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            parsed = gme.parse_transcript(p)
            self.assertTrue(parsed["found"])
            self.assertEqual(parsed["usage"]["input_tokens"], 107)
            self.assertEqual(parsed["usage"]["output_tokens"], 23)
            self.assertEqual(parsed["usage"]["cache_creation_input_tokens"], 300)
            self.assertEqual(parsed["usage"]["cache_read_input_tokens"], 400)
            self.assertEqual(parsed["unknown_usage"]["server_tool_use_tokens"], 11)
            self.assertEqual(parsed["models"], {"claude-opus-test": 2})
            self.assertEqual(parsed["mcp_servers_used"], {"glean": 1, "slack": 1})
            self.assertTrue(parsed["retrieval_attempted"])


class ReportTest(unittest.TestCase):
    def test_report_from_synthetic_pair(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "golden_prompts.tsv").write_text("ID\tDept\tPrompt\nQ1\tEng\tQuestion?\n", encoding="utf-8")
            cfg = {
                "eval_name": "unit",
                "prompts_file": "golden_prompts.tsv",
                "results_dir": "results",
                "pricing_per_million": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 1,
                    "cache_read_input_tokens": 1,
                },
                "arms": {"glean": {}, "direct": {}},
            }
            cfg_path = root / "eval.config.json"
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            for arm, tokens in [("glean", 100), ("direct", 200)]:
                d = root / "results" / "p1" / arm / "Q1"
                d.mkdir(parents=True)
                (d / "metadata.json").write_text(json.dumps({"id": "Q1", "dept": "Eng", "prompt": "Question?"}), encoding="utf-8")
                (d / "answer.md").write_text(f"{arm} answer", encoding="utf-8")
                run = {
                    "success": True,
                    "total_tokens": tokens,
                    "computed_cost_usd": tokens / 1_000_000,
                    "usage": {"input_tokens": tokens, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                    "transcript": {"retrieval_attempted": True, "models": {"m": 1}, "mcp_servers_used": {arm: 1}},
                }
                (d / "run.json").write_text(json.dumps(run), encoding="utf-8")
            rc = gme.main(["report", "--config", str(cfg_path)])
            self.assertEqual(rc, 0)
            summary = (root / "results" / "aggregate_summary.md").read_text(encoding="utf-8")
            self.assertIn("50.0% lower for Glean", summary)
            csv_text = (root / "results" / "aggregate_rows.csv").read_text(encoding="utf-8")
            self.assertIn("Q1", csv_text)

    def test_import_participant_submission_zip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = {"eval_name": "unit", "prompts_file": "golden_prompts.tsv", "results_dir": "results", "arms": {"glean": {}, "direct": {}}}
            cfg_path = root / "eval.config.json"
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            (root / "golden_prompts.tsv").write_text("ID\tPrompt\nQ1\tQuestion?\n", encoding="utf-8")

            src = root / "submission_src"
            participant_run = src / "customer01" / "glean" / "Q1"
            participant_run.mkdir(parents=True)
            (participant_run / "run.json").write_text(json.dumps({"success": True}), encoding="utf-8")
            (participant_run / "metadata.json").write_text(json.dumps({"id": "Q1"}), encoding="utf-8")
            zip_path = root / "eval_submission.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                for p in src.rglob("*"):
                    if p.is_file():
                        zf.write(p, arcname=p.relative_to(src).as_posix())

            rc = gme.main(["import", "--config", str(cfg_path), str(zip_path)])
            self.assertEqual(rc, 0)
            self.assertTrue((root / "results" / "customer01" / "glean" / "Q1" / "run.json").exists())


class BlindGradingTest(unittest.TestCase):
    def test_blind_assignment_deterministic(self):
        # Same inputs → same label every time (auditable / reproducible regrades).
        first = gme.blind_assignment("user01", "Q1")
        second = gme.blind_assignment("user01", "Q1")
        self.assertEqual(first, second)
        self.assertIsInstance(first, bool)

    def test_deblind_when_glean_is_a(self):
        blind = {
            "winner": "A",
            "completeness_winner": "B",
            "groundedness_winner": "tie",
            "completeness_a": 4, "completeness_b": 2,
            "groundedness_a": 5, "groundedness_b": 3,
            "confidence": "high", "reasoning": "x",
        }
        g = gme.deblind_grade(blind, glean_is_a=True)
        self.assertEqual(g["winner"], "glean")
        self.assertEqual(g["completeness_winner"], "direct")
        self.assertEqual(g["groundedness_winner"], "tie")
        self.assertEqual(g["completeness_glean"], 4)
        self.assertEqual(g["completeness_direct"], 2)
        self.assertEqual(g["groundedness_glean"], 5)
        self.assertEqual(g["confidence"], "high")

    def test_deblind_when_glean_is_b(self):
        # Glean shown as B: an "A" win must de-blind to direct.
        blind = {
            "winner": "A",
            "completeness_a": 4, "completeness_b": 2,
            "groundedness_a": 5, "groundedness_b": 3,
        }
        g = gme.deblind_grade(blind, glean_is_a=False)
        self.assertEqual(g["winner"], "direct")
        self.assertEqual(g["completeness_glean"], 2)
        self.assertEqual(g["completeness_direct"], 4)
        self.assertEqual(g["groundedness_glean"], 3)
        self.assertEqual(g["groundedness_direct"], 5)


class CursorServerIsolationTest(unittest.TestCase):
    def test_permissions_gate_tools_not_servers(self):
        arm = {
            "allowed_tools": ["mcp__glean_default__search"],
            "disallowed_tools": ["mcp__glean_default__run_tool"],
        }
        perms = cursor_host._permissions_from_arm(arm)
        self.assertIn("Mcp(glean_default:search)", perms["allow"])
        self.assertIn("Mcp(glean_default:run_tool)", perms["deny"])
        self.assertIn("Write(**)", perms["deny"])
        self.assertIn("Shell(**)", perms["deny"])

    def test_servers_to_disable_uses_require_live_then_expected(self):
        discovered = {
            "glean_default": "glean_default",
            "plugin-atlassian-atlassian": "plugin-atlassian-atlassian",
            "plugin-glean-vnext-glean": "plugin-glean-vnext-glean",
        }
        direct = {
            "expected_mcp_servers": ["atlassian"],
            "require_live_tool_servers": ["plugin-atlassian-atlassian"],
        }
        self.assertEqual(
            cursor_host._servers_to_disable(direct, discovered),
            ["glean_default", "plugin-glean-vnext-glean"],
        )
        glean = {"expected_mcp_servers": ["glean_default"]}
        self.assertEqual(
            cursor_host._servers_to_disable(glean, discovered),
            ["plugin-atlassian-atlassian", "plugin-glean-vnext-glean"],
        )

    def test_project_slug_matches_cursor(self):
        # Cursor collapses non-alphanumeric runs to '-'; '_cursor_ws' -> 'cursor-ws'.
        slug = cursor_host._cursor_project_slug(Path("/private/tmp/x/MVL-01/_cursor_ws"))
        self.assertTrue(slug.endswith("MVL-01-cursor-ws"))
        self.assertNotIn("_", slug)

    def test_discover_available_servers_global_and_plugins(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cur = home / ".cursor"
            (cur).mkdir()
            (cur / "mcp.json").write_text(
                json.dumps({"mcpServers": {"glean_default": {"url": "x"}}}), encoding="utf-8"
            )
            # plugin.json under .cursor-plugin/, .mcp.json at plugin root
            proot = cur / "plugins" / "cache" / "gleanwork" / "glean-vnext" / "abc"
            (proot / ".cursor-plugin").mkdir(parents=True)
            (proot / ".cursor-plugin" / "plugin.json").write_text(
                json.dumps({"name": "glean-vnext"}), encoding="utf-8"
            )
            (proot / ".mcp.json").write_text(
                json.dumps({"mcpServers": {"glean": {"command": "node"}}}), encoding="utf-8"
            )
            found = cursor_host._discover_available_servers(home)
        self.assertIn("glean_default", found)
        self.assertIn("plugin-glean-vnext-glean", found)  # plugin-<name>-<serverKey>

    def test_discover_missing_home_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cursor_host._discover_available_servers(Path(tmp)), {})

    def test_build_command_strips_approve_mcps_when_isolating(self):
        from unittest import mock
        disc = {
            "glean_default": "glean_default",
            "plugin-atlassian-atlassian": "plugin-atlassian-atlassian",
        }
        adapter = cursor_host.CursorAdapter()
        with mock.patch.object(cursor_host, "_discover_available_servers", return_value=disc):
            cfg = {"model": "m", "extra_cursor_args": ["--approve-mcps"]}
            arm = {"expected_mcp_servers": ["glean_default"]}
            cmd, ctx = adapter.build_command(Path("."), cfg, arm, "hi", Path("/tmp/xrun"))
        self.assertNotIn("--approve-mcps", cmd)  # dropped: it defeats isolation
        self.assertEqual(ctx["disabled_servers"], ["plugin-atlassian-atlassian"])

    def test_prepare_strips_approvals_for_disabled_servers(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".cursor").mkdir(parents=True)
            ws = Path(tmp) / "run" / "_cursor_ws"
            ws.mkdir(parents=True)
            src = Path(tmp) / "src"
            src.mkdir()
            (src / "mcp-auth.json").write_text("{}", encoding="utf-8")
            (src / "mcp-approvals.json").write_text(
                json.dumps([
                    "glean_default-abc",
                    "plugin-atlassian-atlassian-abc",
                    "atlassian-abc",
                ]),
                encoding="utf-8",
            )
            ctx = {
                "ws": str(ws),
                "servers": {},
                "permissions": {},
                "auth_source_dir": str(src),
                "disabled_servers": ["plugin-atlassian-atlassian", "plugin-glean-vnext-glean"],
            }
            adapter = cursor_host.CursorAdapter()
            with mock.patch.object(cursor_host.Path, "home", return_value=home):
                adapter.prepare(ctx)
            slug = cursor_host._cursor_project_slug(ws)
            staged = json.loads(
                (home / ".cursor" / "projects" / slug / "mcp-approvals.json").read_text()
            )
            self.assertIn("glean_default-abc", staged)       # sanctioned kept
            self.assertIn("atlassian-abc", staged)           # not disabled, kept
            self.assertNotIn("plugin-atlassian-atlassian-abc", staged)  # disabled -> stripped
            disabled = json.loads(
                (home / ".cursor" / "projects" / slug / "mcp-disabled.json").read_text()
            )
            self.assertEqual(
                sorted(disabled),
                ["plugin-atlassian-atlassian", "plugin-glean-vnext-glean"],
            )
class JudgeAliasNormalizationTest(unittest.TestCase):
    def test_winner_prefix_aliases_and_explicit_null(self):
        # Cursor emitted winner: null plus winner_overall/winner_quality/winner_tokens.
        bg = {
            "winner": None,
            "winner_overall": "B",
            "winner_quality": "B",
            "winner_tokens": "A",
            "efficiency_winner": None,
        }
        gme._normalize_judge_aliases(bg)
        self.assertEqual(bg["winner"], "B")
        self.assertEqual(bg["efficiency_winner"], "A")
        self.assertEqual(bg["usefulness_winner"], "B")

    def test_suffix_aliases_and_confidence_bucketing(self):
        bg = {"overall_winner": "A", "token_efficiency_winner": "tie", "rationale": "because", "confidence": 0.9}
        gme._normalize_judge_aliases(bg)
        self.assertEqual(bg["winner"], "A")
        self.assertEqual(bg["efficiency_winner"], "tie")
        self.assertEqual(bg["reasoning"], "because")
        self.assertEqual(bg["confidence"], "high")

    def test_canonical_values_are_preserved(self):
        bg = {"winner": "A", "efficiency_winner": "B", "reasoning": "keep", "confidence": "low"}
        gme._normalize_judge_aliases(bg)
        self.assertEqual(bg["winner"], "A")
        self.assertEqual(bg["efficiency_winner"], "B")
        self.assertEqual(bg["reasoning"], "keep")
        self.assertEqual(bg["confidence"], "low")


class PromptPrefixTest(unittest.TestCase):
    def test_prefix_prepended_when_present(self):
        out = gme.apply_prompt_prefix({"prompt_prefix": "Use the Atlassian MCP."}, "Question?")
        self.assertEqual(out, "Use the Atlassian MCP.\n\nQuestion?")

    def test_no_prefix_returns_prompt_unchanged(self):
        self.assertEqual(gme.apply_prompt_prefix({}, "Question?"), "Question?")
        self.assertEqual(gme.apply_prompt_prefix({"prompt_prefix": "  "}, "Question?"), "Question?")


class ServerIsolationValidityTest(unittest.TestCase):
    CFG = {
        "arms": {
            "glean": {
                "expected_mcp_servers": ["glean_default"],
                "forbidden_mcp_servers": ["plugin-atlassian-atlassian", "jira"],
            },
            "direct": {
                "expected_mcp_servers": ["atlassian"],
                "require_live_tool_servers": ["plugin-atlassian-atlassian"],
                "forbidden_mcp_servers": ["glean_default", "glean"],
            },
        }
    }

    @staticmethod
    def _run(servers):
        return {
            "success": True,
            "transcript": {
                "retrieval_attempted": bool(servers),
                "mcp_servers_used": servers,
                "models": {"Opus": 1},
            },
        }

    def test_clean_arms_have_no_isolation_flags(self):
        glean = self._run({"glean_default": 8})
        direct = self._run({"plugin-atlassian-atlassian": 6})
        self.assertEqual(gme.validity_flags(glean, direct, self.CFG), [])

    def test_direct_via_workspace_atlassian_server_is_clean(self):
        # After isolation the direct arm reaches the workspace mcp.json 'atlassian'
        # server (not the plugin); union(expected, require_live) must accept it.
        glean = self._run({"glean_default": 18})
        direct = self._run({"atlassian": 30})
        self.assertEqual(gme.validity_flags(glean, direct, self.CFG), [])

    def test_direct_reaching_glean_is_flagged(self):
        # The real contamination: 'direct' arm used glean_default (forbidden + unexpected).
        glean = self._run({"glean_default": 8})
        direct = self._run({"glean_default": 14})
        flags = gme.validity_flags(glean, direct, self.CFG)
        self.assertIn("direct_forbidden_server", flags)
        self.assertIn("direct_unexpected_server", flags)
        self.assertNotIn("glean_forbidden_server", flags)

    def test_glean_reaching_atlassian_is_flagged(self):
        glean = self._run({"glean_default": 12, "plugin-atlassian-atlassian": 8})
        direct = self._run({"plugin-atlassian-atlassian": 6})
        flags = gme.validity_flags(glean, direct, self.CFG)
        self.assertIn("glean_forbidden_server", flags)
        self.assertIn("glean_unexpected_server", flags)

    def test_no_cfg_preserves_legacy_behavior(self):
        glean = self._run({"glean_default": 8})
        direct = self._run({"glean_default": 14})
        self.assertEqual(gme.validity_flags(glean, direct), [])

    def test_no_retrieval_does_not_add_isolation_flag(self):
        empty = self._run({})
        flags = gme.validity_flags(empty, empty, self.CFG)
        self.assertIn("glean_no_retrieval", flags)
        self.assertNotIn("glean_unexpected_server", flags)


class NumericScoreNormalizationTest(unittest.TestCase):
    def test_coerce_score_clamps_and_parses(self):
        self.assertEqual(gme._coerce_score(4), 4)
        self.assertEqual(gme._coerce_score(4.6), 5)
        self.assertEqual(gme._coerce_score(9), 5)      # clamp high
        self.assertEqual(gme._coerce_score(0), 1)      # clamp low
        self.assertEqual(gme._coerce_score("4/5"), 4)  # parse from string
        self.assertIsNone(gme._coerce_score("n/a"))
        self.assertIsNone(gme._coerce_score(True))     # bools are not scores
        self.assertIsNone(gme._coerce_score(None))

    def test_flat_scores_are_clamped_in_place(self):
        bg = {"completeness_a": 7, "completeness_b": "3", "groundedness_a": 5, "groundedness_b": 4}
        gme._normalize_numeric_scores(bg)
        self.assertEqual(bg["completeness_a"], 5)
        self.assertEqual(bg["completeness_b"], 3)
        self.assertEqual(bg["groundedness_a"], 5)

    def test_nested_and_aliased_scores(self):
        # Cursor-style nested scores object with varied casing.
        bg = {"scores": {"completeness": {"a": 4, "b": 2}, "groundedness": {"A": 5, "B": 3}}}
        gme._normalize_numeric_scores(bg)
        self.assertEqual(bg["completeness_a"], 4)
        self.assertEqual(bg["completeness_b"], 2)
        self.assertEqual(bg["groundedness_a"], 5)
        self.assertEqual(bg["groundedness_b"], 3)

    def test_missing_scores_stay_absent(self):
        bg = {"winner": "A"}
        gme._normalize_numeric_scores(bg)
        self.assertNotIn("completeness_a", bg)
        self.assertNotIn("groundedness_b", bg)

    def test_judge_prompt_requests_integer_scores(self):
        meta = {"id": "Q1", "dept": "Eng", "prompt": "Question?"}
        run = {"_answer": "answer", "usage": {"input_tokens": 1, "output_tokens": 1}}
        prompt = gme.judge_prompt(meta, run, run)
        self.assertIn("completeness_a", prompt)
        self.assertIn("groundedness_b", prompt)
        self.assertIn("1-5", prompt)


class ServerPresentTest(unittest.TestCase):
    def test_exact_match_from_inventory(self):
        inv = {"servers": ["glean_default"]}
        mcp_list = {"servers_hint": [], "raw": None}
        self.assertTrue(gme.server_present("glean_default", inv, mcp_list))

    def test_no_substring_false_positive(self):
        # "teams" must NOT match "myteamspace-connector" (regression: raw substring).
        inv = {"servers": ["glean_default"]}
        mcp_list = {"servers_hint": [], "raw": {"stdout": "myteamspace-connector: connected"}}
        self.assertFalse(gme.server_present("teams", inv, mcp_list))

    def test_word_boundary_true_positive(self):
        inv = {"servers": []}
        mcp_list = {"servers_hint": [], "raw": {"stdout": "zoom: connected"}}
        self.assertTrue(gme.server_present("zoom", inv, mcp_list))


class WrapperTest(unittest.TestCase):
    def test_literal_braces_do_not_crash(self):
        wrapper = "Dept {dept} / {id}\n\nQ: {prompt}"
        row = {"Prompt": 'SELECT * FROM {schema}.t WHERE j={"a":1}', "ID": "Q1", "Dept": "Eng"}
        out = gme.render_wrapper(wrapper, row)
        self.assertIn("{schema}", out)
        self.assertIn('{"a":1}', out)
        self.assertIn("Dept Eng / Q1", out)


class BootstrapTest(unittest.TestCase):
    def test_constant_savings_gives_tight_ci(self):
        pairs = [(1.0, 2.0), (1.0, 2.0), (1.0, 2.0)]  # each 50% lower
        ci = gme.bootstrap_savings_ci(pairs)
        self.assertEqual(ci, (50.0, 50.0))

    def test_needs_two_rows(self):
        self.assertIsNone(gme.bootstrap_savings_ci([(1.0, 2.0)]))
        # zero/blank direct values are filtered out, leaving too few rows
        self.assertIsNone(gme.bootstrap_savings_ci([(1.0, 0.0), (1.0, "")]))


class HostAdapterTest(unittest.TestCase):
    def test_registry_has_both_hosts(self):
        self.assertEqual(gme.get_adapter("claude-code").name, "claude-code")
        self.assertEqual(gme.get_adapter("cursor").name, "cursor")
        self.assertEqual(gme.get_adapter(None).name, "claude-code")  # default

    def test_claude_command_shape(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = {"model": "opus"}
            arm = {"allowed_tools": ["mcp__glean_default__search"], "disallowed_tools": ["mcp__glean_default__run_tool"]}
            cmd, _ctx = gme.get_adapter("claude-code").build_command(root, cfg, arm, "hi", root / "out")
            self.assertEqual(cmd[0], "claude")
            self.assertIn("--allowedTools", cmd)
            self.assertIn("--disallowedTools", cmd)

    def test_cursor_command_and_readonly_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = {"model": "sonnet-4"}
            arm = {"allowed_tools": ["mcp__atlassian__search"], "disallowed_tools": ["mcp__atlassian__editJiraIssue"]}
            cmd, ctx = gme.get_adapter("cursor").build_command(root, cfg, arm, "hi", root / "out")
            self.assertEqual(cmd[0], "cursor-agent")
            self.assertIn("--workspace", cmd)
            self.assertIn("stream-json", cmd)
            # allow-list translated to Cursor rule grammar
            self.assertIn("Mcp(atlassian:search)", ctx["permissions"]["allow"])
            self.assertIn("Mcp(atlassian:editJiraIssue)", ctx["permissions"]["deny"])
            # read-only floor: writes/shell are always denied for an eval run
            self.assertIn("Write(**)", ctx["permissions"]["deny"])
            self.assertIn("Shell(**)", ctx["permissions"]["deny"])


if __name__ == "__main__":
    unittest.main()
