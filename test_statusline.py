"""Tests for statusline.py. Run: python -m unittest test_statusline"""

import os
import pathlib
import re
import sys
import time
import unittest
from unittest import mock

import statusline as sl

NOW = 1_800_000_000.0
HOUR = 3600
ANSI = re.compile(r"\033\[[0-9;]*m")


def plain(s):
    return ANSI.sub("", s)


def stdin_rl(five=None, seven=None):
    """rate_limits as Claude Code pipes it: epoch-second resets_at."""
    rl = {}
    if five:
        rl["five_hour"] = {"used_percentage": five[0], "resets_at": five[1]}
    if seven:
        rl["seven_day"] = {"used_percentage": seven[0], "resets_at": seven[1]}
    return rl


def live(five=None, seven=None):
    """Body of the /api/oauth/usage endpoint: ISO resets_at, fractional seconds."""
    from datetime import datetime, timezone

    def win(w):
        if not w:
            return None
        iso = datetime.fromtimestamp(w[1], timezone.utc).isoformat()
        return {"utilization": w[0], "resets_at": iso}

    return {"five_hour": win(five), "seven_day": win(seven)}


class MergeUsage(unittest.TestCase):
    def test_other_session_usage_wins_over_stale_stdin(self):
        # This session last heard 40%, but other sessions pushed the shared quota to 56%.
        got = sl.merge_usage(stdin_rl(five=(40, NOW + HOUR)), live(five=(56, NOW + HOUR - 0.4)), NOW)
        self.assertEqual(got["five_hour"]["pct"], 56)

    def test_fresher_stdin_wins_over_cached_live(self):
        # A reply landed after the cache was written.
        got = sl.merge_usage(stdin_rl(five=(58, NOW + HOUR)), live(five=(56, NOW + HOUR)), NOW)
        self.assertEqual(got["five_hour"]["pct"], 58)

    def test_new_window_beats_old_window(self):
        # stdin still describes the window that already reset; live has the new one.
        got = sl.merge_usage(stdin_rl(five=(55, NOW - 60)), live(five=(3, NOW + 5 * HOUR)), NOW)
        self.assertEqual(got["five_hour"]["pct"], 3)

    def test_expired_window_with_no_replacement_is_unknown(self):
        # The window reset and no newer reading exists: show "--", not a made-up 0%.
        got = sl.merge_usage(stdin_rl(five=(55, NOW - 60)), None, NOW)
        self.assertIsNone(got["five_hour"])

    def test_expired_window_renders_as_unknown(self):
        usage = sl.merge_usage(stdin_rl(five=(55, NOW - 60), seven=(40, NOW + HOUR)), None, NOW)
        out = plain(sl.format_statusline({"model": {"display_name": "Opus"}}, usage=usage, now=NOW))
        self.assertRegex(out, r"5h\s+--")
        self.assertNotRegex(out, r"5h 0%")

    def test_no_data_is_unknown_not_zero(self):
        got = sl.merge_usage({}, None, NOW)
        self.assertIsNone(got["five_hour"])
        self.assertIsNone(got["seven_day"])

    def test_windows_merge_independently(self):
        got = sl.merge_usage(stdin_rl(seven=(48, NOW + 5 * 24 * HOUR)), live(five=(12, NOW + HOUR)), NOW)
        self.assertEqual(got["five_hour"]["pct"], 12)
        self.assertEqual(got["seven_day"]["pct"], 48)


class Render(unittest.TestCase):
    def test_fresh_session_shows_unknown_not_fake_zero(self):
        out = plain(sl.format_statusline({"model": {"display_name": "Opus"}}, usage=None, now=NOW))
        self.assertNotIn("5h:0%", out)
        self.assertRegex(out, r"5h\s+--")
        self.assertRegex(out, r"7d\s+--")

    def test_shows_context_tokens_used_of_window_size(self):
        cw = {
            "context_window_size": 1_000_000,
            "used_percentage": 14,
            "current_usage": {"input_tokens": 2, "output_tokens": 148,
                              "cache_creation_input_tokens": 787, "cache_read_input_tokens": 134_488},
        }
        out = plain(sl.format_statusline({"context_window": cw}, now=NOW, show_usage=False))
        self.assertRegex(out, r"ctx 14% \S+ 135k/1M")

    def test_no_fake_zero_tokens_before_first_reply(self):
        for usage in (None, {}):
            cw = {"context_window_size": 200_000, "used_percentage": 0, "current_usage": usage}
            out = plain(sl.format_statusline({"context_window": cw}, now=NOW, show_usage=False))
            self.assertRegex(out, r"ctx 0% \S+ │ \$0.00$")
            self.assertNotIn("/200k", out)

    def test_ctx_is_unknown_without_a_window_size_or_percentage(self):
        for data in ({}, {"context_window": None}, {"context_window": {}},
                     {"context_window": {"current_usage": {"input_tokens": 5}}},
                     {"context_window": {"context_window_size": None, "used_percentage": None}}):
            out = plain(sl.format_statusline(data, now=NOW, show_usage=False))
            self.assertIn("ctx --", out, data)
            self.assertNotIn("0%", out, data)
            self.assertNotIn(sl.BAR, out, data)
            self.assertNotIn("/", out, data)

    def test_ctx_is_zero_when_the_window_size_is_known_but_nothing_was_used_yet(self):
        for cw in ({"context_window_size": 1_000_000, "current_usage": {}},
                   {"context_window_size": 1_000_000, "used_percentage": None},
                   {"context_window_size": 1_000_000}):
            out = plain(sl.format_statusline({"context_window": cw}, now=NOW, show_usage=False))
            self.assertIn("ctx 0%", out, cw)
            self.assertNotIn("/1M", out, cw)
        # tokens without a percentage: the percentage is worked out from them
        cw = {"context_window_size": 200_000, "current_usage": {"input_tokens": 50_000}}
        out = plain(sl.format_statusline({"context_window": cw}, now=NOW, show_usage=False))
        self.assertIn("ctx 25%", out)
        self.assertIn("50k/200k", out)

    def test_ctx_with_a_window_but_no_usage_has_a_percentage_and_no_tokens(self):
        cw = {"context_window_size": 1_000_000, "used_percentage": 14, "current_usage": None}
        out = plain(sl.format_statusline({"context_window": cw}, now=NOW, show_usage=False))
        self.assertIn(f"ctx 14% {plain(sl.bar(14))} │", out)

    def test_ctx_label_takes_the_percentage_color_like_usage(self):
        raw = sl.format_statusline({"context_window": {"used_percentage": 43}}, now=NOW, show_usage=False)
        c = sl.color_pct(43)
        self.assertIn(f"{c}ctx{sl.RST} {c}43%{sl.RST}", raw)

    def test_duration_is_labelled_as_session_time(self):
        data = {"cost": {"total_duration_ms": 5_580_000}}
        out = plain(sl.format_statusline(data, now=NOW, show_usage=False))
        self.assertIn("session 1h33m", out)
        self.assertEqual(plain(sl.session_time({"cost": {"total_duration_ms": 0}})), "")

    def test_reset_countdown_is_an_arrow_and_at_most_four_characters(self):
        cases = {5 * 24 * HOUR: "→5d", 5 * 24 * HOUR + 2 * HOUR: "→5d", 3 * HOUR: "→3h", 3 * HOUR + 5 * 60: "→3h",
                 2 * HOUR + 10 * 60: "→2h", 45 * 60: "→45m", 30: "→1m", 0: "", None: ""}
        self.assertEqual({s: sl.fmt_reset(s) for s in cases}, cases)
        self.assertTrue(all(len(v) <= 4 for v in cases.values()))

    def test_cost_always_has_two_decimals(self):
        self.assertEqual([plain(sl.fmt_cost(c)) for c in (47.5, 0.37, 120)], ["$47.50", "$0.37", "$120.00"])

    def test_usage_colors_come_in_four_stages(self):
        green, yellow, orange, red = sl.fg(114), sl.fg(221), sl.fg(208), sl.fg(203)
        cases = {0: green, 49: green, 50: yellow, 69: yellow, 70: orange, 84: orange,
                 85: red, 99: red, 100: red}
        self.assertEqual({p: sl.color_pct(p) for p in cases}, cases)
        self.assertEqual(sl.RED, red)

    def test_shows_effort_level_after_model(self):
        data = {"model": {"display_name": "Opus"}, "effort": {"level": "xhigh"}}
        out = plain(sl.format_statusline(data, now=NOW, show_usage=False))
        self.assertTrue(out.startswith("Opus · xhigh │"), out)

    def test_effort_is_dim_up_to_high_and_purple_beyond(self):
        for level in ("low", "medium", "high"):
            self.assertEqual(sl.EFFORT[level], sl.DIM, level)
        for level in ("xhigh", "max"):
            self.assertEqual(sl.EFFORT[level], sl.fg(141), level)
            self.assertNotIn(sl.EFFORT[level], (sl.fg(203), sl.fg(208), sl.fg(221), sl.fg(114)))

    def test_effort_omitted_when_absent(self):
        out = plain(sl.format_statusline({"model": {"display_name": "Opus"}}, now=NOW, show_usage=False))
        self.assertTrue(out.startswith("Opus │"), out)

    def test_token_counts_are_compact(self):
        self.assertEqual([sl.fmt_tokens(n) for n in (0, 950, 1_000, 12_345, 135_277, 200_000, 1_000_000, 1_250_000)],
                         ["0", "950", "1k", "12k", "135k", "200k", "1M", "1.2M"])

    def test_reset_countdown_shows_only_from_70_percent(self):
        usage = sl.merge_usage({}, live(five=(74, NOW + 2 * HOUR + 10 * 60), seven=(48, NOW + 5 * 24 * HOUR)), NOW)
        raw = sl.format_statusline({"model": {"display_name": "Opus"}}, usage=usage, now=NOW)
        out = plain(raw)
        self.assertRegex(out, "5h 74% \\S+ →2h │")
        self.assertRegex(out, "7d 48% \\S+ │")
        self.assertNotIn("5d", out)
        self.assertIn(f"{sl.bar(74)} {sl.DIM}→2h{sl.RST}", raw)
        usage = sl.merge_usage({}, live(five=(69, NOW + HOUR), seven=(70, NOW + 3 * 24 * HOUR)), NOW)
        out = plain(sl.format_statusline({"model": {"display_name": "Opus"}}, usage=usage, now=NOW))
        self.assertNotIn("5h 69% █████ →", out)
        self.assertIn("7d 70% █████ →3d", out)

    def test_the_countdown_stays_with_its_meter_even_without_the_bar(self):
        usage = sl.merge_usage({}, live(five=(90, NOW + 45 * 60), seven=(10, NOW + HOUR)), NOW)
        data = {"model": {"display_name": "Opus"}}
        wide = plain(sl.format_statusline(data, usage, NOW))
        self.assertIn("5h 90% █████ →45m", wide)
        for width in range(sl.visible_width(wide) - 1, 38, -1):
            out = plain(sl.format_statusline(data, usage, NOW, width=width))
            self.assertIn("5h 90% ", out)
            self.assertIn("→45m", out, (width, out))
            self.assertLessEqual(sl.visible_width(out), width, out)

    def test_usage_label_takes_the_percentage_color(self):
        usage = sl.merge_usage({}, live(five=(56, NOW + HOUR)), NOW)
        raw = sl.format_statusline({"model": {"display_name": "Opus"}}, usage=usage, now=NOW)
        c = sl.color_pct(56)
        self.assertIn(f"{c}5h{sl.RST} {c}56%{sl.RST} {sl.bar(56)}", raw)


class VersionStatus(unittest.TestCase):
    def test_up_to_date(self):
        self.assertEqual(sl.version_status("2.1.296", ["2.1.295", "2.1.296"], "2.1.296"), ("ok", None))

    def test_newer_build_installed_needs_restart(self):
        self.assertEqual(sl.version_status("2.1.290", ["2.1.290", "2.1.296"], "2.1.296"), ("restart", "2.1.296"))

    def test_newer_release_published(self):
        self.assertEqual(sl.version_status("2.1.290", ["2.1.290"], "2.1.296"), ("update", "2.1.296"))

    def test_compares_numerically_not_as_text(self):
        self.assertEqual(sl.version_status("2.1.99", [], "2.1.100"), ("update", "2.1.100"))

    def test_unknown_when_nothing_to_compare(self):
        self.assertEqual(sl.version_status("2.1.296", [], None), ("unknown", None))


class PluginsNeedingReload(unittest.TestCase):
    INSTALLED = {"plugins": {
        "a@m": [{"lastUpdated": "2026-10-09T16:00:00Z"}],
        "b@m": [{"lastUpdated": "2026-10-09T18:00:00Z"}],
        "off@m": [{"lastUpdated": "2026-10-09T18:00:00Z"}],
    }}

    def test_enabled_plugins_updated_after_load(self):
        loaded = sl._epoch("2026-10-09T17:00:00Z")
        self.assertEqual(sl.plugins_needing_reload(self.INSTALLED, {"a@m", "b@m"}, loaded), ["b@m"])

    def test_none_after_reload(self):
        loaded = sl._epoch("2026-10-09T19:00:00Z")
        self.assertEqual(sl.plugins_needing_reload(self.INSTALLED, {"a@m", "b@m"}, loaded), [])


class PluginsLoadedAt(unittest.TestCase):
    """plugins_loaded_at scans only what a transcript gained since the last scan."""

    RELOAD = b"<command-name>/reload-plugins</command-name>"

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "t.jsonl"

    def line(self, ts, text="hi"):
        import json
        from datetime import datetime, timezone
        iso = datetime.fromtimestamp(ts, timezone.utc).isoformat()
        return json.dumps({"timestamp": iso, "message": text}).encode() + b"\n"

    def append(self, data):
        with open(self.path, "ab") as f:
            f.write(data)

    def test_full_scan_without_state_is_unchanged(self):
        self.path.write_bytes(self.line(NOW) + self.line(NOW + 5, self.RELOAD.decode()) + self.line(NOW + 9))
        self.assertEqual(sl.plugins_loaded_at(self.path), NOW + 5)
        self.path.write_bytes(self.line(NOW) + self.line(NOW + 9))
        self.assertEqual(sl.plugins_loaded_at(self.path), NOW)
        self.assertIsNone(sl.plugins_loaded_at(self.path.with_name("missing.jsonl")))
        self.assertIsNone(sl.plugins_loaded_at(None))

    def test_next_scan_reads_only_the_appended_bytes(self):
        state = {}
        self.path.write_bytes(self.line(NOW) + self.line(NOW + 5, self.RELOAD.decode()))
        self.assertEqual(sl.plugins_loaded_at(self.path, state), NOW + 5)
        self.assertEqual(state["size"], self.path.stat().st_size)
        # Blank out the old marker without changing the size: only a rescan would notice.
        raw = self.path.read_bytes().replace(self.RELOAD, b"x" * len(self.RELOAD))
        self.path.write_bytes(raw)
        self.assertEqual(sl.plugins_loaded_at(self.path, state), NOW + 5)
        self.append(self.line(NOW + 20))
        self.assertEqual(sl.plugins_loaded_at(self.path, state), NOW + 5)
        self.append(self.line(NOW + 30, self.RELOAD.decode()))
        self.assertEqual(sl.plugins_loaded_at(self.path, state), NOW + 30)
        self.assertEqual(state["size"], self.path.stat().st_size)

    def test_a_half_written_last_line_is_picked_up_once_complete(self):
        state = {}
        self.path.write_bytes(self.line(NOW))
        sl.plugins_loaded_at(self.path, state)
        full = self.line(NOW + 7, self.RELOAD.decode())
        self.append(full[:25])
        self.assertEqual(sl.plugins_loaded_at(self.path, state), NOW)
        self.append(full[25:])
        self.assertEqual(sl.plugins_loaded_at(self.path, state), NOW + 7)

    def test_shrunk_or_replaced_transcript_is_rescanned(self):
        state = {}
        self.path.write_bytes(self.line(NOW) + self.line(NOW + 5, self.RELOAD.decode()) + self.line(NOW + 9))
        self.assertEqual(sl.plugins_loaded_at(self.path, state), NOW + 5)
        self.path.write_bytes(self.line(NOW + 100))  # shorter: a new session file
        self.assertEqual(sl.plugins_loaded_at(self.path, state), NOW + 100)
        other = self.path.with_name("other.jsonl")
        other.write_bytes(self.line(NOW + 200) + self.line(NOW + 201) + self.line(NOW + 202))
        self.assertEqual(sl.plugins_loaded_at(other, state), NOW + 200)  # same-or-larger size, other path

    def test_health_probe_keeps_the_scan_state_in_the_probe_cache(self):
        import tempfile
        from unittest import mock
        self.path.write_bytes(self.line(NOW))
        data = {"session_id": "s", "version": "2.1.296", "transcript_path": str(self.path)}
        cache_path = pathlib.Path(self.tmp.name) / sl.PROBE_CACHE_NAME
        seen, real = [], sl.plugins_loaded_at

        def spy(path, state=None):
            seen.append(dict(state or {}))
            return real(path, state)

        with mock.patch.object(sl, "read_mcp_logs", return_value=None), \
                mock.patch.object(sl, "CLAUDE_DIR", pathlib.Path(self.tmp.name)), \
                mock.patch.object(sl, "NATIVE_VERSIONS", pathlib.Path(self.tmp.name) / "v"), \
                mock.patch.object(sl, "plugins_loaded_at", spy):
            cache = sl.ProbeCache(cache_path, NOW)
            sl._health_probe(data, cache)
            cache.save()
            first_size = self.path.stat().st_size
            self.append(self.line(NOW + 3))
            cache = sl.ProbeCache(cache_path, NOW + 11)
            sl._health_probe(data, cache)
        self.assertEqual(seen[0], {})
        self.assertEqual(seen[1]["path"], str(self.path))
        self.assertEqual(seen[1]["size"], first_size)


