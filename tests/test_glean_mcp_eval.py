import json
import tempfile
import unittest
import zipfile
from unittest.mock import patch
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
            self.assertIn("Quality judging: **NOT RUN**", summary)
            self.assertIn("## MCP usage by row", summary)
            csv_text = (root / "results" / "aggregate_rows.csv").read_text(encoding="utf-8")
            self.assertIn("Q1", csv_text)

    def test_report_from_plugin_pair_uses_configured_arm_labels(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "golden_prompts.tsv").write_text("ID\\tDept\\tPrompt\\nQ1\\tEng\\tQuestion?\\n", encoding="utf-8")
            cfg = {
                "eval_name": "plugin-unit",
                "prompts_file": "golden_prompts.tsv",
                "results_dir": "results",
                "comparison": {
                    "treatment_arm": "treatment",
                    "control_arm": "control",
                    "treatment_label": "Glean plugin active",
                    "control_label": "Glean plugin inactive",
                    "subject_label": "plugin treatment",
                },
                "arms": {"treatment": {}, "control": {}},
            }
            cfg_path = root / "eval.config.json"
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            for arm, tokens, route in [("treatment", 100, "plugin"), ("control", 120, "mcp")]:
                d = root / "results" / "p1" / arm / "Q1"
                d.mkdir(parents=True)
                (d / "metadata.json").write_text(json.dumps({"id": "Q1", "dept": "Eng", "prompt": "Question?"}), encoding="utf-8")
                (d / "answer.md").write_text(f"{arm} answer", encoding="utf-8")
                run = {
                    "success": True,
                    "total_tokens": tokens,
                    "computed_cost_usd": tokens / 1_000_000,
                    "usage": {"input_tokens": tokens, "output_tokens": 0},
                    "transcript": {
                        "retrieval_attempted": True,
                        "models": {"m": 1},
                        "mcp_servers_used": {"plugin-glean-vnext-glean" if arm == "treatment" else "glean_default": 1},
                        "routing_outcome": route,
                    },
                }
                (d / "run.json").write_text(json.dumps(run), encoding="utf-8")
            rc = gme.main(["report", "--config", str(cfg_path)])
            self.assertEqual(rc, 0)
            summary = (root / "results" / "aggregate_summary.md").read_text(encoding="utf-8")
            self.assertIn("Glean plugin active", summary)
            self.assertIn("Glean plugin inactive", summary)
            csv_text = (root / "results" / "aggregate_rows.csv").read_text(encoding="utf-8")
            self.assertIn("treatment_total_tokens", csv_text)
            self.assertIn("plugin", csv_text)

    def test_report_negative_savings_wording_is_human_readable(self):
        self.assertEqual(gme.format_delta(-12.25), "12.2% higher for Glean")
        self.assertEqual(gme.format_delta(-8.0, positive_word="faster", negative_word="slower"), "8.0% slower for Glean")

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


