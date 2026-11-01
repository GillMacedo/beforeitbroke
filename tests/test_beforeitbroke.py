import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugin" / "scripts"))
import beforeitbroke as bib  # noqa: E402

FAKES = str(ROOT / "tests" / "fakes")
AT = "2026-11-01T14:32:00Z"
UTC = timezone.utc


def run_cli(*args, env=None):
    """Run main in-process with the fake tools first on PATH."""
    out = io.StringIO()
    values = {"PATH": FAKES + os.pathsep + os.environ["PATH"]}
    values.update(env or {})
    with mock.patch.dict(os.environ, values), contextlib.redirect_stdout(out):
        code = bib.main(["--at", AT, *args])
    return code, out.getvalue()


def rows(output):
    return json.loads(output)["changes"]


class ParsingTest(unittest.TestCase):
    def test_time_needs_a_timezone(self):
        with self.assertRaises(ValueError):
            bib.parse_time("2026-11-01T14:32:00")

    def test_times_convert_to_utc(self):
        self.assertEqual(bib.parse_time("2026-11-01T11:32:00-03:00"), datetime(2026, 11, 1, 14, 32, tzinfo=UTC))
        self.assertEqual(bib.parse_time("2026-11-01T14:32:00Z"), datetime(2026, 11, 1, 14, 32, tzinfo=UTC))

    def test_durations(self):
        self.assertEqual(bib.parse_duration("90s"), timedelta(seconds=90))
        self.assertEqual(bib.parse_duration("2h"), timedelta(hours=2))
        self.assertEqual(bib.parse_duration("1d"), timedelta(days=1))
        with self.assertRaises(ValueError):
            bib.parse_duration("soon")

    def test_clean_removes_line_breaks_and_escapes(self):
        self.assertEqual(bib.clean("a\nb\x1b[31mc"), "a b [31mc")
        self.assertEqual(len(bib.clean("x" * 500)), bib.TEXT_LIMIT)

    def test_offset(self):
        at = datetime(2026, 11, 1, 14, 32, tzinfo=UTC)
        self.assertEqual(bib.offset(at - timedelta(minutes=11), at), "-11m")
        self.assertEqual(bib.offset(at + timedelta(seconds=30), at), "+30s")
        self.assertEqual(bib.offset(at - timedelta(minutes=125), at), "-2h05m")


class PodSpecDiffTest(unittest.TestCase):
    def test_image_change(self):
        old = {"containers": [{"name": "app", "image": "a:1"}]}
        new = {"containers": [{"name": "app", "image": "a:2"}]}
        self.assertEqual(bib.pod_spec_diff(old, new), "app: image a:1 -> a:2")

    def test_env_change_names_the_variable_and_hides_values(self):
        old = {"containers": [{"name": "app", "env": [{"name": "KEY", "value": "old-value"}]}]}
        new = {"containers": [{"name": "app", "env": [{"name": "KEY", "value": "new-value"}]}]}
        result = bib.pod_spec_diff(old, new)
        self.assertIn("env changed: KEY", result)
        self.assertNotIn("old-value", result)
        self.assertNotIn("new-value", result)

    def test_added_removed_resources_and_args(self):
        old = {"containers": [{"name": "a", "image": "x", "resources": {"limits": {"cpu": "1"}}, "args": ["-v"]}, {"name": "gone"}]}
        new = {"containers": [{"name": "a", "image": "x", "resources": {"limits": {"cpu": "2"}}, "args": []}, {"name": "new", "image": "y"}]}
        result = bib.pod_spec_diff(old, new)
        for expected in ("a: resources changed", "a: command or args changed", "gone: removed", "new: added (y)"):
            self.assertIn(expected, result)


class GitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = self.temp.name
        self.git("init", "-q", "-b", "main")
        self.commit("old.txt", "Too old", "2026-11-01T10:00:00Z")
        self.commit("pay.txt", "Lower payment timeout", "2026-11-01T14:10:00Z")
        self.commit("late.txt", "Too late", "2026-11-01T16:00:00Z")

    def git(self, *args, date="2026-11-01T14:10:00Z"):
        env = dict(os.environ, GIT_AUTHOR_NAME="Demo", GIT_AUTHOR_EMAIL="demo@example.com", GIT_COMMITTER_NAME="Demo",
                   GIT_COMMITTER_EMAIL="demo@example.com", GIT_COMMITTER_DATE=date, GIT_AUTHOR_DATE=date, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
        subprocess.run(["git", "-C", self.repo, *args], check=True, env=env, capture_output=True)

    def commit(self, name, subject, date):
        Path(self.repo, name).write_text(subject)
        self.git("add", name, date=date)
        self.git("commit", "-q", "-m", subject, date=date)

    def test_only_commits_in_the_window(self):
        code, output = run_cli("--repo", self.repo, "--format", "json")
        self.assertEqual(code, 0)
        found = rows(output)
        self.assertEqual([row["kind"] for row in found], ["commit"])
        self.assertIn("Lower payment timeout (Demo) [pay.txt]", found[0]["summary"])
        self.assertEqual(found[0]["offset"], "-22m")

    def test_tag_in_window(self):
        self.git("tag", "-a", "v1", "-m", "v1", date="2026-11-01T14:20:00Z")
        _, output = run_cli("--repo", self.repo, "--format", "json")
        self.assertIn(("tag", "v1"), [(row["kind"], row["summary"]) for row in rows(output)])

    def test_not_a_repo_fails_visibly(self):
        with tempfile.TemporaryDirectory() as empty:
            code, output = run_cli("--repo", empty)
        self.assertEqual(code, bib.EXIT_PARTIAL)
        self.assertIn("FAILED", output)


class GitHubTest(unittest.TestCase):
    def test_deployment_release_and_merged_pr(self):
        code, output = run_cli("--github", "demo/shop", "--format", "json")
        self.assertEqual(code, 0)
        found = {row["kind"]: row for row in rows(output)}
        self.assertEqual(sorted(found), ["deployment", "merged-pr", "release"])
        self.assertEqual(found["deployment"]["summary"], "production: v1.9.0 (success)")
        self.assertIn("#42 Lower payment timeout", found["merged-pr"]["summary"])
        self.assertNotIn("Closed without merging", output)

    def test_only_reads(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp, "log")
            run_cli("--github", "demo/shop", env={"FAKE_LOG": str(log)})
            calls = log.read_text().splitlines()
        self.assertTrue(calls)
        for call in calls:
            self.assertTrue(call.startswith("gh api repos/demo/shop/"), call)
            self.assertNotRegex(call, r"-X|--method|-f |--field|--input")

    def test_full_page_inside_the_window_warns(self):
        _, output = run_cli("--github", "demo/shop", env={"FAKE_FULL_PAGE": "deployments"})
        self.assertIn("deployments: read only the newest 100", output)

    def test_full_page_reaching_past_the_window_doesnt_warn(self):
        _, output = run_cli("--github", "demo/shop", "--before", "10m", env={"FAKE_FULL_PAGE": "deployments"})
        self.assertNotIn("read only the newest", output)


class KubernetesTest(unittest.TestCase):
    def changes(self, *args, env=None):
        code, output = run_cli("--kube", "--format", "json", *args, env=env)
        return code, json.loads(output)

    def test_rollout_compares_with_the_previous_revision(self):
        _, report = self.changes()
        rollout = next(row for row in report["changes"] if row["summary"].startswith("Deployment shop/checkout"))
        self.assertEqual(rollout["kind"], "rollout")
        self.assertIn("image registry.example.com/checkout:1.8.0 -> registry.example.com/checkout:1.9.0", rollout["summary"])
        self.assertIn("env changed: PAYMENT_TIMEOUT (values hidden)", rollout["summary"])
        self.assertEqual(rollout["ref"], "revision 2")

    def test_deployment_update_with_no_new_replicaset_is_flagged(self):
        _, report = self.changes()
        activity = [row for row in report["changes"] if row["kind"] == "rollout-activity"]
        self.assertEqual(len(activity), 1)
        self.assertIn("shop/search", activity[0]["summary"])
        self.assertIn("possibly a restart", activity[0]["summary"])

    def test_rollback_uses_the_deployment_time_and_compares_revisions(self):
        _, report = self.changes()
        billing = [row for row in report["changes"] if "shop/billing" in row["summary"]]
        rollback = next(row for row in billing if row["kind"] == "rollback")
        self.assertTrue(rollback["time"].startswith("2026-11-01T14:25"))
        self.assertIn("rolled back to an earlier revision: app: image registry.example.com/billing:1.1.0 -> registry.example.com/billing:1.0.0", rollback["summary"])
        rollout = next(row for row in billing if row["kind"] == "rollout")
        self.assertIn("image registry.example.com/billing:1.0.0 -> registry.example.com/billing:1.1.0", rollout["summary"])

    def test_statefulset_rollout(self):
        _, report = self.changes()
        self.assertTrue(any("StatefulSet shop/cache: redis: image redis:7.2 -> redis:7.4" in row["summary"] for row in report["changes"]))

    def test_configmap_write_time_without_its_values(self):
        code, output = run_cli("--kube")
        self.assertIn("shop/checkout-config written by kubectl-edit (values hidden)", output)
        self.assertNotIn("made-up-configmap-value", output)
        self.assertNotIn("kube-root-ca.crt", output)
        self.assertNotIn("made-up-new-value", output)

    def test_warnings_are_grouped_and_flattened(self):
        _, report = self.changes()
        warnings = [row for row in report["changes"] if row["kind"] == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("BackOff x7", warnings[0]["summary"])
        self.assertNotIn("\n", warnings[0]["summary"])
        self.assertTrue(warnings[0]["time"].startswith("2026-11-01T14:31"))

    def test_never_reads_secrets_or_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp, "log")
            run_cli("--kube", env={"FAKE_LOG": str(log)})
            calls = log.read_text().splitlines()
        for call in calls:
            self.assertRegex(call, r"^kubectl (config current-context|get [a-z]+ -o json)")
            self.assertNotIn("secret", call)

    def test_context_and_namespaces_are_passed(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp, "log")
            run_cli("--kube", "--context", "stage", "--namespace", "a", "--namespace", "b", env={"FAKE_LOG": str(log)})
            calls = log.read_text().splitlines()
        self.assertIn("kubectl --context stage get events -o json -n a", calls)
        self.assertIn("kubectl --context stage get events -o json -n b", calls)
        self.assertFalse(any("config current-context" in call or " -A" in call for call in calls))

    def test_forbidden_resource_is_partial_not_silent(self):
        code, output = run_cli("--kube", env={"FAKE_DENY": "events"})
        self.assertEqual(code, bib.EXIT_PARTIAL)
        self.assertIn("PARTIAL", output)
        self.assertIn("Couldn't read events", output)
        self.assertIn("checkout", output)


class OutputTest(unittest.TestCase):
    def test_missing_tool_doesnt_lose_other_sources(self):
        real_which = bib.shutil.which
        with mock.patch.object(bib.shutil, "which", lambda name: None if name == "kubectl" else real_which(name)):
            code, output = run_cli("--kube", "--github", "demo/shop")
        self.assertEqual(code, bib.EXIT_PARTIAL)
        self.assertIn("kubectl isn't installed", output)
        self.assertIn("Lower payment timeout", output)

    def test_timeline_is_sorted_and_ends_with_the_caution(self):
        _, output = run_cli("--kube", "--github", "demo/shop")
        times = re.findall(r"^\s+[+-]\S+\s+(\d\d:\d\d:\d\d)", output, re.M)
        self.assertGreater(len(times), 5)
        self.assertEqual(times, sorted(times))
        self.assertIn("not proof of the cause", output)

    def test_max_keeps_the_closest_changes(self):
        _, output = run_cli("--kube", "--github", "demo/shop", "--max", "2", "--format", "json")
        report = json.loads(output)
        self.assertEqual(len(report["changes"]), 2)
        self.assertGreater(report["left_out"], 0)
        self.assertTrue(all(row["time"] >= "2026-11-01T14:21" for row in report["changes"]))

    def test_markdown_is_a_table_and_escapes_pipes(self):
        change = bib.Change(datetime(2026, 11, 1, 14, 0, tzinfo=UTC), "s", "commit", "a|b")
        at = datetime(2026, 11, 1, 14, 32, tzinfo=UTC)
        result = bib.render_markdown([bib.Coverage("s")], at, at, at, [change], [change])
        self.assertIn("| -32m | 14:00:00 | s | commit | a\\|b |", result)

    def test_no_source_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
            bib.main(["--at", AT])
        self.assertEqual(caught.exception.code, bib.EXIT_USAGE)

    def test_time_without_timezone_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
            bib.main(["--at", "2026-11-01T14:32:00", "--kube"])
        self.assertEqual(caught.exception.code, bib.EXIT_USAGE)


class ProjectTest(unittest.TestCase):
    def test_script_has_no_network_or_third_party_imports(self):
        source = (ROOT / "plugin" / "scripts" / "beforeitbroke.py").read_text()
        imported = set(re.findall(r"^(?:from|import) (\w+)", source, re.M))
        self.assertLessEqual(imported, {"argparse", "json", "re", "shutil", "subprocess", "sys", "dataclasses", "datetime"})
        self.assertNotIn("shell=True", source)

    def test_skill_frontmatter(self):
        text = (ROOT / "plugin" / "skills" / "finding-recent-changes" / "SKILL.md").read_text()
        name = re.search(r"^name: (.+)$", text, re.M).group(1)
        description = re.search(r"^description: (.+)$", text, re.M).group(1)
        self.assertRegex(name, r"^[a-z0-9-]{1,64}$")
        self.assertLessEqual(len(description), 1024)
        self.assertLess(len(text.splitlines()), 500)

    def test_evals_are_valid(self):
        evals = json.loads((ROOT / "evals" / "evals.json").read_text())
        self.assertGreaterEqual(len(evals), 3)


if __name__ == "__main__":
    unittest.main()