class McpStatus(unittest.TestCase):
    SID = "s1"

    def ev(self, msg, sid="s1", key="debug", ts="2026-10-09T19:00:00Z"):
        return {key: msg, "sessionId": sid, "timestamp": ts}

    def test_last_connection_event_for_this_session_wins(self):
        logs = {
            "plugin-discord-discord": [self.ev("Connection failed (CONNECTION_CLOSED): Connection closed", key="error")],
            "claude-ai-Gmail": [self.ev("Successfully connected (transport: claudeai-proxy) in 4ms")],
            "plugin-resend-resend": [
                self.ev("Connection failed (400): bad request", key="error", ts="2026-10-09T19:00:00Z"),
                self.ev("Successfully connected", ts="2026-10-09T19:05:00Z"),
            ],
            "plugin-telegram-telegram": [self.ev("Connection failed", sid="other", key="error")],
        }
        ok, failed = sl.mcp_status(logs, self.SID)
        self.assertEqual(sorted(ok), ["Gmail", "resend"])
        self.assertEqual(failed, ["discord"])

    def test_tool_errors_after_connecting_do_not_count(self):
        logs = {"plugin-x-x": [self.ev("Successfully connected"),
                               self.ev("Tool call failed: timeout", key="error", ts="2026-10-09T19:09:00Z")]}
        self.assertEqual(sl.mcp_status(logs, self.SID), (["x"], []))


class SkillCount(unittest.TestCase):
    def test_counts_enabled_plugin_skills_commands_and_user_skills(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)

            def skill(d):
                d.mkdir(parents=True)
                (d / "SKILL.md").write_text("x")

            listed = root / "listed"  # manifest names its skills: only those load
            (listed / ".claude-plugin").mkdir(parents=True)
            (listed / ".claude-plugin/plugin.json").write_text(json.dumps({"skills": ["./skills/a/one", "./skills/a/two"]}))
            for n in ("one", "two", "unlisted"):
                skill(listed / "skills/a" / n)
            plain_p = root / "plain"  # no list: each skills/<name>/SKILL.md, plus commands
            skill(plain_p / "skills/x")
            (plain_p / "commands").mkdir()
            (plain_p / "commands/do.md").write_text("x")
            off = root / "off"
            skill(off / "skills/y")
            installed = {"plugins": {"listed@m": [{"installPath": str(listed)}],
                                     "plain@m": [{"installPath": str(plain_p)}],
                                     "off@m": [{"installPath": str(off)}]}}
            user = root / "claude"
            skill(user / "skills/mine")
            project = root / "proj"
            skill(project / ".claude/skills/local")
            n = sl.count_skills(installed, {"listed@m", "plain@m"}, user, project)
            self.assertEqual(n, 2 + 2 + 1 + 1)  # listed 2, plain skill + command, user 1, project 1


class McpLogFiles(unittest.TestCase):
    def test_finds_this_session_behind_newer_files_of_other_sessions(self):
        import json, os, tempfile
        with tempfile.TemporaryDirectory() as root:
            d = pathlib.Path(root) / "mcp-logs-plugin-x-x"
            d.mkdir()
            ours = d / "2026-10-09T19-00-00-000Z.jsonl"
            ours.write_text(json.dumps({"debug": "Successfully connected", "sessionId": "me",
                                        "timestamp": "2026-10-09T19:00:01Z"}) + "\n")
            for i in range(5):
                f = d / f"2026-10-09T20-0{i}-00-000Z.jsonl"
                f.write_text(json.dumps({"debug": "Successfully connected", "sessionId": f"other{i}"}) + "\n")
            logs = sl.read_mcp_logs(root)
            self.assertEqual(sl.mcp_status(logs, "me"), (["x"], []))

    def test_only_connection_events_are_kept(self):
        # The scan is shared through the probe cache file, so it keeps only what mcp_status reads.
        import json, tempfile
        with tempfile.TemporaryDirectory() as root:
            d = pathlib.Path(root) / "mcp-logs-plugin-x-x"
            d.mkdir()
            (d / "a.jsonl").write_text("\n".join(json.dumps(e) for e in (
                {"debug": "Successfully connected (transport: stdio) in 4ms", "sessionId": "me", "timestamp": "1"},
                {"error": "Tool call failed: timeout", "sessionId": "me", "timestamp": "2"},
                {"debug": "Received 400 tools", "sessionId": "me", "timestamp": "3", "big": "x" * 5000},
                {"error": "Connection failed (CLOSED): closed", "sessionId": "me", "timestamp": "4"})) + "\n")
            logs = sl.read_mcp_logs(root)
        self.assertEqual([e["timestamp"] for e in logs["plugin-x-x"]], ["1", "4"])
        self.assertEqual(sl.read_mcp_logs(None), None)
        self.assertEqual(sl.read_mcp_logs(os.path.join(root, "gone")), None)


class McpNames(unittest.TestCase):
    def test_display_names(self):
        known = {"chrome-devtools-mcp", "discord"}
        got = [sl._mcp_name(n, known) for n in
               ("plugin-discord-discord", "plugin-chrome-devtools-mcp-chrome-devtools", "claude-ai-Google-Drive", "bigquery")]
        self.assertEqual(got, ["discord", "chrome-devtools", "Google-Drive", "bigquery"])


class HealthMarks(unittest.TestCase):
    """health_marks() -> [(text, level, word)]: one '✓ mcp 3 · 2.1.296' cluster when all is well,
    else one item per problem. Levels: OK 0 < RELOAD (grey) < UPDATE (amber) < FAILED (red)."""

    def tearDown(self):
        sl.set_icons("none")

    def texts(self, *args):
        return [(plain(t), lvl, word) for t, lvl, word in sl.health_marks(*args)]

    def test_levels_are_ordered_by_severity(self):
        self.assertEqual((sl.OK, sl.RELOAD, sl.UPDATE, sl.FAILED), (0, 1, 2, 3))

    def test_all_good_is_one_cluster(self):
        got = self.texts("2.1.296", ("ok", None), [], (["a"] * 3, []))
        self.assertEqual(got, [("✓ mcp 3 · 2.1.296", sl.OK, "")])

    def test_the_cluster_is_a_green_tick_and_dim_text(self):
        (text, _, _), = sl.health_marks("2.1.296", ("ok", None), [], (["a"], []))
        self.assertEqual(text, f"{sl.GREEN}✓{sl.RST} {sl.DIM}mcp 1 · 2.1.296{sl.RST}")

    def test_the_cluster_shows_what_it_knows(self):
        self.assertEqual(self.texts("2.1.296", ("ok", None), None, None), [("✓ 2.1.296", 0, "")])
        self.assertEqual(self.texts(None, ("unknown", None), None, (["a", "b"], [])), [("✓ mcp 2", 0, "")])
        self.assertEqual(self.texts("2.1.296", ("unknown", None), [], None), [("✓ 2.1.296", 0, "")])  # plugins checked

    def test_a_version_nothing_vouches_for_has_no_tick(self):
        self.assertEqual(self.texts("2.1.296", ("unknown", None), None, None), [("2.1.296", 0, "")])

    def test_marks_without_data_are_hidden(self):
        self.assertEqual(sl.health_marks(None, ("unknown", None), None, None), [])
        self.assertEqual(sl.health_marks(None, ("unknown", None), None, ([], [])), [])

    def test_failed_server_is_named_in_red(self):
        marks = sl.health_marks("2.1.296", ("ok", None), [], (["a"] * 13, ["Gmail"]))
        self.assertEqual([(plain(t), lvl, w) for t, lvl, w in marks], [("▲ Gmail", sl.FAILED, "Gmail")])
        self.assertTrue(marks[0][0].startswith(sl.RED))
        got = self.texts(None, ("unknown", None), None, ([], ["Gmail", "x", "y"]))
        self.assertEqual(got, [("▲ Gmail +2", sl.FAILED, "Gmail")])

    def test_problems_replace_the_cluster(self):
        got = self.texts("2.1.296", ("ok", None), [], (["a"] * 13, ["Gmail"]))
        self.assertNotIn("mcp 13", "".join(t for t, _, _ in got))
        self.assertNotIn("✓", "".join(t for t, _, _ in got))

    def test_long_server_names_are_shortened(self):
        got = self.texts(None, ("unknown", None), None, (["a"], ["a-very-long-server-name-x"]))
        self.assertEqual(got[0][0], "▲ a-ver…name-x")

    def test_update_and_restart_are_amber(self):
        marks = sl.health_marks("2.1.290", ("update", "2.2.0"), None, None)
        self.assertEqual([(plain(t), lvl, w) for t, lvl, w in marks], [("↑ 2.2.0", sl.UPDATE, "2.2.0")])
        self.assertTrue(marks[0][0].startswith(sl.AMBER))
        self.assertEqual(self.texts("2.1.290", ("update", None), None, None), [("↑ update", sl.UPDATE, "update")])
        self.assertEqual(self.texts("2.1.290", ("restart", "2.1.296"), None, None),
                         [("↑ restart", sl.UPDATE, "restart")])

    def test_plugins_to_reload_are_grey_advice(self):
        marks = sl.health_marks(None, ("unknown", None), ["p1", "p2"], None)
        self.assertEqual([(plain(t), lvl, w) for t, lvl, w in marks], [("~reload", sl.RELOAD, "reload")])
        self.assertTrue(marks[0][0].startswith(sl.DIM))
        self.assertNotIn(sl.AMBER, marks[0][0])

    def test_every_problem_is_its_own_item_in_order(self):
        got = self.texts("2.1.290", ("update", "2.2.0"), ["p"], (["a"], ["Gmail"]))
        self.assertEqual(got, [("▲ Gmail", sl.FAILED, "Gmail"), ("↑ 2.2.0", sl.UPDATE, "2.2.0"),
                               ("~reload", sl.RELOAD, "reload")])

    def test_nerd_marks_use_nerd_icons(self):
        sl.set_icons("nerd")
        got = self.texts("2.1.296", ("ok", None), [], (["a"], []))
        self.assertEqual([t for t, _, _ in got], [f"{sl.ICON['ok']} mcp 1 · 2.1.296"])
        got = self.texts("2.1.290", ("update", "2.2"), ["p"], (["a"], ["Gmail"]))
        self.assertEqual([t for t, _, _ in got], [f"{sl.ICON['warn']} Gmail", f"{sl.ICON['up']} 2.2",
                                                   f"{sl.ICON['reload']} reload"])
        self.assertNotEqual(sl.ICON["reload"], sl.ICON["up"])


class ModelColors(unittest.TestCase):
    def test_each_family_has_its_own_color(self):
        fams = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"]
        colors = [sl.model_color(m) for m in fams]
        self.assertEqual(len(set(colors)), 4)
        self.assertEqual(sl.model_color("claude-opus-4-6"), sl.model_color("claude-opus-5-5[1m]"))
        self.assertEqual(sl.model_color("something-else"), sl.MODEL)
        self.assertEqual(sl.model_color(None), sl.MODEL)

    def test_opus_is_terracotta_and_no_family_borrows_a_stage_color(self):
        self.assertEqual(sl.model_color("claude-opus-5-5"), sl.fg(173))
        self.assertEqual(sl.MODEL, sl.fg(173))
        stage_colors = {c for _, c in sl.STAGES} | {sl.RED}
        for m in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1", "x"):
            self.assertNotIn(sl.model_color(m), stage_colors, m)

    def test_main_model_name_uses_its_color(self):
        data = {"model": {"id": "claude-haiku-5-5", "display_name": "Haiku 5.5"}}
        out = sl.format_statusline(data, now=NOW, show_usage=False)
        self.assertTrue(out.startswith(f"{sl.model_color('claude-haiku-5-5')}Haiku 5.5"), repr(out[:40]))


class Layout(unittest.TestCase):
    def test_bars_use_full_blocks(self):
        out = plain(sl.bar(50))
        self.assertEqual(out, "█" * 5)

    def test_plain_bar_lights_one_block_for_any_usage(self):
        self.assertEqual(sl.bar(2), f"{sl.color_pct(2)}█{sl.RST}{sl.BAR_TRACK}{'█' * 4}{sl.RST}")
        self.assertEqual(sl.bar(0), f"{sl.color_pct(0)}{sl.RST}{sl.BAR_TRACK}{'█' * 5}{sl.RST}")

    def filled(self, p):
        raw = sl.bar(p)
        return len(raw.split(sl.RST)[0].replace(sl.color_pct(p), ""))

    def test_plain_bars_fill_by_stage(self):
        # 1 cell below 25%, 2 below 50, 3 below 70, 4 below 85, 5 from 85: a green value never shows
        # more than 2 cells, a red one always shows 5.
        cases = {0: 0, 1: 1, 24: 1, 25: 2, 49: 2, 50: 3, 69: 3, 70: 4, 84: 4, 85: 5, 100: 5}
        self.assertEqual({p: self.filled(p) for p in cases}, cases)
        for p in range(0, 101):
            cells, color = self.filled(p), sl.color_pct(p)
            if color == sl.STAGES[0][1]:
                self.assertLessEqual(cells, 2, p)
            if color == sl.RED:
                self.assertEqual(cells, 5, p)

    def test_long_branch_is_not_capped_when_there_is_room(self):
        branch = "live-usage-and-redesign-of-the-meters"
        out = plain(sl.where_item("claude-code-statusline", (branch, []), width=116))
        self.assertEqual(out, f"claude-code-statusline › {branch}")

    def test_branch_is_cut_from_its_end_before_the_project_is_touched(self):
        proj, branch = "claude-code-statusline", "live-usage-and-redesign-of-the-meters"
        out = plain(sl.where_item(proj, (branch, ["+1"]), width=40))
        self.assertEqual(out, "claude-code-statusline › live-usage-… +1")  # exactly 40, project whole
        out = plain(sl.where_item(proj, (branch, ["+1"]), width=36))
        self.assertEqual(out, "claude-code-statusline › live-us… +1")  # the shortest branch: 7 + …

    def test_branch_goes_before_the_project_loses_a_character(self):
        proj, branch = "claude-code-statusline", "live-usage-and-redesign"
        self.assertEqual(plain(sl.where_item(proj, (branch, ["+1"]), width=30)), proj)
        self.assertEqual(plain(sl.where_item(proj, (branch, ["+1"]), width=22)), proj)
        out = plain(sl.where_item(proj, (branch, ["+1"]), width=20))
        self.assertEqual(out, sl.mid_trunc(proj, 20))
        self.assertEqual(plain(sl.where_item(proj, (branch, []), width=10)), sl.mid_trunc(proj, sl.MIN_NAME))

    def test_middle_ellipsis(self):
        self.assertEqual(sl.mid_trunc("claude-code-statusline", 30), "claude-code-statusline")
        self.assertEqual(sl.mid_trunc("claude-code-statusline", 19), "claude-co…tatusline")
        self.assertEqual(sl.mid_trunc("claude-code-statusline", 20), "claude-co…statusline")


class VisibleWidth(unittest.TestCase):
    def test_width_ignores_color_codes(self):
        self.assertEqual(sl.visible_width(f"{sl.GREEN}abc{sl.RST}"), 3)


class SlantedBars(unittest.TestCase):
    SEG = ""  # lower-right + upper-left triangle: one slanted segment

    def tearDown(self):
        sl.set_icons("none")

    def test_nerd_bar_is_three_slanted_segments(self):
        sl.set_icons("nerd")
        self.assertEqual(plain(sl.bar(60)), self.SEG * 3)
        self.assertEqual(self.SEG, sl.NERD_ICONS["segment"])

    def test_filled_segments_take_the_ramp_color_and_the_rest_the_track(self):
        sl.set_icons("nerd")
        self.assertEqual(sl.bar(60), f"{sl.color_pct(60)}{self.SEG * 2}{sl.RST}{sl.BAR_TRACK}{self.SEG}{sl.RST}")

    def test_any_usage_shows_at_least_one_segment(self):
        sl.set_icons("nerd")
        self.assertIn(f"{sl.color_pct(3)}{self.SEG}{sl.RST}{sl.BAR_TRACK}{self.SEG * 2}", sl.bar(3))

    def test_segments_fill_by_stage(self):
        # one segment while green (<50%), two while yellow or orange, all three when red (85%+)
        sl.set_icons("nerd")

        def filled(p):
            return len(sl.bar(p).split(sl.RST)[0].replace(sl.color_pct(p), "")) // len(self.SEG)
        cases = {0: 0, 1: 1, 49: 1, 50: 2, 69: 2, 70: 2, 84: 2, 85: 3, 100: 3}
        self.assertEqual({p: filled(p) for p in cases}, cases)