class PromptValidationTest(unittest.TestCase):
    def test_malformed_tsv_row_fails_loudly(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg_path = root / "eval.config.json"
            cfg_path.write_text(json.dumps({"prompts_file": "golden_prompts.tsv", "arms": {"glean": {}}}), encoding="utf-8")
            (root / "golden_prompts.tsv").write_text(
                "ID\tDept\tPrompt\nQ1\tEng\tGood?\nQ2\tSales missing prompt tab\n",
                encoding="utf-8",
            )
            with self.assertRaises(gme.EvalError) as cm:
                gme.load_prompts(cfg_path, json.loads(cfg_path.read_text()))
            self.assertIn("Prompt file validation failed", str(cm.exception))
            self.assertIn("line 3", str(cm.exception))


class SetupDirectTest(unittest.TestCase):
    def test_setup_direct_filters_source_servers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "claude.json"
            source.write_text(json.dumps({
                "mcpServers": {
                    "glean_default": {"type": "http", "url": "https://glean.example/mcp"},
                    "slack": {"type": "http", "url": "https://slack.example/mcp"},
                    "atlassian": {"type": "http", "url": "https://atlassian.example/mcp"},
                }
            }), encoding="utf-8")
            config = root / "eval.config.json"
            config.write_text(json.dumps({
                "arms": {"direct": {"expected_mcp_servers": ["slack", "atlassian"]}}
            }), encoding="utf-8")
            output = root / "mcp" / "direct.mcp.json"
            rc = gme.main([
                "setup-direct",
                "--config", str(config),
                "--source", str(source),
                "--output", str(output),
            ])
            self.assertEqual(rc, 0)
            generated = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(sorted(generated["mcpServers"]), ["atlassian", "slack"])
            self.assertNotIn("glean_default", generated["mcpServers"])

    def test_setup_direct_fails_when_expected_server_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "claude.json"
            source.write_text(json.dumps({"mcpServers": {"slack": {}}}), encoding="utf-8")
            config = root / "eval.config.json"
            config.write_text(json.dumps({
                "arms": {"direct": {"expected_mcp_servers": ["slack", "github"]}}
            }), encoding="utf-8")
            rc = gme.main([
                "setup-direct",
                "--config", str(config),
                "--source", str(source),
            ])
            self.assertEqual(rc, 2)


class SetupCursorPluginTest(unittest.TestCase):
    def test_setup_cursor_plugin_copies_selected_global_servers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            (root / "prompts").mkdir()
            (root / "config" / "eval.config.cursor.plugin.example.json").write_text(json.dumps({
                "comparison": {"variant": "cursor-glean-plugin"},
                "prompts_file": "golden_prompts.tsv",
                "arms": {
                    "treatment": {"expected_mcp_servers": ["glean_default", "slack"]},
                    "control": {"expected_mcp_servers": ["glean_default", "slack"]},
                },
            }), encoding="utf-8")
            (root / "prompts" / "golden_prompts.cursor.plugin.example.tsv").write_text(
                "ID\tPrompt\nQ1\tQuestion?\n", encoding="utf-8"
            )
            source = root / "global-mcp.json"
            source.write_text(json.dumps({
                "mcpServers": {
                    "glean_default": {"type": "http", "url": "https://glean.example/mcp"},
                    "slack": {"type": "http", "url": "https://slack.example/mcp"},
                    "ambient": {"type": "http", "url": "https://ambient.example/mcp"},
                }
            }), encoding="utf-8")
            config = root / "eval.config.json"
            rc = gme.main([
                "setup-cursor-plugin",
                "--config", str(config),
                "--source", str(source),
            ])
            self.assertEqual(rc, 0)
            self.assertTrue(config.exists())
            self.assertTrue((root / "golden_prompts.tsv").exists())
            generated = json.loads((root / "mcp" / "plugin.shared.mcp.json").read_text())
            self.assertEqual(sorted(generated["mcpServers"]), ["glean_default", "slack"])
            self.assertNotIn("ambient", generated["mcpServers"])


class StrictMcpDiagnosticsTest(unittest.TestCase):
    def test_placeholder_mcp_config_fails_static_validation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mcp = root / "mcp" / "glean.mcp.json"
            mcp.parent.mkdir()
            mcp.write_text(json.dumps({
                "mcpServers": {
                    "glean_default": {
                        "type": "http",
                        "url": "https://<your-glean-subdomain>-be.glean.com/mcp/default",
                        "headers": {"Authorization": "Bearer <REPLACE_WITH_YOUR_GLEAN_MCP_TOKEN>"},
                    }
                }
            }), encoding="utf-8")
            cfg = {
                "mcp_mode": "strict",
                "arms": {
                    "glean": {
                        "mcp_config": "mcp/glean.mcp.json",
                        "expected_mcp_servers": ["glean_default"],
                    }
                },
            }
            static = gme.validate_static_setup(root, cfg, "glean")
            self.assertFalse(static["static_pass"])
            self.assertTrue(static["strict_config_diagnostics"]["errors"])


class BlindGradingTest(unittest.TestCase):
    def test_extended_grade_schema_contains_plugin_dimensions(self):
        schema = gme.grade_schema(extended=True)
        self.assertIn("accuracy_a", schema["properties"])
        self.assertIn("workflow_fit_winner", schema["required"])

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


class ValidityFlagsTest(unittest.TestCase):
    def test_builtin_tool_call_does_not_count_as_mcp_retrieval(self):
        glean = {"success": True, "transcript": {"retrieval_attempted": True, "mcp_servers_used": {}, "models": {"m": 1}}}
        direct = {"success": True, "transcript": {"retrieval_attempted": True, "mcp_servers_used": {"atlassian": 1}, "models": {"m": 1}}}
        self.assertIn("glean_no_mcp_retrieval", gme.validity_flags(glean, direct))
        self.assertNotIn("direct_no_mcp_retrieval", gme.validity_flags(glean, direct))


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

    def test_prompt_routing_instruction_uses_expected_evidence(self):
        cfg = {
            "prompt_wrapper": "Question: {prompt}",
            "prompt_routing_instruction": (
                "Use source-specific read-only MCPs for: {expected_evidence}."
            ),
        }
        row = {
            "Prompt": "Find the current issue status.",
            "ExpectedEvidence": "Jira, Confluence, Slack",
        }
        out = gme.render_prompt(cfg, row)
        self.assertTrue(out.startswith("Use source-specific read-only MCPs"))
        self.assertIn("Jira, Confluence, Slack", out)
        self.assertIn("Question: Find the current issue status.", out)

    def test_prompt_routing_instruction_is_opt_in(self):
        row = {"Prompt": "Question?"}
        self.assertEqual(gme.render_prompt({"prompt_wrapper": "{prompt}"}, row), "Question?")

    def test_prefetch_plan_and_verification_require_exact_tools(self):
        tools = [
            "mcp__Atlassian-MCP-Server__searchJiraIssuesUsingJql",
            "mcp__slack__slack_search_public_and_private",
        ]
        cfg = {
            "prefetch": {
                "enabled": True,
                "strict": True,
                "tool_plan_by_prompt": {"Q1": tools},
                "additional_tools_by_arm": {"treatment": ["mcp__plugin-Glean vNext-glean__search"]},
            }
        }
        self.assertEqual(
            gme.prefetch_tool_plan(cfg, "treatment", "Q1"),
            tools + ["mcp__plugin-Glean vNext-glean__search"],
        )
        prompt = gme.build_prefetch_prompt({"Prompt": "Find issue status."}, tools)
        self.assertIn(tools[0], prompt)
        self.assertIn(tools[1], prompt)
        record = {
            "success": True,
            "answer_text": "Evidence digest",
            "transcript": {
                "tool_calls": [
                    {"name": "mcp__Atlassian-MCP-Server__searchJiraIssuesUsingJql", "server": "Atlassian-MCP-Server"},
                    {"name": "mcp__slack__slack_search_public_and_private", "server": "slack"},
                ]
            },
        }
        verification = gme.verify_prefetch_record(record, tools)
        self.assertTrue(verification["passed"])
        injected = gme.inject_prefetch_evidence(
            "Question?", record, verification, "Use the digest and do not repeat broad searches."
        )
        self.assertIn("Evidence digest", injected)
        self.assertIn("do not repeat broad searches", injected)

    def test_prefetch_verification_reports_missing_and_unexpected_tools(self):
        required = ["mcp__Atlassian-MCP-Server__searchJiraIssuesUsingJql"]
        record = {
            "success": True,
            "transcript": {
                "tool_calls": [
                    {"name": "mcp__slack__slack_search_public", "server": "slack"},
                ]
            },
        }
        verification = gme.verify_prefetch_record(record, required)
        self.assertFalse(verification["passed"])
        self.assertEqual(verification["missing_tools"], required)
        self.assertEqual(verification["unexpected_mcp_tools"], ["mcp__slack__slack_search_public"])
        self.assertEqual(verification["unexpected_tools"], ["mcp__slack__slack_search_public"])

    def test_prefetch_metrics_are_added_without_changing_answer_routing(self):
        record = {
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "duration_ms_reported_by_claude": 100,
            "transcript": {
                "tool_calls": [{"name": "mcp__glean_default__search", "server": "glean_default"}],
                "mcp_servers_used": {"glean_default": 1},
                "routing_outcome": "mcp",
                "plugin_servers_used": {},
            },
        }
        prefetch = {
            "usage": {"input_tokens": 20, "output_tokens": 7},
            "duration_ms": 300,
            "transcript": {
                "tool_calls": [{"name": "mcp__slack__slack_search_public", "server": "slack"}],
                "mcp_servers_used": {"slack": 1},
            },
        }
        verification = {"passed": True}
        gme.merge_prefetch_into_record(record, prefetch, verification, {"pricing_per_million": {}})
        self.assertEqual(record["duration_ms_reported_by_claude"], 400)
        self.assertEqual(record["usage"]["input_tokens"], 30)
        self.assertEqual(record["transcript"]["mcp_servers_used"], {"slack": 1, "glean_default": 1})
        self.assertEqual(record["transcript"]["routing_outcome"], "mcp")
        self.assertEqual(record["transcript"]["prefetch_mcp_servers_used"], {"slack": 1})
        self.assertEqual(verification["tool_call_count"], 1)

    def test_synthesis_arm_config_can_disable_answer_mcp_tools(self):
        arm = {
            "allowed_tools": ["mcp__glean_default__search"],
            "disallowed_tools": ["mcp__glean_default__run_tool"],
        }
        answer = gme.synthesis_arm_config({"prefetch": {"answer_mcp_tools": "none"}}, arm)
        self.assertEqual(answer["allowed_tools"], [])
        self.assertIn("mcp__glean_default__search", answer["disallowed_tools"])
        self.assertEqual(arm["allowed_tools"], ["mcp__glean_default__search"])


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
    def test_plugin_comparison_spec_and_pairing_defaults(self):
        spec = gme.comparison_spec({
            "arms": {
                "treatment": {"label": "Plugin on"},
                "control": {"label": "Plugin off"},
            },
            "comparison": {
                "treatment_arm": "treatment",
                "control_arm": "control",
            },
        })
        self.assertEqual(spec["treatment_arm"], "treatment")
        self.assertEqual(spec["control_arm"], "control")
        self.assertFalse(spec["legacy"])

    def test_cursor_plugin_dir_command_and_routing_harvest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plugin_dir = root / "glean-vnext"
            (plugin_dir / ".cursor-plugin").mkdir(parents=True)
            (plugin_dir / ".cursor-plugin" / "plugin.json").write_text(json.dumps({
                "name": "glean-vnext", "version": "0.2.42"
            }), encoding="utf-8")
            cfg = {
                "model": "sonnet-4",
                "cursor_plugin": {
                    "plugin_id": "glean-vnext",
                    "server_identifier": "plugin-Glean vNext-glean",
                    "activation_mode": "plugin-dir",
                    "plugin_dir": str(plugin_dir),
                    "manual_confirmation": False,
                    "glean_mcp_server_identifiers": ["glean_default"],
                },
            }
            arm = {
                "plugin_state": "enabled",
                "allowed_tools": ["mcp__plugin-glean-vnext-glean__find_skills"],
            }
            adapter = cursor_host.CursorAdapter()
            cmd, ctx = adapter.build_command(root, cfg, arm, "hi", root / "out")
            self.assertIn("--plugin-dir", cmd)
            self.assertIn(str(plugin_dir), cmd)
            self.assertIn("Mcp(plugin-Glean vNext-glean:find_skills)", ctx["permissions"]["allow"])
            stdout = "\n".join([
                json.dumps({
                    "type": "tool_call", "call_id": "1",
                    "tool_call": {"mcpToolCall": {"args": {
                        "providerIdentifier": "plugin-Glean vNext-glean", "toolName": "find_skills"
                    }}},
                }),
                json.dumps({
                    "type": "tool_call", "call_id": "2",
                    "tool_call": {"mcpToolCall": {"args": {
                        "providerIdentifier": "glean_default", "toolName": "search"
                    }}},
                }),
                json.dumps({"type": "result", "result": "ok", "duration_ms": 12}),
            ])
            harvested = adapter.harvest(
                {"stdout": stdout, "returncode": 0}, root, root / "out", cfg, ctx
            )
            transcript = harvested["transcript"]
            self.assertEqual(transcript["routing_outcome"], "mixed")
            self.assertEqual(transcript["plugin_tool_call_count"], 1)
            self.assertTrue(transcript["routing_confounded"])

    def test_manual_plugin_checkpoint_fails_closed_without_retry(self):
        class NonInteractive:
            def isatty(self):
                return False

        with patch.object(cursor_host.sys, "stdin", NonInteractive()):
            with self.assertRaises(cursor_host.HostSetupError):
                cursor_host._manual_plugin_checkpoint(
                    "control", "disabled", {"disable_instruction": "uninstall the plugin"}
                )

    def test_cursor_control_command_does_not_load_plugin_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = {
                "cursor_plugin": {
                    "plugin_id": "glean-vnext",
                    "activation_mode": "plugin-dir",
                    "plugin_dir": str(root / "plugin"),
                }
            }
            arm = {"plugin_state": "disabled"}
            cmd, _ctx = cursor_host.CursorAdapter().build_command(root, cfg, arm, "hi", root / "out")
            self.assertNotIn("--plugin-dir", cmd)

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