class Icons(unittest.TestCase):
    DATA = {"model": {"display_name": "Opus"}, "cost": {"total_cost_usd": 2.39},
            "context_window": {"used_percentage": 14}}

    def tearDown(self):
        sl.set_icons("none")

    def test_plain_by_default(self):
        out = plain(sl.format_statusline(self.DATA, now=NOW, show_usage=False))
        self.assertTrue(out.startswith("Opus │ ctx"), out)
        self.assertIn("$2.39", out)

    def test_nerd_mode_has_no_icons_on_model_ctx_or_cost(self):
        sl.set_icons("nerd")
        out = plain(sl.format_statusline(self.DATA, now=NOW, show_usage=False))
        self.assertTrue(out.startswith("Opus │ ctx 14% "), out)
        self.assertTrue(out.endswith(" │ $2.39"), out)
        for name in ("model", "ctx", "cost", "project", "branch", "mcp", "cc", "plugins", "skills", "agents"):
            self.assertNotIn(name, sl.NERD_ICONS, name)
        for name in ("five_hour", "seven_day", "duration", "tag", "venv", "ports", "ok", "up", "warn", "reload", "segment"):
            self.assertIn(name, sl.NERD_ICONS, name)

    def test_separators_are_dimmer_than_labels(self):
        self.assertEqual(sl.SEP, f" {sl.fg(240)}│{sl.RST} ")
        self.assertEqual(sl.DIM, sl.fg(252))

    def test_cost_is_plain_green_not_bold(self):
        out = sl.format_statusline(self.DATA, now=NOW, show_usage=False)
        self.assertIn(f"{sl.fg(114)}$2.39{sl.RST}", out)
        self.assertNotIn("[1;", out)
        self.assertEqual(sl.COST, sl.GREEN)


class Theme(unittest.TestCase):
    """STATUSLINE_THEME=light swaps the palette for one that reads on a white background."""

    def tearDown(self):
        sl.set_theme("dark")

    def test_light_palette(self):
        sl.set_theme("light")
        self.assertEqual(sl.DIM, sl.fg(244))
        self.assertEqual([c for _, c in sl.STAGES], [sl.fg(28), sl.fg(136), sl.fg(166)])
        self.assertEqual(sl.RED, sl.fg(160))
        self.assertEqual(sl.COST, sl.fg(28))
        self.assertEqual(sl.SEP, f" {sl.fg(250)}│{sl.RST} ")
        self.assertEqual(sl.color_pct(10), sl.fg(28))
        self.assertEqual(sl.EFFORT["low"], sl.fg(244))
        self.assertNotIn(sl.fg(252), sl.format_statusline(SingleLine.DATA, now=NOW))

    def test_default_and_unknown_names_are_the_dark_palette(self):
        sl.set_theme("light")
        sl.set_theme("bananas")
        self.assertEqual(sl.DIM, sl.fg(252))
        self.assertEqual(sl.color_pct(10), sl.fg(114))
        self.assertEqual(sl.SEP, f" {sl.fg(240)}│{sl.RST} ")

    def test_every_light_color_reads_on_white(self):
        # 3:1 contrast against white (WCAG for large text and UI parts) for every xterm-256 index used as text.
        def luminance(index):
            if index >= 232:
                v = 8 + (index - 232) * 10
                rgb = (v, v, v)
            else:
                i = index - 16
                steps = (0, 95, 135, 175, 215, 255)
                rgb = (steps[i // 36], steps[i // 6 % 6], steps[i % 6])
            lin = [(c / 255 / 12.92) if c / 255 <= 0.03928 else ((c / 255 + 0.055) / 1.055) ** 2.4 for c in rgb]
            return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
        for name, value in sl.THEMES["light"].items():
            if name in ("TRACK", "SEP"):
                continue  # lines, not text
            indices = list(value.values()) if isinstance(value, dict) else list(value) if isinstance(value, tuple) else [value]
            for index in indices:
                ratio = 1.05 / (luminance(index) + 0.05)
                self.assertGreaterEqual(ratio, 3.0, (name, index, ratio))


class SingleLine(unittest.TestCase):
    """format_statusline(): the one line, its items and what it drops at each width."""

    DATA = {"model": {"display_name": "Opus 5.5", "id": "claude-opus-5-5"}, "effort": {"level": "high"},
            "context_window": {"used_percentage": 43, "context_window_size": 1_000_000,
                               "current_usage": {"input_tokens": 430_000}},
            "cost": {"total_cost_usd": 35.5, "total_duration_ms": 720_000}}
    GIT = ("live-usage-and-redesign", [f"{sl.AMBER}~2{sl.RST}"])
    PROJ = "claude-code-statusline"
    NAME = "statusline-py-usage-accuracy"
    OK = [("✓ mcp 3 · 2.1.296", 0, "")]
    BAD = [("▲ Gmail", 3, "Gmail"), ("↑ 2.2.0", 2, "2.2.0"), ("~reload", 1, "reload")]

    def tearDown(self):
        sl.set_icons("none")

    def usage(self):
        return sl.merge_usage({}, live(five=(74, NOW + 2 * HOUR + 600), seven=(69, NOW + 5 * 24 * HOUR + 14 * HOUR)), NOW)

    def extras(self, marks=None, name=None, skills=69, ports="", venv="", git=GIT):
        return {"proj": self.PROJ, "git": git, "name": name or "", "skills": skills,
                "marks": list(marks or []), "ports": ports, "venv": venv}

    def line(self, columns=None, **kw):
        """The line for a window `columns` wide (main() gives format_statusline COLUMNS - EDGE)."""
        width = None if columns is None else columns - sl.EDGE
        return sl.format_statusline(self.DATA, self.usage(), NOW, True, self.extras(**kw), width)

    def at(self, width, **kw):
        out = plain(self.line(width, **kw))
        self.assertNotIn("\n", out)
        self.assertLessEqual(sl.visible_width(out), width - sl.EDGE, out)
        return out

    def full(self, **kw):
        kw.setdefault("name", self.NAME)
        kw.setdefault("marks", self.OK)
        kw.setdefault("ports", ":3000")
        kw.setdefault("venv", "venv:x")
        return kw

    def test_everything_fits_on_a_wide_window(self):
        out = plain(self.line(300, **self.full()))
        b = lambda p: plain(sl.bar(p))
        self.assertEqual(out, f"claude-code-statusline › live-usage-and-redesign ~2 “{self.NAME}”"
                              f" │ Opus 5.5 · high │ ctx 43% {b(43)} 430k/1M │ 5h 74% {b(74)} →2h"
                              f" │ 7d 69% {b(69)} │ $35.50 │ session 12m │ ✓ mcp 3 · 2.1.296"
                              f" │ skills 69 │ venv:x │ :3000")

    def test_no_extras_is_just_the_session_items(self):
        out = plain(sl.format_statusline(self.DATA, self.usage(), NOW))
        self.assertTrue(out.startswith("Opus 5.5 · high │ ctx 43% "), out)
        self.assertNotIn("skills", out)

    def test_unknown_usage_has_no_bar_and_no_countdown(self):
        out = plain(sl.format_statusline(self.DATA, None, NOW))
        self.assertIn("5h -- │ 7d -- │", out)

    def test_countdown_follows_the_bar_in_light_grey(self):
        raw = self.line(300)
        self.assertIn(f"{sl.bar(74)} {sl.DIM}→2h{sl.RST}", raw)
        self.assertIn(f"{sl.bar(43)} {sl.DIM}430k/1M{sl.RST}", raw)

    def test_percentages_keep_stage_colors_on_label_and_value(self):
        raw = self.line(300)
        for label, p in (("ctx", 43), ("5h", 74), ("7d", 69)):
            c = sl.color_pct(p)
            self.assertIn(f"{c}{label}{sl.RST} {c}{p}%{sl.RST} {sl.bar(p)}", raw)

    def test_styles_of_model_cost_project_branch_and_name(self):
        raw = self.line(300, name=self.NAME)
        self.assertIn(f"{sl.model_color('claude-opus-5-5')}Opus 5.5", raw)
        self.assertIn(f"{sl.EFFORT['high']}high", raw)
        self.assertIn(f"{sl.COST}$35.50", raw)
        self.assertTrue(raw.startswith(f"{sl.PROJECT}claude-code-statusline"), repr(raw[:40]))
        self.assertIn(f"{sl.AMBER}live-usage-and-redesign", raw)  # dirty branch
        self.assertIn(f"{sl.CYAN}“{self.NAME}”", raw)
        self.assertIn(f"{sl.DIM}session 12m", raw)

    def test_where_item_comes_first_then_the_name_then_the_model(self):
        out = plain(self.line(300, name="my-task"))
        self.assertTrue(out.startswith("claude-code-statusline › live-usage-and-redesign ~2 “my-task” │ Opus 5.5"), out)

    def test_no_name_no_tag(self):
        self.assertNotIn("“", plain(self.line(300)))
        self.assertTrue(plain(self.line(300)).endswith("skills 69"))

    def test_outside_a_repo_only_the_project_shows(self):
        out = plain(self.line(300, git=None))
        self.assertTrue(out.startswith("claude-code-statusline │ Opus"), out)

    def test_ports_and_venv_only_when_detected(self):
        out = plain(self.line(300, ports=":3000,5173", venv="venv:.venv"))
        self.assertTrue(out.endswith("skills 69 │ venv:.venv │ :3000,5173"), out)
        self.assertTrue(plain(self.line(300)).endswith("skills 69"))

    def test_nerd_session_time_is_clock_and_number(self):
        sl.set_icons("nerd")
        out = plain(self.line(300))
        self.assertIn(f"{sl.ICON['duration']} 12m", out)
        self.assertNotIn("session", out)

    def test_no_session_time_before_any_time_passed(self):
        data = {**self.DATA, "cost": {"total_cost_usd": 35.5, "total_duration_ms": 0}}
        self.assertNotIn("session", plain(sl.format_statusline(data, self.usage(), NOW)))

    def test_nerd_icons_and_three_segment_bars(self):
        sl.set_icons("nerd")
        out = plain(self.line(300, name=self.NAME, marks=self.OK))
        head = (f"claude-code-statusline › live-usage-and-redesign ~2 "
                f"{sl.ICON['tag']} {self.NAME} │ Opus 5.5 · high │ ctx 43% ")
        self.assertTrue(out.startswith(head), out)
        self.assertEqual(out.count(sl.ICON["segment"]), 9)
        self.assertIn(f"{sl.ICON['five_hour']} 5h 74% ", out)
        self.assertIn("│ $35.50 │", out)
        self.assertTrue(out.endswith("│ skills 69"), out)

    def test_nerd_unknown_usage_has_no_bar(self):
        sl.set_icons("nerd")
        out = plain(sl.format_statusline(self.DATA, None, NOW))
        self.assertEqual(out.count(sl.ICON["segment"]), 3)  # only ctx
        self.assertIn(f"{sl.ICON['five_hour']} 5h --", out)

    # --- the drop order ---------------------------------------------------------

    def last_width(self, key, **kw):
        """The narrowest window at which `key` is still on the line."""
        kw = self.full(**kw)
        found = [w for w in range(300, 20, -1) if key in plain(self.line(w, **kw))]
        return found[-1] if found else None

    def test_items_drop_in_the_documented_order(self):
        keys = ["venv:x", "skills 69", "session 12m", "✓ mcp 3 · 2.1.296",
                "“", "live-usage", "claude-code", "430k/1M", "$35.50", "· high"]
        widths = [self.last_width(k) for k in keys]
        self.assertNotIn(None, widths)
        self.assertEqual(self.last_width(":3000"), widths[0])
        self.assertEqual(widths, sorted(widths, reverse=True), dict(zip(keys, widths)))
        self.assertEqual(len(set(widths)), len(widths) - 0, dict(zip(keys, widths)))
        self.assertIsNone(self.last_width("5d"))  # 7d is at 69%: no countdown
        self.assertEqual(self.last_width("→2h"), self.last_width("5h 74%"))  # the 5h countdown never goes alone

    def test_ports_and_venv_go_first(self):
        n = sl.visible_width(plain(self.line(300, **self.full())))
        got = plain(self.line(n + sl.EDGE - 1, **self.full()))
        self.assertNotIn("venv:x", got)
        self.assertNotIn(":3000", got)
        self.assertIn("skills 69", got)
        self.assertIn("→2h", got)

    def test_skills_go_second_and_session_time_third(self):
        kw = self.full(ports="", venv="")
        n = sl.visible_width(plain(self.line(300, **kw)))
        got = plain(self.line(n + sl.EDGE - 1, **kw))
        self.assertNotIn("skills 69", got)
        self.assertIn("session 12m", got)
        kw = self.full(ports="", venv="", skills=None)
        n = sl.visible_width(plain(self.line(300, **kw)))
        got = plain(self.line(n + sl.EDGE - 1, **kw))
        self.assertNotIn("session 12m", got)
        self.assertIn("→2h", got)
        self.assertIn("✓ mcp 3 · 2.1.296", got)
        self.assertIn("430k/1M", got)

    def test_the_healthy_cluster_goes_then_the_name(self):
        kw = self.full(ports="", venv="", skills=None)
        stages = []
        for w in range(300, 20, -1):
            out = plain(self.line(w, **kw))
            state = tuple(k in out for k in ("session 12m", "✓ mcp 3 · 2.1.296", "“"))
            if not stages or stages[-1] != state:
                stages.append(state)
        self.assertEqual(stages[:4], [(True, True, True), (False, True, True), (False, False, True),
                                      (False, False, False)])

    def test_branch_is_shortened_then_dropped_before_the_project_is_touched(self):
        kw = self.full(ports="", venv="", skills=None, marks=[], name=None)
        seen = []
        for w in range(300, 20, -1):
            out = plain(self.line(w, **kw))
            first = out.split(" │ ")[0]
            state = ("live-usage-and-redesign" in first, "live-usage…" in first, "live-" in first, "claude-code-statusline" in first)
            if not seen or seen[-1] != state:
                seen.append(state)
        # full branch -> (mid-truncated by the soft cap) -> short branch (then without its counts) -> no branch;
        # the project stays whole until the branch is gone
        short = [s for s in seen if s[1]]
        self.assertTrue(short, seen)
        gone = [s for s in seen if not s[2]]
        self.assertTrue(gone, seen)
        self.assertEqual(seen.index(short[0]) < seen.index(gone[0]), True)
        self.assertTrue(short[0][3])

    def test_project_is_truncated_to_14_before_it_goes(self):
        kw = self.full(ports="", venv="", skills=None, marks=[], name=None, git=None)
        firsts = [plain(self.line(w, **kw)).split(" │ ")[0] for w in range(300, 20, -1)]
        self.assertEqual(firsts[0], "claude-code-statusline")
        cut = [f for f in firsts if "…" in f]
        self.assertTrue(cut, firsts)
        self.assertEqual(min(len(f) for f in cut), 14)  # never below 14 before it goes
        self.assertTrue(all(len(f) >= 14 for f in firsts if f.startswith("claude")))
        self.assertTrue(any(f.startswith("Opus") for f in firsts))

    def test_tokens_then_bars_then_cost_go_last_and_the_percentages_stay(self):
        kw = self.full(ports="", venv="", skills=None, marks=[], name=None, git=None)
        seen = []
        for w in range(300, 20, -1):
            out = plain(self.line(w, **kw))
            for want in ("Opus 5.5", "ctx 43%", "5h 74%", "7d 69%"):
                self.assertIn(want, out, w)
            state = ("430k/1M" in out, out.count("█") // 5, "$35.50" in out)
            if not seen or seen[-1] != state:
                seen.append(state)
        tokens_gone = next(i for i, s in enumerate(seen) if not s[0])
        bars_gone = next(i for i, s in enumerate(seen) if s[1] == 0)
        cost_gone = next(i for i, s in enumerate(seen) if not s[2])
        self.assertLess(tokens_gone, bars_gone)
        self.assertLess(bars_gone, cost_gone)

    def test_bars_go_from_7d_then_5h_then_ctx(self):
        kw = self.full(ports="", venv="", skills=None, marks=[], name=None, git=None)
        seen = []
        for w in range(300, 20, -1):
            out = plain(self.line(w, **kw))
            state = tuple(bool(re.search(rf"{label} \d+% █", out)) for label in ("ctx", "5h", "7d"))
            if not seen or seen[-1] != state:
                seen.append(state)
        self.assertEqual(seen, [(True, True, True), (True, True, False), (True, False, False), (False, False, False)])

    def test_problems_are_named_while_there_is_room(self):
        out = plain(self.line(300, **self.full(marks=self.BAD)))
        self.assertIn("│ ▲ Gmail │ ↑ 2.2.0 │ ~reload │", out)
        self.assertNotIn("mcp 3", out)

    def test_problems_outlast_session_time(self):
        kw = self.full(ports="", venv="", skills=None, marks=self.BAD)
        n = sl.visible_width(plain(self.line(300, **kw)))
        got = plain(self.line(n + sl.EDGE - 1, **kw))
        self.assertNotIn("session 12m", got)
        self.assertIn("▲ Gmail │ ↑ 2.2.0 │ ~reload", got)

    def test_warning_texts_go_from_the_last_then_a_bare_marker_takes_over(self):
        kw = self.full(ports="", venv="", skills=None, marks=self.BAD, git=None, name=None)
        seen = []
        for w in range(300, 50, -1):
            out = plain(self.line(w, **kw))
            state = ("▲ Gmail" in out, "↑ 2.2.0" in out, "~reload" in out,
                     out.endswith(("▲", "▲Gmail")), "$35.50" in out, "█" in out, "430k/1M" in out)
            if not seen or seen[-1] != state:
                seen.append(state)
        flags = [s[:3] for s in seen]
        self.assertIn((True, True, True), flags)
        self.assertIn((True, True, False), flags)
        self.assertIn((True, False, False), flags)
        self.assertIn((False, False, False), flags)
        marker_states = [s for s in seen if s[3]]
        self.assertTrue(marker_states)
        self.assertFalse(any(s[0] or s[1] or s[2] for s in marker_states))
        # the bare marker outlasts the tokens, the bars and the cost
        self.assertTrue(any(s[3] and s[4] and s[5] and s[6] for s in seen))
        self.assertTrue(any(s[3] and not s[4] and not s[5] and not s[6] for s in seen), seen)

    def test_the_bare_marker_carries_a_reason_when_eight_columns_are_free(self):
        kw = self.full(ports="", venv="", skills=None, marks=self.BAD, git=None, name=None)
        n = sl.visible_width(plain(self.line(300, **kw)))
        reasons, bare = [], []
        for w in range(n + sl.EDGE - 1, 50, -1):
            out = plain(self.line(w, **kw))
            if out.endswith("▲Gmail"):
                reasons.append(w)
                self.assertGreaterEqual(w - sl.EDGE - sl.visible_width(out[:-5]), 8, (w, out))
            elif out.endswith("▲"):
                bare.append(w)
                self.assertLess(w - sl.EDGE - sl.visible_width(out), 8, (w, out))
        self.assertTrue(reasons and bare, (reasons, bare))
        self.assertNotIn(" Gmail", "".join(plain(self.line(w, **kw)) for w in reasons))  # never the item text too

    def test_bare_marker_stays_down_to_model_and_percentages(self):
        kw = self.full(ports="", venv="", skills=None, marks=self.BAD, git=None, name=None)
        small = plain(self.line(52, **kw))
        self.assertTrue(small.endswith(sl.ICON["warn"]), small)
        self.assertNotIn("$35.50", small)
        for want in ("Opus 5.5", "ctx 43%", "5h 74%", "7d 69%"):
            self.assertIn(want, small)

    def test_marker_takes_the_glyph_and_color_of_the_worst_problem(self):
        kw = self.full(ports="", venv="", skills=None, git=None, name=None)
        raw = self.line(52, **{**kw, "marks": self.BAD})
        self.assertTrue(raw.endswith(f"{sl.RED}{sl.ICON['warn']}{sl.RST}"), repr(raw[-30:]))
        raw = self.line(52, **{**kw, "marks": self.BAD[1:]})
        self.assertTrue(raw.endswith(f"{sl.AMBER}{sl.ICON['up']}{sl.RST}"), repr(raw[-30:]))
        raw = self.line(52, **{**kw, "marks": self.BAD[2:]})
        self.assertTrue(raw.endswith(f"{sl.DIM}{sl.ICON['reload']}{sl.RST}"), repr(raw[-30:]))
        ok = plain(self.line(52, **{**kw, "marks": self.OK}))
        self.assertNotIn(sl.ICON["warn"], ok)
        for marks, reason in ((self.BAD[1:], "↑2.2.0"), (self.BAD[2:], "~reload")):
            ends = {plain(self.line(w, **{**kw, "marks": marks})).split(" ")[-1] for w in range(50, 130)}
            self.assertIn(reason, ends, marks)  # some width has the room for the reason
            self.assertIn(reason[0], ends, marks)  # some has only the glyph's

    def test_nerd_marker_keeps_a_space_before_its_reason(self):
        # Nerd glyphs draw past their cell; '󰀦discord' overlapped on screen.
        sl.set_icons("nerd")
        try:
            kw = self.full(ports="", venv="", skills=None, marks=self.BAD[:1], git=None, name=None)
            ends = {plain(self.line(w, **kw)).split(" │ ")[-1] for w in range(50, 130)}
            self.assertIn(f"{sl.ICON['warn']} Gmail", ends, ends)
            self.assertFalse(any(e.startswith(sl.ICON["warn"] + "G") for e in ends), ends)
        finally:
            sl.set_icons("none")

    def test_never_more_than_one_line_even_when_tiny(self):
        for w in (24, 30, 40):
            out = plain(self.line(w, **self.full(marks=self.BAD)))
            self.assertNotIn("\n", out)
            for want in ("Opus 5.5", "ctx 43%", "5h 74%", "7d 69%"):
                self.assertIn(want, out)

    def test_names_get_about_a_third_of_the_line_before_anything_else_goes(self):
        out = self.at(120, **self.full(marks=[], ports="", venv="", skills=None, name=None))
        first = out.split(" │ ")[0]
        self.assertLessEqual(len(first), 0.4 * 116)
        # the project stays whole; the branch is what gets cut or dropped
        self.assertTrue(first.startswith("claude-code-statusline"), first)
        self.assertNotIn("…", first.split(" › ")[0])

    def test_every_width_fits(self):
        # 47 columns is the least that holds the model and the three percentages
        for marks in ([], self.OK, self.BAD):
            for w in range(52, 260, 3):
                self.at(w, **self.full(marks=marks))

    def test_nerd_line_fits_a_120_column_window(self):
        sl.set_icons("nerd")
        out = self.at(120, **self.full(marks=self.OK))
        for want in ("Opus 5.5", "ctx 43%", "5h 74%", "7d 69%", "35.50"):
            self.assertIn(want, out)
        self.assertEqual(out.count(sl.ICON["segment"]), 9)

    def test_width_none_means_no_limit(self):
        self.assertIn("venv:x", plain(self.line(None, **self.full())))


class SessionName(unittest.TestCase):
    """The session name: payload session_name, else the newest title in the transcript's tail."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "t.jsonl"

    def write(self, *entries):
        import json
        lines = [json.dumps(e) for e in entries]
        self.path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))

    def test_slug_is_lowercase_and_hyphenated(self):
        self.assertEqual(sl.slugify("Statusline.py: usage  accuracy!"), "statusline-py-usage-accuracy")
        self.assertEqual(sl.slugify("  --Fix the bug--  "), "fix-the-bug")
        self.assertEqual(sl.slugify("!!!"), "")
        self.assertEqual(sl.slugify("דוגמה בדיקה"), "דוגמה-בדיקה")

    def test_slug_is_capped_at_a_word_boundary(self):
        got = sl.slugify("rebuild the statusline layout as one single upgraded line")
        self.assertEqual(got, "rebuild-the-statusline")
        self.assertLessEqual(len(got), sl.NAME_MAX)
        self.assertEqual(sl.slugify("x" * 40), "x" * sl.NAME_MAX)  # one huge word is cut hard
        self.assertEqual(sl.NAME_MAX, 28)

    def test_payload_session_name_wins(self):
        self.write({"type": "ai-title", "aiTitle": "From The Transcript"})
        data = {"session_name": "My Renamed Session", "transcript_path": str(self.path)}
        self.assertEqual(sl.session_name(data), "my-renamed-session")

    def test_blank_payload_name_falls_back_to_the_transcript(self):
        self.write({"type": "ai-title", "aiTitle": "From The Transcript"})
        data = {"session_name": "  ", "transcript_path": str(self.path)}
        self.assertEqual(sl.session_name(data), "from-the-transcript")

    def test_newest_title_entry_wins_whatever_its_kind(self):
        self.write({"type": "ai-title", "aiTitle": "First"}, {"type": "user"},
                   {"type": "agent-name", "agentName": "Second"},
                   {"type": "custom-title", "customTitle": "Third"},
                   {"type": "ai-title", "aiTitle": "Fourth One"}, {"type": "assistant"})
        self.assertEqual(sl.tail_title(self.path), "Fourth One")
        self.write({"type": "ai-title", "aiTitle": "Old"}, {"type": "agent-name", "agentName": "Newer"})
        self.assertEqual(sl.tail_title(self.path), "Newer")
        self.write({"type": "custom-title", "customTitle": "Mine"})
        self.assertEqual(sl.tail_title(self.path), "Mine")

    def test_no_title_no_name(self):
        self.write({"type": "user"}, {"type": "assistant"})
        self.assertEqual(sl.session_name({"transcript_path": str(self.path)}), "")
        self.assertEqual(sl.session_name({"transcript_path": str(self.path) + ".missing"}), "")
        self.assertEqual(sl.session_name({}), "")

    def test_only_the_tail_of_a_huge_transcript_is_read(self):
        self.write({"type": "ai-title", "aiTitle": "Buried Title"})
        filler = b'{"type":"assistant","text":"' + b"x" * 1000 + b'"}\n'
        with open(self.path, "ab") as f:
            f.write(filler * 600)  # ~600 KB after the title
        self.assertEqual(sl.tail_title(self.path), "")  # out of the 256 KB window
        with open(self.path, "ab") as f:
            f.write(b'{"type":"ai-title","aiTitle":"Fresh Title"}\n' + filler * 10)
        self.assertEqual(sl.tail_title(self.path), "Fresh Title")

    def test_the_read_is_bounded(self):
        from unittest import mock
        self.write({"type": "ai-title", "aiTitle": "T"})
        with open(self.path, "ab") as f:
            f.write(b"x" * 1_000_000)
        sizes = []
        real_open = open

        def spy(file, mode="r", *a, **k):
            f = real_open(file, mode, *a, **k)
            real_read = f.read

            def read(n=-1):
                data = real_read(n)
                sizes.append(len(data))
                return data
            f.read = read
            return f

        with mock.patch("builtins.open", spy):
            sl.tail_title(self.path)
        self.assertTrue(sizes and max(sizes) <= 262_144, sizes)

    def test_a_title_cut_in_half_by_a_partial_last_line_is_ignored(self):
        self.write({"type": "ai-title", "aiTitle": "Whole"})
        with open(self.path, "ab") as f:
            f.write(b'{"type":"ai-title","aiTitle":"Cut ')
        self.assertEqual(sl.tail_title(self.path), "Whole")

    def test_cached_by_path_and_size(self):
        from unittest import mock
        self.write({"type": "ai-title", "aiTitle": "First Name"})
        data = {"transcript_path": str(self.path)}
        cache_file = pathlib.Path(self.tmp.name) / "probe.json"
        with mock.patch.object(sl, "tail_title", wraps=sl.tail_title) as tail:
            c = sl.ProbeCache(cache_file, NOW)
            self.assertEqual(sl.session_name(data, c), "first-name")
            c.save()
            c = sl.ProbeCache(cache_file, NOW + 1)
            self.assertEqual(sl.session_name(data, c), "first-name")
            self.assertEqual(tail.call_count, 1)  # same size: not read again
            with open(self.path, "ab") as f:
                f.write(b'{"type":"ai-title","aiTitle":"Second Name"}\n')
            self.assertEqual(sl.session_name(data, c), "second-name")
            self.assertEqual(tail.call_count, 2)

    def test_a_name_stays_when_the_grown_tail_has_no_title(self):
        self.write({"type": "ai-title", "aiTitle": "Kept Name"})
        data = {"transcript_path": str(self.path)}
        c = sl.ProbeCache(None, NOW)
        self.assertEqual(sl.session_name(data, c), "kept-name")
        with open(self.path, "ab") as f:
            f.write(b'{"type":"assistant"}\n' * 20_000)
        self.assertEqual(sl.session_name(data, c), "kept-name")

    def test_the_cached_entry_is_kept_fresh_while_the_file_does_not_change(self):
        cache_file = pathlib.Path(self.tmp.name) / "probe.json"
        self.write({"type": "ai-title", "aiTitle": "Old Session"})
        data = {"transcript_path": str(self.path)}
        c = sl.ProbeCache(cache_file, NOW)
        sl.session_name(data, c)
        c.save()
        later = NOW + sl.NAME_REFRESH + 5
        c = sl.ProbeCache(cache_file, later)
        sl.session_name(data, c)
        c.save()
        c = sl.ProbeCache(cache_file, later + 40)  # more than the longest probe TTL after the first write
        self.assertEqual(c.peek("name:" + str(self.path)).get("name"), "old-session")

    def test_a_garbage_cache_entry_is_just_reread(self):
        import json
        self.write({"type": "ai-title", "aiTitle": "Name Here"})
        cache_file = pathlib.Path(self.tmp.name) / "probe.json"
        key = "name:" + str(self.path)
        cache_file.write_text(json.dumps({key: {"at": NOW, "v": "junk"}}), encoding="utf-8")
        c = sl.ProbeCache(cache_file, NOW)
        self.assertEqual(sl.session_name({"transcript_path": str(self.path)}, c), "name-here")


class TempClaudeDir:
    """Runs main() against a temp CLAUDE_DIR and a temp project folder."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.dir = root / "claude"
        self.dir.mkdir()
        self.proj = root / "my-special-proj"
        self.proj.mkdir()
        self.saved = {k: getattr(sl, k) for k in ("CLAUDE_DIR", "USAGE_CACHE", "USAGE_LOCK", "NATIVE_VERSIONS")}
        sl.CLAUDE_DIR = str(self.dir)
        sl.USAGE_CACHE = self.dir / "statusline-usage.json"
        sl.USAGE_LOCK = self.dir / "statusline-usage.lock"
        sl.NATIVE_VERSIONS = root / "versions"
        from unittest import mock
        for name, value in (("run", ""), ("open_ports", [])):  # no real git or port scan
            patcher = mock.patch.object(sl, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ)  # a venv of the developer running the tests must not show up
        env.start()
        self.addCleanup(env.stop)
        for var in ("VIRTUAL_ENV", "CONDA_DEFAULT_ENV"):
            os.environ.pop(var, None)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(sl, k, v)
        sl.set_icons("none")
        sl.set_theme("dark")
        self.tmp.cleanup()

    def render(self, icons="none", columns=120, health=None, data_update=None,
               live_usage=False, drop=()):
        """main() against the temp folders; setUp made git say 'not a repo' and no port answer."""
        import io, json, os
        from contextlib import ExitStack, redirect_stdout
        from unittest import mock
        data = dict(SingleLine.DATA)
        data["rate_limits"] = {"five_hour": {"used_percentage": 19, "resets_at": 9e9},
                               "seven_day": {"used_percentage": 55, "resets_at": 9e9}}
        data["workspace"] = {"current_dir": str(self.proj)}
        data.update(data_update or {})
        for k in drop:
            data.pop(k, None)
        buf = io.StringIO()
        with ExitStack() as stack:
            if health is not None:
                stack.enter_context(mock.patch.object(sl, "health_state", return_value=health))
            stack.enter_context(mock.patch.object(sys, "stdin", io.StringIO(json.dumps(data))))
            stack.enter_context(mock.patch.object(sys, "argv", ["statusline.py", "--icons", icons]))
            stack.enter_context(mock.patch.dict(os.environ, {"COLUMNS": str(columns), "STATUSLINE_LIVE_USAGE": "1" if live_usage else "0", "WT_SESSION": "test"}))
            stack.enter_context(redirect_stdout(buf))
            with self.assertRaises(SystemExit):
                sl.main()
        return buf.getvalue().rstrip("\n")


class MainRendering(TempClaudeDir, unittest.TestCase):
    """main() renders the one line in a temp CLAUDE_DIR."""

    DISCORD = ("2.1.296", ("ok", None), [], (["a"], ["discord"]), None)
    THREE_PROBLEMS = ("2.1.296", ("restart", "2.1.300"), ["p1"], (["a"], ["discord"]), None)
    HEALTHY = ("2.1.296", ("ok", None), [], (["a", "b", "c"], []), 69)

    def test_one_line_with_project_and_every_reading(self):
        raw = self.render(health=self.HEALTHY, columns=200)
        out = plain(raw)
        self.assertNotIn("\n", out)
        self.assertTrue(out.startswith("my-special-proj │ Opus 5.5 · high │ ctx 43% "), out)
        for want in ("5h 19%", "7d 55%", "$35.50", "session 12m", "✓ mcp 3 · 2.1.296", "skills 69"):
            self.assertIn(want, out)
        self.assertIn(sl.SEP, raw)

    def test_light_theme_from_the_environment(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"STATUSLINE_THEME": "light"}):
            raw = self.render(health=self.HEALTHY, columns=200)
        self.assertIn(sl.fg(244), raw)
        self.assertNotIn(sl.fg(252), raw)
        self.assertIn(f" {sl.fg(250)}│", raw)
        raw = self.render(health=self.HEALTHY, columns=200)
        self.assertIn(sl.fg(252), raw)

    def test_the_mode_file_and_the_agents_line_are_gone(self):
        for text in ("full", "compact", "bananas"):
            (self.dir / "statusline-mode").write_text(text, encoding="utf-8")
            self.assertEqual(plain(self.render(health=self.HEALTHY)).count("\n"), 0)
        for name in ("read_mode", "MODE_FILE_NAME", "TOGGLE_KEY", "format_compact", "compact_hint",
                     "toggle_hint", "workspace_line", "flow", "format_health", "compact_problems",
                     "SHOW_AGENTS", "format_agents", "running_subagents", "subagent_state", "fmt_model_id",
                     "_tail_entries", "SUBAGENT_STALE", "SUBAGENTS_SHOWN"):
            self.assertFalse(hasattr(sl, name), name)

    def test_the_script_does_not_import_pathlib(self):
        import subprocess
        r = subprocess.run([sys.executable, "-S", "-c", "import sys, statusline; print('pathlib' in sys.modules)"],
                           cwd=str(pathlib.Path(sl.__file__).parent), capture_output=True, text=True, timeout=20)
        self.assertEqual(r.stdout.strip(), "False", r.stderr)
        self.assertEqual(sl.CLAUDE_DIR, str(sl.CLAUDE_DIR))  # a plain string path

    def test_plain_120_with_a_failed_server_keeps_the_bars_and_a_warning(self):
        out = plain(self.render(health=self.DISCORD))
        self.assertEqual(out.count("█") // 5, 3)
        self.assertIn(sl.PLAIN_ICONS["warn"], out)
        self.assertLessEqual(sl.visible_width(out), 120 - sl.EDGE)

    def test_nerd_120_has_three_three_segment_bars_and_fits(self):
        out = plain(self.render(icons="nerd", health=self.HEALTHY))
        self.assertNotIn("\n", out)
        self.assertEqual(out.count(sl.NERD_ICONS["segment"]), 9)
        self.assertLessEqual(sl.visible_width(out), 120 - sl.EDGE)
        for want in ("my-special-proj", "Opus 5.5", "ctx 43%", "5h 19%", "7d 55%", "35.50"):
            self.assertIn(want, out)

    def test_a_failed_server_is_named_on_a_wide_window_and_a_red_marker_on_a_narrow_one(self):
        wide = plain(self.render(icons="nerd", columns=200, health=self.DISCORD))
        self.assertIn(f"│ {sl.NERD_ICONS['warn']} discord", wide)
        raw = self.render(icons="nerd", columns=60, health=self.DISCORD)
        self.assertNotIn(" discord", raw)
        self.assertIn(f"{sl.RED}{sl.NERD_ICONS['warn']}", raw)
        ok = self.render(icons="nerd", columns=60, health=self.HEALTHY)
        self.assertNotIn(sl.NERD_ICONS["warn"], ok)

    def test_usage_items_are_hidden_when_neither_stdin_nor_live_data_ever_had_them(self):
        for health in (None, self.DISCORD):
            out = plain(self.render(health=health, drop=("rate_limits",)))
            self.assertNotIn("5h", out)
            self.assertNotIn("7d", out)
            self.assertIn("ctx 43%", out)

    def test_usage_items_stay_as_unknown_once_a_source_had_them(self):
        # stdin had the windows but they expired: the items stay, as "--"
        expired = {"five_hour": {"used_percentage": 19, "resets_at": 5}, "seven_day": {"used_percentage": 55, "resets_at": 5}}
        out = plain(self.render(data_update={"rate_limits": expired}))
        self.assertIn("5h -- │ 7d --", out)
        # live data has them even though stdin never did
        from unittest import mock
        cached = {"usage": {"five_hour": {"utilization": 31, "resets_at": "2099-01-01T00:00:00Z"}}}
        with mock.patch.object(sl, "load_usage_cache", return_value=cached), mock.patch.object(sl, "maybe_refresh"), \
                mock.patch.object(sl, "read_oauth_token", return_value=None):
            out = plain(self.render(live_usage=True, drop=("rate_limits",)))
        self.assertIn("5h 31%", out)
        self.assertIn("7d --", out)

    def test_absent_context_window_shows_ctx_unknown(self):
        out = plain(self.render(drop=("context_window",)))
        self.assertRegex(out, r"ctx\s+--")
        self.assertNotIn("430k", out)

    def test_60_columns_is_one_line_with_every_reading(self):
        out = plain(self.render(columns=60, health=self.DISCORD))
        self.assertNotIn("\n", out)
        for want in ("ctx 43%", "5h 19%", "7d 55%"):
            self.assertIn(want, out)
        self.assertLessEqual(sl.visible_width(out), 60 - sl.EDGE)

    def test_three_problems_fit_one_line(self):
        out = plain(self.render(health=self.THREE_PROBLEMS))
        self.assertNotIn("\n", out)
        self.assertLessEqual(sl.visible_width(out), 120 - sl.EDGE)

    def test_unknown_width_shows_everything(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {}):
            out = plain(self.render(columns="", health=self.HEALTHY))
        self.assertIn("skills 69", out)

    def test_the_session_name_comes_from_the_transcript_tail(self):
        transcript = self.dir / "t.jsonl"
        transcript.write_text('{"type":"ai-title","aiTitle":"Statusline py usage accuracy"}\n', encoding="utf-8")
        out = plain(self.render(columns=200, health=self.HEALTHY, data_update={"transcript_path": str(transcript)}))
        self.assertIn("“statusline-py-usage-accuracy”", out)
        self.assertTrue(out.startswith("my-special-proj “statusline-py-usage-accuracy” │ Opus"), out)

    def test_a_payload_session_name_is_used_and_no_name_no_tag(self):
        out = plain(self.render(columns=200, health=self.HEALTHY, data_update={"session_name": "My Task"}))
        self.assertIn("“my-task”", out)
        self.assertNotIn("“", plain(self.render(columns=200, health=self.HEALTHY)))

    def test_dev_ports_and_venv_show_only_when_found(self):
        from unittest import mock
        self.assertTrue(plain(self.render(columns=300, health=self.HEALTHY)).endswith("skills 69"))
        (self.dir / sl.PROBE_CACHE_NAME).unlink()  # the port scan is cached for PORTS_TTL
        with mock.patch.object(sl, "open_ports", return_value=[3000, 5173]), \
                mock.patch.dict(os.environ, {"VIRTUAL_ENV": "/x/.venv"}):
            out = plain(self.render(columns=300, health=self.HEALTHY))
        self.assertTrue(out.endswith("skills 69 │ venv:.venv │ :3000,5173"), out)

    def test_a_git_repo_shows_branch_and_dirty_count_after_the_project(self):
        from unittest import mock
        with mock.patch.object(sl, "run", return_value=GIT_PORCELAIN):
            out = plain(self.render(columns=200, health=self.HEALTHY))
        self.assertTrue(out.startswith("my-special-proj › main ~1 ?1 │ Opus"), out)

    def test_a_broken_probe_never_blanks_the_line(self):
        from unittest import mock
        with mock.patch.object(sl, "health_state", side_effect=RuntimeError("x")), \
                mock.patch.object(sl, "session_name", side_effect=RuntimeError("x")):
            out = plain(self.render(columns=200))
        self.assertIn("ctx 43%", out)
        self.assertNotIn("Claude | --", out)


class ConsoleHost(unittest.TestCase):
    """console_host(chain, windows): which console a Windows process really sits in.

    Claude Code spawns the status line with no console window and no WT_SESSION, in Windows
    Terminal just as in the classic console, so the verdict comes from the ancestors' windows.
    """

    CHAIN = [40, 30, 20, 10]  # python <- claude.exe <- pwsh.exe <- explorer.exe

    def test_claude_session_in_windows_terminal_as_default_terminal(self):
        windows = {20: [("PseudoConsoleWindow", True)], 10: [("Shell_TrayWnd", True), ("Progman", True)]}
        self.assertEqual(sl.console_host(self.CHAIN, windows), "pty")

    def test_claude_session_in_a_classic_console_window(self):
        windows = {20: [("ConsoleWindowClass", True)], 10: [("Progman", True)]}
        self.assertEqual(sl.console_host(self.CHAIN, windows), "classic")

    def test_windows_terminal_tab(self):
        windows = {20: [("PseudoConsoleWindow", True)], 10: [("CASCADIA_HOSTING_WINDOW_CLASS", True)]}
        self.assertEqual(sl.console_host(self.CHAIN, windows), "pty")

    def test_the_nearest_console_window_decides(self):
        # cmd.exe opened a classic window from a Windows Terminal shell: python <- cmd <- pwsh
        windows = {30: [("ConsoleWindowClass", True)], 20: [("PseudoConsoleWindow", True)]}
        self.assertEqual(sl.console_host(self.CHAIN, windows), "classic")

    def test_no_console_window_anywhere_is_no_verdict(self):
        self.assertIsNone(sl.console_host(self.CHAIN, {10: [("Progman", True)]}))
        self.assertIsNone(sl.console_host(self.CHAIN, {}))
        self.assertIsNone(sl.console_host([], {}))

    def test_hidden_console_windows_do_not_count(self):
        windows = {30: [("ConsoleWindowClass", False)], 20: [("PseudoConsoleWindow", True)]}
        self.assertEqual(sl.console_host(self.CHAIN, windows), "pty")


class Demo(TempClaudeDir, unittest.TestCase):
    """--demo prints two realistic sample lines at the window's width and touches no cache."""

    def demo(self, icons="none", columns=120, theme=None):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        buf = io.StringIO()
        env = {"COLUMNS": str(columns), "WT_SESSION": "x", "STATUSLINE_LIVE_USAGE": "1"}
        if theme:
            env["STATUSLINE_THEME"] = theme
        with mock.patch.object(sys, "argv", ["statusline.py", "--demo", "--icons", icons]), \
                mock.patch.dict(os.environ, env), redirect_stdout(buf), \
                mock.patch.object(sl, "maybe_refresh", side_effect=AssertionError("network")), \
                mock.patch.object(sl, "load_usage_cache", side_effect=AssertionError("usage cache read")), \
                self.assertRaises(SystemExit) as leave:
            sl.main()
        self.assertEqual(leave.exception.code, 0)
        return buf.getvalue().rstrip("\n").split("\n")

    def test_two_lines_healthy_and_troubled(self):
        healthy, troubled = [plain(x) for x in self.demo(columns=200)]
        self.assertIn("claude-code-statusline › live-usage-and-redesign ~2", healthy)
        self.assertIn("✓ mcp", healthy)
        self.assertIn("▲ discord", troubled)
        self.assertIn("↑ 2.", troubled)
        self.assertIn("“statusline-py-usage-accuracy”", troubled)
        for line in (healthy, troubled):
            self.assertLessEqual(sl.visible_width(line), 196, line)
            for want in ("ctx ", "5h ", "7d ", "$", "Opus"):
                self.assertIn(want, line)

    def test_at_120_columns_the_branch_survives_squeezed(self):
        healthy, troubled = [plain(x) for x in self.demo()]
        # The project stays whole at 120; the branch is cut or gone.
        self.assertRegex(healthy, r"^claude-code-statusline( › live-\S*…)? │ Opus 4\.6 · high │ ctx 43% █████ 430k/1M │ 5h 74% █████ →2h")
        # the long session name and two problems do not fit next to the meters: the problems fold into the marker
        self.assertTrue(troubled.endswith(("▲", "▲discord")), troubled)
        self.assertTrue(troubled.startswith("claude-code-statusline │ Opus 4.6 · high │ ctx 43%"), troubled)
        for line in (healthy, troubled):
            self.assertLessEqual(sl.visible_width(line), 116, line)

    def test_demo_fits_any_width_in_both_icon_sets(self):
        for icons in ("none", "nerd"):
            for columns in (60, 100, 120, 160, 200):
                for line in self.demo(icons, columns):
                    self.assertLessEqual(sl.visible_width(plain(line)), columns - sl.EDGE, (icons, columns, line))
        self.assertIn(sl.NERD_ICONS["segment"], self.demo("nerd")[0])

    def test_demo_touches_no_cache_file(self):
        self.demo()
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [])


class IconFallback(unittest.TestCase):
    """The classic Windows console (conhost) draws no Nerd Font glyphs: it gets plain icons."""

    def icons(self, verdict, wt=False, term=None):
        from unittest import mock
        env = {k: v for k, v in os.environ.items() if k not in ("WT_SESSION", "TERM_PROGRAM")}
        env.update({"WT_SESSION": "x"} if wt else {})
        env.update({"TERM_PROGRAM": term} if term else {})
        stack = mock.patch.dict(os.environ, env, clear=True)
        stack.__enter__()
        self.addCleanup(stack.__exit__, None, None, None)
        for name, value in (("_probe_console_host", verdict), ("name", "nt")):
            p = mock.patch.object(sl if name != "name" else sl.os, name, **({"return_value": value} if name != "name" else {"new": value}))
            p.start()
            self.addCleanup(p.stop)
        return sl.effective_icons("nerd")

    def test_classic_console_gets_plain_icons(self):
        self.assertEqual(self.icons("classic"), "none")

    def test_windows_terminal_keeps_nerd_icons(self):
        self.assertEqual(self.icons("pty"), "nerd")
        self.assertEqual(self.icons("pty", wt=True), "nerd")

    def test_no_verdict_keeps_nerd_icons(self):
        # A status line spawned by Claude Code has no console window of its own: that is not "classic".
        self.assertEqual(self.icons(None), "nerd")
        self.assertEqual(self.icons(None, wt=True), "nerd")
        self.assertEqual(self.icons(None, term="vscode"), "nerd")

    def test_a_failing_probe_keeps_nerd_icons(self):
        from unittest import mock
        env = {k: v for k, v in os.environ.items() if k not in ("WT_SESSION", "TERM_PROGRAM")}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sl.os, "name", "nt"), \
                mock.patch.object(sl, "_probe_console_host", side_effect=OSError("no ctypes")):
            self.assertEqual(sl.effective_icons("nerd"), "nerd")

    def test_plain_request_is_never_upgraded(self):
        self.icons("pty", wt=True)
        self.assertEqual(sl.effective_icons("none"), "none")

    def test_not_windows_means_no_probe(self):
        from unittest import mock
        with mock.patch.object(sl.os, "name", "posix"), \
                mock.patch.object(sl, "_probe_console_host", side_effect=AssertionError("probed")):
            self.assertEqual(sl.effective_icons("nerd"), "nerd")

    def test_force_variable_wins(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"STATUSLINE_FORCE_ICONS": "1"}), mock.patch.object(sl.os, "name", "nt"), \
                mock.patch.object(sl, "_probe_console_host", return_value="classic"):
            self.assertEqual(sl.effective_icons("nerd"), "nerd")

    def test_plain_glyphs_are_ones_consolas_and_cascadia_have(self):
        self.assertEqual(sorted(sl.PLAIN_ICONS), ["ok", "reload", "up", "warn"])
        self.assertNotIn(sl.PLAIN_ICONS["reload"], "↻")


GIT_PORCELAIN = ("# branch.oid 0123456789abcdef\n# branch.head main\n"
                 "1 .M N... 100644 100644 100644 a b file.py\n? new.txt\n")


class ProbeCache(unittest.TestCase):
    """Slow probes (git, ports, health) are kept in CLAUDE_DIR/statusline-probe-cache.json for a few seconds."""

    def setUp(self):
        import tempfile
        from unittest import mock
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.path = self.dir / sl.PROBE_CACHE_NAME
        self.repo = self.dir / "repo"
        self.repo.mkdir()
        for name, value in (("CLAUDE_DIR", self.dir), ("USAGE_CACHE", self.dir / "usage.json"),
                            ("NATIVE_VERSIONS", self.dir / "versions")):
            patcher = mock.patch.object(sl, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def at(self, now, probe):
        """Run probe(cache) as one render would at time `now`: load the cache, probe, save."""
        cache = sl.ProbeCache(self.path, now)
        value = probe(cache)
        cache.save()
        return value

    def git(self, now, cwd=None):
        return self.at(now, lambda c: sl.git_info(str(cwd or self.repo), c))

    def test_git_within_ttl_is_not_run_again(self):
        from unittest import mock
        with mock.patch.object(sl, "run", return_value=GIT_PORCELAIN) as run:
            first = self.git(NOW)
            second = self.git(NOW + sl.GIT_TTL - 0.5)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(sl.where_item("p", second), sl.where_item("p", first))
        self.assertEqual(plain(sl.where_item("p", second)), "p › main ~1 ?1")

    def test_expired_git_entry_refreshes(self):
        from unittest import mock
        with mock.patch.object(sl, "run", return_value=GIT_PORCELAIN) as run:
            self.git(NOW)
            run.return_value = "# branch.oid 0123456789abcdef\n# branch.head other\n"
            later = self.git(NOW + sl.GIT_TTL + 0.1)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(plain(sl.where_item("p", later)), "p › other")

    def test_git_is_cached_per_directory(self):
        from unittest import mock
        other = self.dir / "other"
        other.mkdir()
        with mock.patch.object(sl, "run", return_value=GIT_PORCELAIN) as run:
            self.git(NOW)
            self.git(NOW + 1, cwd=other)
            self.git(NOW + 2)
        self.assertEqual(run.call_count, 2)

    def test_ports_within_ttl_are_not_probed_again(self):
        from unittest import mock
        with mock.patch.object(sl, "open_ports", return_value=[3000, 8080]) as probe:
            first = self.at(NOW, sl.ports_tag)
            second = self.at(NOW + sl.PORTS_TTL - 0.5, sl.ports_tag)
            self.assertEqual(probe.call_count, 1)
            self.at(NOW + sl.PORTS_TTL + 0.1, sl.ports_tag)
            self.assertEqual(probe.call_count, 2)
        self.assertEqual(second, first)
        self.assertEqual(plain(first), ":3000,8080")

    def health(self, now, session="s1", project="proj"):
        data = {"session_id": session, "version": "2.1.296",
                "workspace": {"project_dir": str(self.dir / project)}}
        return self.at(now, lambda c: sl.health_state(data, cache=c))

    def test_health_is_cached_per_session_and_project(self):
        from unittest import mock
        logs = {"plugin-x-x": [{"debug": "Successfully connected", "sessionId": "s1"}]}
        with mock.patch.object(sl, "read_mcp_logs", return_value=logs) as read, \
                mock.patch.object(sl, "count_skills", return_value=7):
            first = self.health(NOW)
            second = self.health(NOW + sl.HEALTH_TTL - 0.5)
            self.assertEqual(read.call_count, 1)
            self.health(NOW + 1, project="elsewhere")
            self.assertEqual(read.call_count, 2)
            self.health(NOW + sl.HEALTH_TTL + 0.1)
            self.assertEqual(read.call_count, 3)
        self.assertEqual(sl.health_marks(*second[:4]), sl.health_marks(*first[:4]))  # JSON turns tuples to lists
        self.assertEqual([plain(t) for t, _, _ in sl.health_marks(*first[:4])], ["✓ mcp 1 · 2.1.296"])
        self.assertEqual(first[4], 7)

    def test_sessions_of_one_project_share_the_mcp_log_scan(self):
        from unittest import mock
        logs = {"plugin-x-x": [{"debug": "Successfully connected", "sessionId": "s1", "timestamp": "1"},
                               {"error": "Connection failed (x)", "sessionId": "s2", "timestamp": "2"}]}
        with mock.patch.object(sl, "read_mcp_logs", return_value=logs) as read, \
                mock.patch.object(sl, "count_skills", return_value=0):
            one = self.health(NOW, session="s1")
            two = self.health(NOW + 1, session="s2")
            three = self.health(NOW + 2, session="s3")
        self.assertEqual(read.call_count, 1)
        self.assertEqual(list(one[3]), [["x"], []])
        self.assertEqual(list(two[3]), [[], ["x"]])
        self.assertEqual(list(three[3]), [[], []])  # no connection events of its own: nothing to show
        self.assertIn("mcplogs:", "".join(self.entries()))

    def test_skill_count_lives_longer_than_the_rest_of_health(self):
        from unittest import mock
        with mock.patch.object(sl, "read_mcp_logs", return_value=None), \
                mock.patch.object(sl, "count_skills", return_value=7) as count:
            self.health(NOW)
            self.health(NOW + sl.HEALTH_TTL + 1)
            self.assertEqual(count.call_count, 1)
            self.health(NOW + sl.SKILLS_TTL + 0.1)
            self.assertEqual(count.call_count, 2)

    def test_corrupt_cache_file_is_ignored(self):
        import json
        from unittest import mock
        for garbage in (b"\x00\xff not json", b"[1, 2]", b"null", b'{"git": "x"}'):
            self.path.write_bytes(garbage)
            with mock.patch.object(sl, "run", return_value=GIT_PORCELAIN) as run:
                got = self.git(NOW)
            self.assertEqual(run.call_count, 1, garbage)
            self.assertEqual(plain(sl.where_item("p", got)), "p › main ~1 ?1")
            self.assertIsInstance(json.loads(self.path.read_text(encoding="utf-8")), dict)

    def test_malformed_entries_are_recomputed(self):
        import json
        from unittest import mock
        with mock.patch.object(sl, "run", return_value=GIT_PORCELAIN) as run:
            self.git(NOW)
            for bad in (5, None, {"at": "soon", "v": ["x"]}, {"at": NOW}, {"v": ["main", 0, 0, 0]},
                        {"at": NOW + 100, "v": ["future", 0, 0, 0]}):
                entries = json.loads(self.path.read_text(encoding="utf-8"))
                self.path.write_text(json.dumps({k: bad for k in entries}), encoding="utf-8")
                got = self.git(NOW + 1)
                self.assertEqual(plain(sl.where_item("p", got)), "p › main ~1 ?1", bad)
        self.assertEqual(run.call_count, 7)

    def test_unwritable_cache_never_breaks_rendering(self):
        from unittest import mock
        self.path.mkdir()  # a folder where the file should be: reads and writes both fail
        with mock.patch.object(sl, "run", return_value=GIT_PORCELAIN) as run:
            self.assertEqual(plain(sl.where_item("p", self.git(NOW))), "p › main ~1 ?1")
            self.git(NOW + 1)
        self.assertEqual(run.call_count, 2)

    def entries(self):
        import json
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_concurrent_sessions_keep_each_others_entries(self):
        # Both sessions loaded the file before either saved: the second save must merge, not overwrite.
        a, b = sl.ProbeCache(self.path, NOW), sl.ProbeCache(self.path, NOW + 0.3)
        a.get("only-a", 10, lambda: ["a"])
        b.get("only-b", 10, lambda: ["b"])
        a.save()
        b.save()
        got = self.entries()
        self.assertEqual({k: e["v"] for k, e in got.items()}, {"only-a": ["a"], "only-b": ["b"]})

    def test_two_threads_saving_different_keys_both_survive(self):
        import threading
        caches = [sl.ProbeCache(self.path, NOW), sl.ProbeCache(self.path, NOW)]
        loaded = threading.Barrier(2)

        def writer(cache, key):
            cache.get(key, 10, lambda: [key])
            loaded.wait()
            cache.save()

        threads = [threading.Thread(target=writer, args=(c, k)) for c, k in zip(caches, ("k1", "k2"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(set(self.entries()), {"k1", "k2"})

    def test_newest_entry_per_key_wins_whoever_saves_last(self):
        older, newer = sl.ProbeCache(self.path, NOW), sl.ProbeCache(self.path, NOW + 1)
        older.put("k", ["old"])
        newer.put("k", ["new"])
        newer.save()
        older.save()
        self.assertEqual(self.entries()["k"]["v"], ["new"])

    def test_entries_from_a_session_a_moment_ahead_are_kept_and_used(self):
        ahead = sl.ProbeCache(self.path, NOW + 0.5)
        ahead.get("k", 10, lambda: ["v"])
        ahead.save()
        behind = sl.ProbeCache(self.path, NOW)
        self.assertEqual(behind.get("k", 10, lambda: self.fail("recomputed")), ["v"])
        behind.get("other", 10, lambda: ["o"])
        behind.save()
        self.assertIn("k", self.entries())

    def test_save_retries_a_sharing_violation(self):
        from unittest import mock
        real = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("in use")
            return real(src, dst)

        cache = sl.ProbeCache(self.path, NOW)
        with mock.patch.object(sl.os, "replace", flaky):
            cache.get("k", 10, lambda: ["v"])  # a claimed compute saves at once
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.entries()["k"]["v"], ["v"])
        self.assertEqual([p.name for p in self.dir.glob("*.tmp")], [])

    def test_only_one_session_recomputes_an_expired_key(self):
        seed =sl.ProbeCache(self.path, NOW)
        seed.get("k", 10, lambda: ["stale"])
        seed.save()
        first, second = sl.ProbeCache(self.path, NOW + 20), sl.ProbeCache(self.path, NOW + 20)
        seen = {}

        def slow_compute():
            # While the first session is computing, the second one renders.
            seen["second"] = second.get("k", 10, lambda: self.fail("second session recomputed"))
            return ["fresh"]

        self.assertEqual(first.get("k", 10, slow_compute), ["fresh"])
        self.assertEqual(seen["second"], ["stale"])

    def test_a_dead_claim_does_not_block_recomputing(self):
        seed = sl.ProbeCache(self.path, NOW)
        seed.get("k", 10, lambda: ["stale"])
        seed.save()
        cache = sl.ProbeCache(self.path, NOW + 20)
        cache.get("k", 10, lambda: ["x"])  # leaves nothing behind once done
        claims = list(self.dir.glob("*.claim"))
        self.assertEqual(claims, [])
        claim = pathlib.Path(f"{self.path}.{sl.zlib.crc32(b'k'):08x}.claim")
        claim.write_bytes(b"")
        os.utime(claim, (time.time() - 60, time.time() - 60))  # a session that died mid-probe
        again = sl.ProbeCache(self.path, NOW + 40)
        self.assertEqual(again.get("k", 10, lambda: ["fresh"]), ["fresh"])

    def test_a_missing_key_is_computed_once_another_sessions_claim_leads_nowhere(self):
        from unittest import mock
        claim = pathlib.Path(f"{self.path}.{sl.zlib.crc32(b'k'):08x}.claim")
        claim.write_bytes(b"")
        with mock.patch.object(sl.ProbeCache, "CLAIM_WAIT", 0.05):
            start = time.monotonic()
            self.assertEqual(sl.ProbeCache(self.path, NOW).get("k", 10, lambda: ["v"]), ["v"])
        self.assertGreaterEqual(time.monotonic() - start, 0.04)  # it waited for the other session first
        self.assertTrue(claim.exists())  # and left the live claim alone

    def test_two_sessions_racing_on_a_missing_key_compute_it_once(self):
        import threading
        computed = []
        started = threading.Event()

        def slow():
            started.set()
            time.sleep(0.08)
            computed.append(1)
            return ["fresh"]

        results = {}

        def session(name, compute):
            results[name] = sl.ProbeCache(self.path, NOW).get("k", 10, compute)

        a = threading.Thread(target=session, args=("a", slow))
        a.start()
        started.wait()
        b = threading.Thread(target=session, args=("b", lambda: computed.append(2) or ["dup"]))
        b.start()
        a.join()
        b.join()
        self.assertEqual(computed, [1])
        self.assertEqual(results, {"a": ["fresh"], "b": ["fresh"]})

    def test_a_won_claim_rereads_the_file_before_computing(self):
        # Between this session's load and its claim, another session stored the value.
        mine = sl.ProbeCache(self.path, NOW)
        other = sl.ProbeCache(self.path, NOW)
        other.get("k", 10, lambda: ["theirs"])
        other.save()
        self.assertEqual(mine.get("k", 10, lambda: self.fail("recomputed")), ["theirs"])

    def test_a_computed_value_is_on_disk_as_soon_as_the_claim_is_released(self):
        cache = sl.ProbeCache(self.path, NOW)
        cache.get("k", 10, lambda: ["v"])
        self.assertEqual(self.entries()["k"]["v"], ["v"])
        self.assertEqual(list(self.dir.glob("*.claim")), [])

    def test_entries_keep_their_own_ttl_in_the_file(self):
        cache = sl.ProbeCache(self.path, NOW)
        cache.get("long", 3600, lambda: ["l"])
        cache.get("short", 3, lambda: ["s"])
        cache.save()
        later = sl.ProbeCache(self.path, NOW + 120)
        later.get("other", 10, lambda: ["o"])
        later.save()
        self.assertEqual(set(self.entries()), {"long", "other"})
        self.assertEqual(later.get("long", 3600, lambda: self.fail("recomputed")), ["l"])


class CachedRendering(TempClaudeDir, unittest.TestCase):
    """Whole renders through main(): probes come from the cache, everything else is live."""

    def probes(self):
        from contextlib import ExitStack
        from unittest import mock
        stack = ExitStack()
        mocks = {name: stack.enter_context(mock.patch.object(sl, name, return_value=value))
                 for name, value in (("run", GIT_PORCELAIN), ("open_ports", [5173]), ("read_mcp_logs", None))}
        return stack, mocks

    WIDE = 300

    def test_second_render_within_ttl_does_not_probe_again(self):
        stack, m = self.probes()
        with stack:
            first = self.render(columns=self.WIDE)
            second = self.render(columns=self.WIDE)
        self.assertEqual(second, first)
        self.assertIn("main ~1 ?1", plain(first))
        self.assertIn(":5173", plain(first))
        self.assertEqual([m[k].call_count for k in ("run", "open_ports", "read_mcp_logs")], [1, 1, 1])
        self.assertTrue((self.dir / sl.PROBE_CACHE_NAME).exists())

    def test_the_session_name_is_not_read_again_until_the_transcript_grows(self):
        from unittest import mock
        transcript = self.dir / "t.jsonl"
        transcript.write_text('{"type":"ai-title","aiTitle":"First name"}\n', encoding="utf-8")
        update = {"transcript_path": str(transcript)}
        stack, _ = self.probes()
        with stack, mock.patch.object(sl, "tail_title", wraps=sl.tail_title) as tail:
            first = plain(self.render(columns=self.WIDE, data_update=update))
            self.render(columns=self.WIDE, data_update=update)
            self.assertEqual(tail.call_count, 1)
            with open(transcript, "a", encoding="utf-8") as f:
                f.write('{"type":"ai-title","aiTitle":"Second name"}\n')
            second = plain(self.render(columns=self.WIDE, data_update=update))
            self.assertEqual(tail.call_count, 2)
        self.assertIn("“first-name”", first)
        self.assertIn("“second-name”", second)

    def test_live_numbers_are_never_cached(self):
        stack, _ = self.probes()
        with stack:
            self.render(columns=self.WIDE)
            out = plain(self.render(columns=self.WIDE, data_update={
                "context_window": {"used_percentage": 77},
                "cost": {"total_cost_usd": 99.25, "total_duration_ms": 120_000}}))
        self.assertIn("ctx 77%", out)
        self.assertIn("$99.25", out)
        self.assertIn("session 2m", out)

    def test_corrupt_cache_file_still_renders(self):
        import json
        (self.dir / sl.PROBE_CACHE_NAME).write_bytes(b"{\"half\": [")
        stack, _ = self.probes()
        with stack:
            out = plain(self.render(columns=self.WIDE))
        self.assertIn("main ~1 ?1", out)
        self.assertIsInstance(json.loads((self.dir / sl.PROBE_CACHE_NAME).read_text(encoding="utf-8")), dict)


class BadUsageCache(TempClaudeDir, unittest.TestCase):
    """A wrongly shaped statusline-usage.json must never blank the line or crash the refresh."""

    GOOD = {"five_hour": {"utilization": 30, "resets_at": "2099-01-01T00:00:00Z"},
            "seven_day": {"utilization": 60, "resets_at": "2099-01-01T00:00:00Z"}}
    BAD = ("[]", "null", '"x"', "42", '{"usage":"x"}', '{"usage":[1]}', '{"usage":{"five_hour":"x"}}',
           '{"usage":{"five_hour":{"utilization":"hi","resets_at":"2099-01-01T00:00:00Z"}}}',
           '{"usage":{"five_hour":{"utilization":5,"resets_at":{"a":1}}}}',
           '{"fetched_at":"x","tags_at":[1],"tags":"x"}', '{"usage":null}')

    def test_load_returns_a_dict_for_any_shape(self):
        for text in self.BAD:
            sl.USAGE_CACHE.write_text(text, encoding="utf-8")
            self.assertIsInstance(sl.load_usage_cache(), dict, text)

    def test_bad_inner_window_is_dropped_but_good_one_kept(self):
        import json
        usage = {"five_hour": {"utilization": "hi", "resets_at": "2099-01-01T00:00:00Z"},
                 "seven_day": self.GOOD["seven_day"]}
        sl.USAGE_CACHE.write_text(json.dumps({"usage": usage, "fetched_at": "x"}), encoding="utf-8")
        got = sl.load_usage_cache()
        self.assertNotIn("five_hour", got["usage"])
        self.assertEqual(got["usage"]["seven_day"]["utilization"], 60)
        self.assertNotIn("fetched_at", got)

    def test_every_render_still_shows_the_line(self):
        from unittest import mock
        for text in self.BAD:
            sl.USAGE_CACHE.write_text(text, encoding="utf-8")
            with mock.patch.object(sl, "maybe_refresh"), mock.patch.object(sl, "read_oauth_token", return_value=None):
                out = plain(self.render(live_usage=True))
            self.assertIn("ctx 43%", out, text)
            self.assertNotIn("Claude | --", out, text)

    def test_unvalidated_merge_failure_falls_back_to_stdin_data(self):
        from unittest import mock
        with mock.patch.object(sl, "load_usage_cache", return_value={"usage": {"five_hour": {"utilization": "hi"}}}), \
                mock.patch.object(sl, "maybe_refresh"):
            out = plain(self.render(live_usage=True))
        self.assertIn("5h 19%", out)

    def test_refresh_survives_and_rewrites_a_bad_file(self):
        import json
        from unittest import mock
        for text in self.BAD:
            sl.USAGE_CACHE.write_text(text, encoding="utf-8")
            with mock.patch.object(sl, "_fetch_usage"), mock.patch("urllib.request.urlopen", side_effect=OSError):
                sl.refresh_cache()
            self.assertIsInstance(json.loads(sl.USAGE_CACHE.read_text(encoding="utf-8")), dict, text)


class CheapWins(unittest.TestCase):
    def test_ctypes_is_not_imported_when_the_terminal_announces_itself(self):
        from unittest import mock
        for var in ("WT_SESSION", "TERM_PROGRAM"):
            fake = mock.MagicMock()
            with mock.patch.dict(os.environ, {var: "x"}), mock.patch.dict(sys.modules, {"ctypes": fake}), \
                    mock.patch.object(sl.os, "name", "nt"):
                got = sl.terminal_kind()
            self.assertEqual(fake.mock_calls, [], var)
            self.assertIsNone(got)
            with mock.patch.dict(os.environ, {var: "x"}):
                self.assertEqual(sl.effective_icons("nerd"), "nerd")

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_the_real_probe_runs_in_a_few_milliseconds_and_returns_a_verdict_or_none(self):
        start = time.perf_counter()
        got = sl._probe_console_host()
        self.assertLess(time.perf_counter() - start, 0.2)
        self.assertIn(got, ("classic", "pty", None))

    def test_the_console_verdict_is_asked_once_per_parent_and_kept_an_hour(self):
        import tempfile
        from unittest import mock
        env = {k: v for k, v in os.environ.items() if k not in ("WT_SESSION", "TERM_PROGRAM")}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sl, "_probe_console_host", return_value="classic") as probe, \
                mock.patch.object(sl.os, "name", "nt"):
            path = os.path.join(tmp, sl.PROBE_CACHE_NAME)
            first = sl.ProbeCache(path, NOW)
            self.assertEqual(sl.effective_icons("nerd", first), "none")
            first.save()
            later = sl.ProbeCache(path, NOW + 3000)
            self.assertEqual(sl.effective_icons("nerd", later), "none")
            self.assertEqual(probe.call_count, 1)
            with open(path, encoding="utf-8") as f:
                self.assertIn(f"console:{os.getppid()}", f.read())
            expired = sl.ProbeCache(path, NOW + sl.CONSOLE_TTL + 1)
            self.assertEqual(sl.effective_icons("nerd", expired), "none")
            self.assertEqual(probe.call_count, 2)
            with mock.patch.dict(os.environ, {"WT_SESSION": "x"}):
                self.assertEqual(sl.effective_icons("nerd", later), "nerd")  # the env still wins, no probe needed
            self.assertEqual(probe.call_count, 2)

    def test_the_verdict_is_keyed_by_session_so_a_changing_parent_pid_still_hits(self):
        import json
        import tempfile
        env = {k: v for k, v in os.environ.items() if k not in ("WT_SESSION", "TERM_PROGRAM")}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sl, "_probe_console_host", return_value="pty") as probe, \
                mock.patch.object(sl.os, "name", "nt"):
            path = os.path.join(tmp, sl.PROBE_CACHE_NAME)
            for parent in (101, 102, 103):  # Claude Code runs the status line through a new shell each time
                with mock.patch.object(sl.os, "getppid", return_value=parent):
                    cache = sl.ProbeCache(path, NOW)
                    self.assertEqual(sl.effective_icons("nerd", cache, "sess-1"), "nerd")
                    cache.save()
            self.assertEqual(probe.call_count, 1)
            with open(path, encoding="utf-8") as f:
                keys = [k for k in json.load(f) if k.startswith("console:")]
            self.assertEqual(keys, ["console:sess-1"])

    def test_old_terminal_verdicts_are_capped_so_the_cache_cannot_grow_without_bound(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, sl.PROBE_CACHE_NAME)
            cache = sl.ProbeCache(path, NOW)
            for i in range(300):
                cache.entries[f"console:{i}"] = {"at": NOW - 300 + i, "v": "pty", "ttl": sl.CONSOLE_TTL}
                cache.touched.add(f"console:{i}")
            cache.dirty = True
            cache.save()
            with open(path, encoding="utf-8") as f:
                kept = [k for k in json.load(f) if k.startswith("console:")]
            self.assertEqual(len(kept), sl.ProbeCache.MAX_VERDICTS)
            self.assertIn("console:299", kept)
            self.assertNotIn("console:0", kept)

    def test_port_probe_waits_20ms(self):
        import inspect
        self.assertEqual(inspect.signature(sl.open_ports).parameters["timeout"].default, 0.02)

    def test_a_local_listener_is_found_every_time_and_a_closed_port_is_not(self):
        import socket
        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(50)
            port = srv.getsockname()[1]
            with socket.socket() as spare:
                spare.bind(("127.0.0.1", 0))
                closed = spare.getsockname()[1]
            for _ in range(20):
                self.assertEqual(sl.open_ports([closed, port]), [port])


class StandaloneRun(TempClaudeDir, unittest.TestCase):
    """The status line runs as `python -S statusline.py --icons nerd`: stdlib only, no site-packages."""

    def test_runs_under_python_dash_S_as_one_line(self):
        import json
        import subprocess
        data = dict(SingleLine.DATA, workspace={"current_dir": str(self.proj)},
                    rate_limits={"five_hour": {"used_percentage": 74, "resets_at": time.time() + 7800},
                                 "seven_day": {"used_percentage": 69, "resets_at": time.time() + 400_000}})
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.dir), STATUSLINE_LIVE_USAGE="0", COLUMNS="120",
                   WT_SESSION="x", PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, "-S", sl.__file__, "--icons", "nerd"], input=json.dumps(data),
                           capture_output=True, text=True, encoding="utf-8", env=env, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        out = plain(r.stdout.rstrip("\n"))
        self.assertNotIn("\n", out)
        self.assertLessEqual(sl.visible_width(out), 120 - sl.EDGE)
        for want in ("my-special-proj", "Opus 5.5", "ctx 43%", "5h 74%", "7d 69%", "35.50"):
            self.assertIn(want, out)


class Utf8Everywhere(TempClaudeDir, unittest.TestCase):
    """stdin and git output are UTF-8 whatever the console code page says."""

    def test_hebrew_and_accents_render_under_a_cp1252_console(self):
        import json
        import subprocess
        proj = self.proj.parent / "פרויקט-café"
        proj.mkdir()
        data = dict(SingleLine.DATA, workspace={"current_dir": str(proj)}, session_name="משימה naïve")
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.dir), STATUSLINE_LIVE_USAGE="0", COLUMNS="200",
                   WT_SESSION="x", PYTHONIOENCODING="cp1252")
        r = subprocess.run([sys.executable, "-S", sl.__file__], input=json.dumps(data, ensure_ascii=False).encode("utf-8"),
                           capture_output=True, env=env, timeout=20)
        out = plain(r.stdout.decode("utf-8").rstrip("\r\n"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("פרויקט-café", out)
        self.assertIn("“משימה-naïve”", out)

    def test_stdin_without_a_buffer_is_still_read(self):
        out = plain(self.render(columns=200, data_update={"session_name": "Caf\u00e9"}))  # StringIO stdin
        self.assertIn("“café”", out)


class GitOutputEncoding(unittest.TestCase):
    def test_git_output_is_decoded_as_utf8(self):
        from unittest import mock
        with mock.patch("subprocess.run") as run:
            sl.run(["git", "status"])
        self.assertEqual(run.call_args.kwargs.get("encoding"), "utf-8")
        self.assertEqual(run.call_args.kwargs.get("errors"), "replace")


class LineWidth(unittest.TestCase):
    def width_for(self, columns):
        from unittest import mock
        env = {k: v for k, v in os.environ.items() if k != "COLUMNS"}
        if columns is not None:
            env["COLUMNS"] = columns
        with mock.patch.dict(os.environ, env, clear=True):
            return sl.line_width()

    def test_narrow_windows_are_clamped_not_unlimited(self):
        self.assertEqual(self.width_for("21"), 20)
        self.assertEqual(self.width_for("24"), 20)
        self.assertEqual(self.width_for("10"), 20)
        self.assertEqual(self.width_for("120"), 116)

    def test_unknown_or_invalid_width_means_unlimited(self):
        for columns in (None, "", "0", "-5", "abc"):
            self.assertIsNone(self.width_for(columns), columns)

    def test_long_model_names_are_cut_in_the_middle(self):
        data = {"model": {"display_name": "Claude Opus 4.1 Experimental Preview Build"}}
        out = plain(sl.format_statusline(data, now=NOW, show_usage=False))
        head = out.split(" │ ")[0]
        self.assertLessEqual(len(head), 20)
        self.assertIn("…", head)
        self.assertTrue(head.startswith("Claude Op"), head)

    def test_no_width_overflows_for_a_grid_of_payloads(self):
        names = ["Opus 5.5", "Claude Sonnet 4.6 Preview Experimental Build", "X"]
        efforts = [None, "max"]
        pcts = [(0, 0, 0), (100, 100, 100), (43, 74, 69)]
        marks = [[], [("▲ discord +2", sl.FAILED, "discord"), ("↑ 2.2.0", sl.UPDATE, "2.2.0")]]
        count = 0
        for name in names:
            for effort in efforts:
                for ctx, five, seven in pcts:
                    data = {"model": {"display_name": name}, "effort": {"level": effort} if effort else None,
                            "context_window": {"used_percentage": ctx, "context_window_size": 1_000_000,
                                               "current_usage": {"input_tokens": ctx * 10_000}},
                            "cost": {"total_cost_usd": 1234.5, "total_duration_ms": 10 * HOUR * 1000}}
                    usage = sl.merge_usage({}, live(five=(five, NOW + HOUR), seven=(seven, NOW + 3 * 24 * HOUR)), NOW)
                    for m in marks:
                        extras = {"proj": "claude-code-statusline", "git": ("live-usage-and-redesign", ["~2"]),
                                  "name": "statusline-py-usage-accuracy", "skills": 69, "marks": m,
                                  "ports": ":3000", "venv": "venv:x"}
                        for width in range(40, 301, 7):
                            out = sl.format_statusline(data, usage, NOW, True, extras, width)
                            count += 1
                            self.assertNotIn("\n", out)
                            # the floor: model (8), three percentages and their countdowns, 47 columns at 100%
                            self.assertLessEqual(sl.visible_width(out), max(width, 47), (name, effort, ctx, width, plain(out)))
                            if width < 47:
                                self.assertRegex(plain(out), r"^.{1,8}( · \S+)? │ ctx \d+% │ 5h \d+%( →\S+)? │ 7d \d+%( →\S+)?( │ \S+)?$")
        self.assertGreater(count, 200)


class TerminalCells(unittest.TestCase):
    """visible_width counts terminal cells, not code points."""

    def test_ascii_and_colors(self):
        self.assertEqual(sl.visible_width(f"{sl.GREEN}abc{sl.RST} d"), 5)

    def test_wide_and_fullwidth_characters_take_two_cells(self):
        self.assertEqual(sl.visible_width("日本語"), 6)
        self.assertEqual(sl.visible_width("Ａ"), 2)  # fullwidth A
        self.assertEqual(sl.visible_width("x\U0001F600y"), 4)  # emoji

    def test_zero_width_characters_take_none(self):
        self.assertEqual(sl.visible_width("e\u0301"), 1)  # combining acute
        self.assertEqual(sl.visible_width("a\u200bb\u200d"), 2)
        self.assertEqual(sl.visible_width("\u2713\ufe0f"), 2)  # check mark in emoji presentation
        self.assertEqual(sl.visible_width("\u2713"), 1)  # text presentation: one cell

    def test_hebrew_and_nerd_glyphs_are_one_cell(self):
        self.assertEqual(sl.visible_width("שלום"), 4)
        # private-use glyphs are one cell each (the bar segment is two glyphs)
        self.assertEqual(sl.visible_width(sl.NERD_ICONS["segment"] + sl.NERD_ICONS["warn"]), 3)

    def test_unicodedata_is_not_imported_for_ascii(self):
        from unittest import mock
        with mock.patch.dict(sys.modules, {"unicodedata": None}):  # import would raise ImportError
            self.assertEqual(sl.visible_width(f"{sl.DIM}plain ascii │ ok{sl.RST}".replace("│", "|")), 16)


class SmallBugs(unittest.TestCase):
    def test_mcp_log_dir_key_keeps_only_ascii_alphanumerics(self):
        root = sl.mcp_log_root("C:\\dev\\פרויקט café_1")  # six Hebrew letters, a space, an accent, an underscore
        self.assertTrue(root.endswith("C--dev--------caf--1"), root)

    def test_malformed_percentages_count_as_missing(self):
        for bad in ("n/a", "", [74], None, float("nan")):
            self.assertIsNone(sl._window(bad, NOW + HOUR, NOW), bad)
        got = sl.merge_usage({"five_hour": 5, "seven_day": {"used_percentage": "", "resets_at": NOW + HOUR}}, None, NOW)
        self.assertEqual(got, {"five_hour": None, "seven_day": None})
        out = plain(sl.format_statusline({"model": {"display_name": "Opus"}}, usage=got, now=NOW))
        self.assertIn("5h -- │ 7d --", out)

    def test_null_display_name_falls_back_to_claude(self):
        for model in ({"display_name": None}, {}, None):
            out = plain(sl.format_statusline({"model": model}, now=NOW, show_usage=False))
            self.assertTrue(out.startswith("Claude │"), out)

    def test_oauth_token_is_none_for_any_bad_credentials_file(self):
        import json
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            creds = pathlib.Path(tmp) / ".credentials.json"
            with mock.patch.object(sl, "CLAUDE_DIR", tmp), mock.patch.object(sl, "run", return_value=""):
                for text in ('{"claudeAiOauth": null}', '{"claudeAiOauth": {"accessToken": "t", "expiresAt": "soon"}}',
                             '{"claudeAiOauth": []}', "not json", ""):
                    creds.write_text(text, encoding="utf-8")
                    self.assertIsNone(sl.read_oauth_token(), text)
                creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok", "expiresAt": 9e15}}), encoding="utf-8")
                self.assertEqual(sl.read_oauth_token(), "tok")

    def test_plugins_without_an_install_path_are_skipped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "skills" / "here").mkdir(parents=True)
            (root / "skills" / "here" / "SKILL.md").write_text("x")
            installed = {"plugins": {"p@m": [{}], "q@m": [{"installPath": None}]}}
            cwd = os.getcwd()
            os.chdir(tmp)  # a glob of "" would count the cwd's skills
            try:
                self.assertEqual(sl.count_skills(installed, {"p@m", "q@m"}, root / "nowhere"), 0)
            finally:
                os.chdir(cwd)


class UsageLockTakeover(TempClaudeDir, unittest.TestCase):
    def stale_lock(self):
        sl.USAGE_LOCK.write_text("dead", encoding="utf-8")
        os.utime(sl.USAGE_LOCK, (time.time() - 120, time.time() - 120))

    def test_a_stale_lock_is_taken_over(self):
        self.stale_lock()
        self.assertTrue(sl._take_lock(time.time(), "me"))
        self.assertEqual(sl.USAGE_LOCK.read_text(encoding="utf-8"), "me")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if "lock" in p.name),
                         sorted([sl.USAGE_LOCK.name, sl.USAGE_LOCK.name + ".guard"]))

    def test_a_fresh_lock_is_respected(self):
        sl.USAGE_LOCK.write_text("busy", encoding="utf-8")
        self.assertFalse(sl._take_lock(time.time(), "me"))
        self.assertEqual(sl.USAGE_LOCK.read_text(encoding="utf-8"), "busy")

    def test_losing_the_takeover_race_leaves_the_winners_lock_alone(self):
        from unittest import mock
        self.stale_lock()
        def someone_else_first(src, dst):
            # Between our stat and our move, another session moved the stale lock away and made its own.
            sl.USAGE_LOCK.write_text("winner", encoding="utf-8")
            raise FileNotFoundError(src)

        with mock.patch.object(sl.os, "replace", someone_else_first):
            self.assertFalse(sl._take_lock(time.time(), "me"))
        self.assertEqual(sl.USAGE_LOCK.read_text(encoding="utf-8"), "winner")


class FastLaunch(TempClaudeDir, unittest.TestCase):
    """main() is importable: python -S -c "...import statusline; statusline.main()" --icons nerd."""

    def test_import_form_matches_the_script_form_and_reuses_its_pycache(self):
        import json
        import shutil
        import subprocess
        home = self.proj.parent / "home"
        home.mkdir()
        shutil.copy(sl.__file__, home / "statusline.py")
        data = dict(SingleLine.DATA, workspace={"current_dir": str(self.proj)},
                    rate_limits={"five_hour": {"used_percentage": 74, "resets_at": 9e9}})
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.dir), STATUSLINE_LIVE_USAGE="0", COLUMNS="120",
                   WT_SESSION="x", PYTHONIOENCODING="utf-8")
        fast = [sys.executable, "-S", "-c",
                f"import os,sys;sys.path.insert(0,{str(home)!r});import statusline;statusline.main()", "--icons", "nerd"]
        script = [sys.executable, "-S", str(home / "statusline.py"), "--icons", "nerd"]
        outs = []
        for cmd in (fast, fast, script):
            r = subprocess.run(cmd, input=json.dumps(data), capture_output=True, text=True, encoding="utf-8",
                               env=env, timeout=20, cwd=str(self.proj))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stderr, "")
            outs.append(r.stdout)
            pyc = list((home / "__pycache__").glob("statusline.*.pyc"))
            self.assertEqual(len(pyc), 1, pyc)
            outs.append(pyc[0].stat().st_mtime_ns)
        self.assertEqual(outs[0], outs[2])
        self.assertEqual(outs[0], outs[4])  # the script form renders the very same line
        self.assertEqual(outs[1], outs[3])  # the second launch reused the compiled module
        self.assertIn(sl.NERD_ICONS["segment"], outs[0])
        self.assertIn("5h 74%", plain(outs[0]))

    def test_refresh_child_is_spawned_from_the_module_file(self):
        from unittest import mock
        with mock.patch("subprocess.Popen") as popen, mock.patch.object(sys, "argv", ["-c", "--icons", "nerd"]):
            sl.maybe_refresh({}, NOW)
        self.assertEqual(popen.call_args.args[0][1], os.path.abspath(sl.__file__))


class RefreshProcess(TempClaudeDir, unittest.TestCase):
    """The detached --refresh child: its lock is its own, and it never outlives its deadline."""

    def run_refresh(self, argv, refresh=None):
        from unittest import mock
        with mock.patch.object(sys, "argv", ["statusline.py"] + argv), \
                mock.patch.object(sl, "refresh_cache", refresh or (lambda: None)) as fn, \
                mock.patch.object(sl, "_arm_deadline"):
            sl.main()
        return fn

    def test_maybe_refresh_hands_the_child_its_lock_token(self):
        from unittest import mock
        with mock.patch("subprocess.Popen") as popen:
            sl.maybe_refresh({}, NOW)
        args = popen.call_args.args[0]
        token = args[args.index("--lock-token") + 1]
        self.assertIn("--refresh", args)
        self.assertEqual(sl.USAGE_LOCK.read_text(encoding="utf-8"), token)

    def test_a_failed_spawn_frees_only_our_own_lock(self):
        from unittest import mock
        with mock.patch("subprocess.Popen", side_effect=OSError):
            sl.maybe_refresh({}, NOW)
        self.assertFalse(sl.USAGE_LOCK.exists())

    def test_child_removes_the_lock_it_was_given(self):
        sl.USAGE_LOCK.write_text("tok", encoding="utf-8")
        fn = self.run_refresh(["--refresh", "--lock-token", "tok"], refresh=lambda: None)
        self.assertFalse(sl.USAGE_LOCK.exists())

    def test_child_leaves_a_lock_that_is_not_its_own(self):
        sl.USAGE_LOCK.write_text("someone-else", encoding="utf-8")
        self.run_refresh(["--refresh", "--lock-token", "tok"])
        self.assertEqual(sl.USAGE_LOCK.read_text(encoding="utf-8"), "someone-else")

    def test_child_removes_nothing_when_refreshing_crashes_after_losing_its_lock(self):
        def crash():
            sl.USAGE_LOCK.write_text("newer-owner", encoding="utf-8")
            raise RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.run_refresh(["--refresh", "--lock-token", "tok"], refresh=crash)
        self.assertEqual(sl.USAGE_LOCK.read_text(encoding="utf-8"), "newer-owner")

    def test_a_manual_refresh_takes_the_lock_itself_or_gives_up(self):
        calls = []
        sl.USAGE_LOCK.write_text("running", encoding="utf-8")
        self.run_refresh(["--refresh"], refresh=lambda: calls.append(1))
        self.assertEqual(calls, [])
        self.assertEqual(sl.USAGE_LOCK.read_text(encoding="utf-8"), "running")
        sl.USAGE_LOCK.unlink()
        self.run_refresh(["--refresh"], refresh=lambda: calls.append(1))
        self.assertEqual(calls, [1])
        self.assertFalse(sl.USAGE_LOCK.exists())

    def test_deadline_timer_is_a_15_second_daemon(self):
        from unittest import mock
        with mock.patch("threading.Timer") as timer:
            sl._arm_deadline("tok")
        interval, fn = timer.call_args.args[:2]
        self.assertEqual(interval, 15)
        self.assertTrue(timer.return_value.daemon)
        timer.return_value.start.assert_called_once()
        self.assertIs(fn, sl._expire)

    def test_expiring_exits_and_unlocks_only_our_lock(self):
        from unittest import mock
        sl.USAGE_LOCK.write_text("tok", encoding="utf-8")
        with mock.patch.object(sl.os, "_exit") as leave:
            sl._expire("tok")
        leave.assert_called_once()
        self.assertFalse(sl.USAGE_LOCK.exists())
        sl.USAGE_LOCK.write_text("other", encoding="utf-8")
        with mock.patch.object(sl.os, "_exit"):
            sl._expire("tok")
        self.assertTrue(sl.USAGE_LOCK.exists())

    def test_a_hung_refresh_process_really_exits_at_the_deadline(self):
        import subprocess
        script = (
            "import sys, time, statusline as sl\n"
            "sl.REFRESH_DEADLINE = 0.3\n"
            "sl.refresh_cache = lambda: time.sleep(30)\n"
            "sys.argv = ['statusline.py', '--refresh', '--lock-token', 'tok']\n"
            "sl.main()\n")
        sl.USAGE_LOCK.write_text("tok", encoding="utf-8")
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.dir))
        start = time.monotonic()
        r = subprocess.run([sys.executable, "-c", script], cwd=str(pathlib.Path(sl.__file__).parent),
                           env=env, timeout=20, capture_output=True)
        self.assertLess(time.monotonic() - start, 10, r.stderr)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(sl.USAGE_LOCK.exists())


class InstallCommand(unittest.TestCase):
    """`statusline.py install` / `uninstall` (and the claude-statusline entry point that calls main())."""

    def setUp(self):
        import json
        import tempfile
        self.json = json
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name) / "claude"
        self.dir.mkdir()
        self.settings = self.dir / "settings.json"

    def run_cli(self, *args):
        argv = ["statusline.py", *args, "--claude-dir", str(self.dir)]
        with mock.patch.object(sys, "argv", argv), mock.patch("builtins.print") as shown:
            code = sl.main()
        return code, "\n".join(str(c.args[0]) for c in shown.call_args_list if c.args)

    def load(self):
        return self.json.loads(self.settings.read_text(encoding="utf-8"))

    def test_install_copies_the_script_and_sets_the_status_line_keeping_other_settings(self):
        self.settings.write_text(self.json.dumps({"model": "opus", "env": {"A": "1"}}), encoding="utf-8")
        code, out = self.run_cli("install", "--icons", "none", "--no-test")
        self.assertEqual(code, 0, out)
        data = self.load()
        self.assertEqual((data["model"], data["env"]), ("opus", {"A": "1"}))
        line = data["statusLine"]
        self.assertEqual((line["type"], line["refreshInterval"]), ("command", 1))
        self.assertIn(" -S -c ", line["command"])
        self.assertIn("statusline.main()", line["command"])
        self.assertNotIn("--icons", line["command"])
        self.assertTrue((self.dir / "statusline.py").is_file())
        self.assertEqual(len(list((self.dir / "backups").glob("statusline-*/settings.json"))), 1)

    def test_nerd_icons_add_the_flag_and_the_command_points_at_a_custom_dir(self):
        code, out = self.run_cli("install", "--icons", "nerd", "--no-test", "--no-font")
        self.assertEqual(code, 0, out)
        command = self.load()["statusLine"]["command"]
        self.assertTrue(command.endswith(" --icons nerd"))
        self.assertIn(os.fsencode(str(self.dir)).hex(), command)

    def test_the_default_dir_is_written_as_tilde_claude(self):
        command = sl._launch_command("python", os.path.join(sl.HOME, ".claude"), "nerd")
        self.assertIn("expanduser('~/.claude')", command)
        self.assertTrue(command.startswith('python -S -c "'))

    def test_installing_twice_keeps_the_original_status_line_for_uninstall(self):
        before = {"type": "command", "command": "my-own-line"}
        self.settings.write_text(self.json.dumps({"statusLine": before}), encoding="utf-8")
        self.assertEqual(self.run_cli("install", "--icons", "none", "--no-test")[0], 0)
        self.assertEqual(self.run_cli("install", "--icons", "none", "--no-test")[0], 0)
        self.assertEqual(self.run_cli("uninstall")[0], 0)
        self.assertEqual(self.load()["statusLine"], before)
        self.assertFalse((self.dir / "statusline.py").exists())
        self.assertFalse((self.dir / sl.INSTALL_RECORD).exists())

    def test_uninstall_without_a_previous_line_removes_ours_only(self):
        self.settings.write_text(self.json.dumps({"model": "opus"}), encoding="utf-8")
        self.run_cli("install", "--icons", "none", "--no-test")
        self.assertEqual(self.run_cli("uninstall")[0], 0)
        self.assertEqual(self.load(), {"model": "opus"})

    def test_uninstall_leaves_a_foreign_status_line_alone(self):
        self.settings.write_text(self.json.dumps({"statusLine": {"command": "other-tool"}}), encoding="utf-8")
        code, out = self.run_cli("uninstall")
        self.assertEqual(code, 0)
        self.assertEqual(self.load(), {"statusLine": {"command": "other-tool"}})
        self.assertIn("left settings.json alone", out)

    def test_an_unparseable_settings_file_is_never_touched(self):
        self.settings.write_text("{ not json", encoding="utf-8")
        code, out = self.run_cli("install", "--icons", "none", "--no-test")
        self.assertEqual(code, 1)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{ not json")
        self.assertFalse((self.dir / "statusline.py").exists())
        self.assertIn("nothing was changed", out)

    def test_dry_run_writes_nothing(self):
        code, out = self.run_cli("install", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [])
        self.assertIn("statusLine.command", out)

    def test_a_bad_icons_value_prints_usage_and_changes_nothing(self):
        code, out = self.run_cli("install", "--icons", "wild")
        self.assertEqual(code, 2)
        self.assertIn("Usage:", out)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [])

    def test_a_bom_in_settings_is_read_and_the_file_written_back_without_one(self):
        self.settings.write_bytes(b"\xef\xbb\xbf" + self.json.dumps({"model": "x"}).encode())
        self.assertEqual(self.run_cli("install", "--icons", "none", "--no-test")[0], 0)
        self.assertFalse(self.settings.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual(self.load()["model"], "x")

    def test_the_self_test_runs_the_installed_command_and_a_failure_rolls_settings_back(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.dir)}):
            code, out = self.run_cli("install", "--icons", "none")
        self.assertEqual(code, 0, out)
        self.assertIn("self-test ok", out)
        self.settings.write_text(self.json.dumps({"model": "x"}), encoding="utf-8")
        with mock.patch.object(sl, "_smoke_test", return_value=(False, "boom")):
            code, out = self.run_cli("install", "--icons", "none")
        self.assertEqual(code, 1)
        self.assertEqual(self.load(), {"model": "x"})
        self.assertIn("put back", out)

    WT = """{
    // keep this comment { with a brace
    "defaultProfile": "{61c54bbd}",
    "profiles": {
        "defaults": {
            "cursorShape": "bar"
        },
        "list": [{ "name": "PowerShell", "font": { "face": "Consolas" } }]
    }
}"""

    def test_wt_gets_a_font_when_defaults_name_none_and_comments_survive(self):
        new, outcome = sl._wt_font_edit(self.WT, "Foo Nerd Font")
        self.assertEqual(outcome, "set")
        self.assertIn('"defaults": { "font": { "face": "Foo Nerd Font" },', new)
        self.assertIn("// keep this comment { with a brace", new)
        self.assertEqual(sl._wt_font_edit(new, "Foo Nerd Font")[1], "kept")
        self.assertIn('"face": "Consolas"', new)  # another profile's own font is not touched

    def test_wt_replaces_a_non_nerd_default_face_and_keeps_a_nerd_one(self):
        text = '{"profiles":{"defaults":{"font":{"face":"Cascadia Mono","size":11}},"list":[]}}'
        new, outcome = sl._wt_font_edit(text, "Foo Nerd Font")
        self.assertEqual(outcome, "set")
        self.assertEqual(self.json.loads(new)["profiles"]["defaults"]["font"],
                         {"face": "Foo Nerd Font", "size": 11})
        text = '{"profiles":{"defaults":{"font":{"face":"JetBrainsMono Nerd Font"}}}}'
        self.assertEqual(sl._wt_font_edit(text, "Foo Nerd Font"), (text, "kept"))

    def test_wt_font_object_without_a_face_gets_one_and_no_defaults_block_is_reported(self):
        text = '{"profiles":{"defaults":{"font":{"size":12}}}}'
        new, outcome = sl._wt_font_edit(text, "Foo Nerd Font")
        self.assertEqual(self.json.loads(new)["profiles"]["defaults"]["font"],
                         {"face": "Foo Nerd Font", "size": 12})
        self.assertEqual(sl._wt_font_edit('{"profiles":{"list":[]}}', "X")[1], "none")

    def test_vscode_font_family_is_added_or_prefixed_but_a_nerd_font_is_kept(self):
        text = '{\n  // editor\n  "editor.fontSize": 14\n}'
        new, outcome = sl._vscode_font_edit(text, "Foo Nerd Font")
        self.assertEqual(outcome, "set")
        self.assertIn('"terminal.integrated.fontFamily": "Foo Nerd Font",', new)
        self.assertIn("// editor", new)
        text = '{"terminal.integrated.fontFamily": "Consolas"}'
        self.assertEqual(self.json.loads(sl._vscode_font_edit(text, "Foo Nerd Font")[0])
                         ["terminal.integrated.fontFamily"], "Foo Nerd Font, Consolas")
        keep = '{"terminal.integrated.fontFamily": "Hack Nerd Font Mono"}'
        self.assertEqual(sl._vscode_font_edit(keep, "Foo Nerd Font"), (keep, "kept"))

    def test_the_installed_face_is_read_from_the_file_names(self):
        fonts = pathlib.Path(self.tmp.name) / "fonts"
        fonts.mkdir()
        for name in ("HackNerdFontMono-Regular.ttf", "HackNerdFontPropo-Regular.ttf", "CaskaydiaMonoNerdFont-Regular.ttf",
                     "HackNerdFont-Regular.ttf", "Arial.ttf"):
            (fonts / name).write_bytes(b"")
        with mock.patch.object(sl, "_font_dirs", return_value=[str(fonts)]):
            self.assertEqual(sl._nerd_font_face(), "CaskaydiaMono Nerd Font")
            (fonts / "JetBrainsMonoNerdFont-Regular.ttf").write_bytes(b"")
            self.assertEqual(sl._nerd_font_face(), "JetBrainsMono Nerd Font")
        for p in fonts.iterdir():
            p.unlink()
        (fonts / "HackNerdFont-Regular.ttf").write_bytes(b"")
        with mock.patch.object(sl, "_font_dirs", return_value=[str(fonts)]):
            self.assertEqual(sl._nerd_font_face(), "Hack Nerd Font")
        with mock.patch.object(sl, "_font_dirs", return_value=[str(fonts / "none")]):
            self.assertIsNone(sl._nerd_font_face())
            self.assertFalse(sl._has_nerd_font())

    def test_fonts_are_extracted_from_the_release_zip_by_style(self):
        import zipfile
        z = pathlib.Path(self.tmp.name) / "f.zip"
        with zipfile.ZipFile(z, "w") as zf:
            for s in ("Regular", "Bold", "Italic", "BoldItalic", "Light"):
                zf.writestr(f"CaskaydiaMonoNerdFont-{s}.ttf", b"x")
            zf.writestr("CaskaydiaMonoNerdFontMono-Regular.ttf", b"x")
            zf.writestr("README.md", b"x")
        written = sl._extract_fonts(str(z), str(pathlib.Path(self.tmp.name) / "out"))
        self.assertEqual(sorted(os.path.basename(p) for p in written),
                         [f"CaskaydiaMonoNerdFont-{s}.ttf" for s in ("Bold", "BoldItalic", "Italic", "Regular")])

    def test_windows_font_registration_names_values_the_way_windows_does(self):
        values = {}

        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        fake = mock.Mock(HKEY_CURRENT_USER=1, KEY_SET_VALUE=2, REG_SZ=3)
        fake.CreateKeyEx.return_value = Key()
        fake.SetValueEx.side_effect = lambda key, name, _r, _t, path: values.__setitem__(name, path)
        with mock.patch.dict(sys.modules, {"winreg": fake}):
            sl._register_fonts_windows([r"C:\f\CaskaydiaMonoNerdFont-Regular.ttf", r"C:\f\CaskaydiaMonoNerdFont-BoldItalic.ttf"])
        self.assertEqual(sorted(values), ["CaskaydiaMono Nerd Font Bold Italic (TrueType)",
                                          "CaskaydiaMono Nerd Font Regular (TrueType)"])
        self.assertEqual(fake.CreateKeyEx.call_args.args[1], r"Software\Microsoft\Windows NT\CurrentVersion\Fonts")

    def test_install_with_nerd_icons_installs_a_missing_font_and_sets_the_terminal_font(self):
        wt = pathlib.Path(self.tmp.name) / "wt.json"
        wt.write_text(self.WT, encoding="utf-8")
        with mock.patch.object(sl, "_nerd_font_face", side_effect=[None, "Foo Nerd Font"]), \
                mock.patch.object(sl, "_has_nerd_font", side_effect=[False, True, True]), \
                mock.patch.object(sl, "_install_nerd_font", return_value="Foo Nerd Font") as fetch, \
                mock.patch.object(sl, "_terminal_settings_files", return_value=[("Windows Terminal", str(wt), sl._wt_font_edit)]):
            code, out = self.run_cli("install", "--no-test")
        self.assertEqual(code, 0, out)
        fetch.assert_called_once()
        self.assertIn('"face": "Foo Nerd Font"', wt.read_text(encoding="utf-8"))
        self.assertTrue(self.load()["statusLine"]["command"].endswith("--icons nerd"))
        self.assertEqual(len(list((self.dir / "backups").glob("statusline-*/windows-terminal-wt.json"))), 1)

    def test_no_font_flag_and_none_icons_never_touch_fonts_or_terminals(self):
        with mock.patch.object(sl, "_install_nerd_font") as fetch, \
                mock.patch.object(sl, "_terminal_settings_files") as files:
            self.run_cli("install", "--no-test", "--no-font", "--icons", "nerd")
            self.run_cli("install", "--no-test", "--icons", "none")
        fetch.assert_not_called()
        files.assert_not_called()

    def test_a_failed_font_install_still_installs_the_status_line_with_plain_icons(self):
        with mock.patch.object(sl, "_nerd_font_face", return_value=None), \
                mock.patch.object(sl, "_has_nerd_font", return_value=False), \
                mock.patch.object(sl, "_install_nerd_font", return_value=None):
            code, out = self.run_cli("install", "--no-test")
        self.assertEqual(code, 0, out)
        self.assertNotIn("--icons", self.load()["statusLine"]["command"])

    def test_dry_run_reports_the_font_work_and_writes_nothing(self):
        wt = pathlib.Path(self.tmp.name) / "wt.json"
        wt.write_text(self.WT, encoding="utf-8")
        with mock.patch.object(sl, "_nerd_font_face", return_value=None), \
                mock.patch.object(sl, "_has_nerd_font", return_value=False), \
                mock.patch.object(sl, "_install_nerd_font") as fetch, \
                mock.patch.object(sl, "_terminal_settings_files", return_value=[("Windows Terminal", str(wt), sl._wt_font_edit)]):
            code, out = self.run_cli("install", "--dry-run")
        self.assertEqual(code, 0)
        fetch.assert_not_called()
        self.assertIn("would install the Nerd Font", out)
        self.assertIn("would set the font", out)
        self.assertEqual(wt.read_text(encoding="utf-8"), self.WT)

    def test_script_is_runnable_as_python_dash_s_install_help(self):
        import subprocess
        r = subprocess.run([sys.executable, "-S", sl.__file__, "install", "--help"], capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("claude-statusline install", r.stdout)


if __name__ == "__main__":
    unittest.main()
