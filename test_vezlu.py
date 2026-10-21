"""Behavioral checks for the local artifact store and its public interfaces."""

import json
import os
import asyncio
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest

from vezlu import Error, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name).resolve() / "store"
        self.store = Store(self.path)
        self.root = self.store.create("launch")["head"]

    def tearDown(self):
        self.temp.cleanup()

    def head(self, branch="main"):
        return self.store.read("launch", branch)["head"]

    def write(self, changes, branch="main"):
        return self.store.write("launch", branch, changes, self.head(branch))["head"]

    def fork(self, name, source="main"):
        return self.store.branch("launch", name, source, self.head(source))

    def merge(self, paths, source="other", target="main", **guards):
        return self.store.merge("launch", source, target, paths,
                                guards.get("source_head", self.head(source)),
                                guards.get("target_head", self.head(target)))

    def test_branch_isolation_history_and_restart(self):
        base = self.write({"brief.md": "Base\n"})
        self.fork("other")
        edited = self.write({"brief.md": "Alternative\n"}, "other")
        self.assertEqual(self.store.read("launch", "main")["artifacts"], {"brief.md": "Base\n"})
        self.assertEqual(self.store.read("launch", "other", base)["artifacts"], {"brief.md": "Base\n"})
        self.assertEqual(self.store.projects(), ["launch"])
        self.assertEqual(len(self.store.branches("launch")), 2)
        self.store = Store(self.path)
        self.assertEqual(self.head("other"), edited)
        self.assertEqual(len(self.store.history("launch", "other")), 3)

    def test_independent_artifacts_combine(self):
        self.write({"a": "base", "b": "base"})
        self.fork("other")
        self.write({"a": "target"})
        self.write({"b": "source"}, "other")
        result = self.merge(["b"])
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.read("launch", "main")["artifacts"], {"a": "target", "b": "source"})

    def test_conflict_rolls_back_all_selected_changes(self):
        self.write({"a": "base", "b": "base"})
        self.fork("other")
        self.write({"a": "target"})
        self.write({"a": "source", "b": "source"}, "other")
        before = self.store.read("launch", "main")
        result = self.merge(["b", "a"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["conflicts"]["a"], {"base": "base", "source": "source", "target": "target"})
        self.assertEqual(before, self.store.read("launch", "main"))

    def test_identical_changes_are_compatible(self):
        self.fork("other")
        self.write({"a": "same"})
        self.write({"a": "same"}, "other")
        self.assertTrue(self.merge(["a"])["ok"])

    def test_deletes_are_versioned_and_mergeable(self):
        old = self.write({"a": "base"})
        self.fork("other")
        self.write({"a": None}, "other")
        self.assertTrue(self.merge(["a"])["ok"])
        self.assertEqual(self.store.read("launch", "main")["artifacts"], {})
        self.assertEqual(self.store.read("launch", "main", old)["artifacts"], {"a": "base"})

    def test_delete_modify_and_add_add_conflict(self):
        self.write({"a": "base"})
        self.fork("other")
        self.write({"a": None, "new": "target"})
        self.write({"a": "source", "new": "source"}, "other")
        result = self.merge(["a", "new"])
        self.assertEqual(set(result["conflicts"]), {"a", "new"})

    def test_stale_write_and_branch_refused(self):
        self.write({"a": "first"})
        for action in [lambda: self.store.write("launch", "main", {"a": "lost"}, self.root),
                       lambda: self.store.branch("launch", "other", "main", self.root)]:
            with self.assertRaisesRegex(Error, "stale"):
                action()
        self.assertEqual(self.store.read("launch", "main")["artifacts"], {"a": "first"})

    def test_stale_source_and_target_merge_refused(self):
        self.fork("other")
        self.write({"a": "source"}, "other")
        for guards in [{"source_head": self.root}, {"target_head": "f" * 32}]:
            with self.assertRaisesRegex(Error, "stale"):
                self.merge(["a"], **guards)
        self.assertEqual(self.head(), self.root)

    def test_partial_merge_preserves_unselected_source_delta(self):
        self.fork("other")
        source = self.write({"a": "source", "b": "later"}, "other")
        first = self.merge(["a"])
        self.assertEqual(first["base"], self.root)
        self.assertEqual(self.store.history("launch", "main")[0]["source"], source)
        second = self.merge(["b"])
        self.assertTrue(second["ok"])
        self.assertEqual(self.store.read("launch", "main")["artifacts"], {"a": "source", "b": "later"})

    def test_full_merge_records_source_ancestry(self):
        self.fork("other")
        source = self.write({"a": "first"}, "other")
        self.merge(["a"])
        self.write({"a": "second"}, "other")
        result = self.merge(["a"])
        self.assertEqual(result["base"], source)
        self.assertTrue(result["ok"])

    def test_common_ancestor_follows_actual_fork(self):
        first = self.write({"a": "one"})
        self.fork("other")
        self.write({"b": "two"}, "other")
        self.fork("third", "other")
        self.write({"c": "three"}, "third")
        result = self.merge(["c"], "third")
        self.assertEqual(result["base"], first)

    def test_unrelated_revision_cannot_be_read(self):
        self.store.create("separate")
        other = self.store.read("separate", "main")["head"]
        with self.assertRaisesRegex(Error, "ancestry"):
            self.store.read("launch", "main", other)

    def test_ambiguous_common_ancestor_refused(self):
        self.fork("other")
        a = self.write({"a": "a"})
        b = self.write({"b": "b"}, "other")
        self.fork("saved", "main")
        self.merge(["a"], "saved", "other")
        self.fork("old-b")
        with sqlite3.connect(self.path / "store.sqlite3") as db:
            db.execute("UPDATE branches SET head=? WHERE project=? AND name=?", (b, "launch", "old-b"))
        self.merge(["b"], "old-b")
        with self.assertRaisesRegex(Error, "ambiguous"):
            self.merge(["a"])
        self.assertNotEqual(a, b)

    def test_diff_is_deterministic_and_includes_deletion(self):
        self.write({"a": "before\n", "gone": "gone\n"})
        self.fork("other")
        self.write({"a": "after\n", "gone": None}, "other")
        result = self.store.compare("launch", "main", "other")
        self.assertEqual(list(result["changes"]), ["a", "gone"])
        self.assertIn("-before\n+after\n", result["changes"]["a"]["diff"])
        self.assertIsNone(result["changes"]["gone"]["after"])
        self.assertEqual(result, self.store.compare("launch", "main", "other"))

    def test_invalid_inputs_and_missing_objects(self):
        for name in ["", "../bad", "a b", "x" * 65, None, 3]:
            with self.subTest(name=name), self.assertRaises(Error):
                self.store.create(name)
        for changes in [{}, {"../escape": "x"}, {"a": 3}, {"a": "x" * 65537}, [], {"a": "\ud800"}]:
            with self.subTest(changes=str(changes)[:50]), self.assertRaises(Error):
                self.store.write("launch", "main", changes, self.head())
        for action in [lambda: self.store.create("launch"),
                       lambda: self.store.read("missing", "main"),
                       lambda: self.store.read("launch", "missing"),
                       lambda: self.store.write("launch", "main", {"missing": None}, self.head()),
                       lambda: self.merge([]),
                       lambda: self.store.history("launch", "main", -1)]:
            with self.assertRaises(Error):
                action()

    def test_snapshot_size_is_bounded(self):
        with self.assertRaisesRegex(Error, "snapshot"):
            self.write({f"a{i}": "x" * 65536 for i in range(17)})
        self.assertEqual(self.head(), self.root)

    def test_permissions_and_immutable_database_rows(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.path / "store.sqlite3").stat().st_mode), 0o600)
        with sqlite3.connect(self.path / "store.sqlite3") as db:
            for query in ["DELETE FROM revisions", "UPDATE revisions SET snapshot='{}'"]:
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(query)

    def test_symlink_directory_database_and_parent_refused(self):
        outside = Path(self.temp.name).resolve() / "outside"
        outside.mkdir(mode=0o700)
        link = Path(self.temp.name).resolve() / "link"
        link.symlink_to(outside, target_is_directory=True)
        for path in [link, link / "child"]:
            with self.assertRaisesRegex(Error, "symlink"):
                Store(path)
        target = outside / "precious"
        target.write_text("keep")
        (outside / "store.sqlite3").symlink_to(target)
        with self.assertRaisesRegex(Error, "symlink"):
            Store(outside)
        self.assertEqual(target.read_text(), "keep")

    def test_concurrent_writers_only_one_uses_expected_head(self):
        def attempt(value):
            try:
                Store(self.path).write("launch", "main", {"a": value}, self.root)
                return "written"
            except Error as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, ["first", "second"]))
        self.assertEqual(results.count("written"), 1)
        self.assertEqual(sum("stale" in result for result in results), 1)
        self.assertEqual(len(self.store.history("launch", "main")), 2)

    def test_invalid_multichange_and_hardlink_leave_data_untouched(self):
        with self.assertRaises(Error):
            self.write({"added": "text", "missing": None})
        self.assertEqual(self.head(), self.root)
        os.link(self.path / "store.sqlite3", Path(self.temp.name).resolve() / "copy")
        with self.assertRaisesRegex(Error, "private"):
            self.store.projects()

    def test_existing_unsafe_store_permissions_refused(self):
        self.path.chmod(0o755)
        with self.assertRaisesRegex(Error, "private"):
            Store(self.path)

    def test_unsafe_database_and_sidecar_refused(self):
        database = self.path / "store.sqlite3"
        database.chmod(0o644)
        with self.assertRaisesRegex(Error, "private"):
            Store(self.path)
        database.chmod(0o600)
        (self.path / "store.sqlite3-journal").symlink_to("/tmp/missing-vezlu")
        with self.assertRaisesRegex(Error, "symlink"):
            self.store.projects()


class CLITests(unittest.TestCase):
    def run_cli(self, store, *args):
        return subprocess.run([sys.executable, str(Path(__file__).with_name("vezlu.py")),
                               "--store", str(store), *args], capture_output=True, text=True, timeout=20)

    def test_demo_and_json_dispatch_persist_across_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Path(temp).resolve() / "store"
            result = self.run_cli(store, "demo")
            self.assertEqual(result.returncode, 0, result.stderr)
            demo = json.loads(result.stdout)
            self.assertEqual(demo["text_source"], "fixtures")
            self.assertEqual(set(demo["comparison"]["changes"]), {"brief.md", "channels.md"})
            self.assertNotEqual(demo["alternative_a"], demo["alternative_b"])
            self.assertNotIn(demo["combined"], [demo["alternative_a"], demo["alternative_b"]])
            self.assertEqual(demo["combined"]["brief.md"], demo["alternative_a"]["brief.md"])
            self.assertEqual(demo["combined"]["channels.md"], demo["alternative_b"]["channels.md"])
            again = self.run_cli(store, "call", "read", '{"project":"launch-demo","branch":"combined"}')
            self.assertEqual(json.loads(again.stdout)["artifacts"], demo["combined"])
            repeat = self.run_cli(store, "demo")
            self.assertNotEqual(repeat.returncode, 0)
            self.assertIn("already exists", json.loads(repeat.stdout)["error"])

    def test_cli_errors_are_json_and_do_not_mutate(self):
        with tempfile.TemporaryDirectory() as temp:
            for args in [("call", "create", "[]"), ("call", "create", "{"),
                         ("call", "unknown", "{}"), ("call", "create", '{"project":"../bad"}')]:
                result = self.run_cli(Path(temp).resolve() / "store", *args)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("error", json.loads(result.stdout))
            result = self.run_cli(Path(temp).resolve() / "store", "call", "projects")
            self.assertEqual(json.loads(result.stdout), [])


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_initialize_tools_errors_and_restart(self):
        from mcp import Client, StdioServerParameters
        with tempfile.TemporaryDirectory() as temp:
            parameters = StdioServerParameters(command=sys.executable,
                args=[str(Path(__file__).with_name("vezlu.py")), "--store", str(Path(temp).resolve() / "store"), "serve"])
            async with asyncio.timeout(40):
                async with Client(parameters, mode="legacy") as client:
                    tools = await client.list_tools()
                    self.assertEqual({tool.name for tool in tools.tools}, {
                        "projects", "create", "branches", "branch", "read", "write", "history", "compare", "merge"})

                    async def call(tool, **arguments):
                        result = await client.call_tool(tool, arguments)
                        self.assertFalse(result.is_error, result)
                        return json.loads(result.content[0].text)

                    created = await call("create", project="launch")
                    root = created["head"]
                    await call("branch", project="launch", branch="other", source="main", expected_head=root)
                    source = await call("write", project="launch", branch="other", changes={"a": "source"}, expected_head=root)
                    comparison = await call("compare", project="launch", left="main", right="other")
                    self.assertIn("a", comparison["changes"])
                    merged = await call("merge", project="launch", source="other", target="main", paths=["a"],
                                        expected_source=source["head"], expected_target=root)
                    self.assertTrue(merged["ok"])
                    self.assertEqual((await call("read", project="launch", branch="main"))["artifacts"], {"a": "source"})
                    for tool, args in [("write", {"project": "launch"}),
                                       ("write", {"project":"launch", "branch":"main", "changes":{"a":"lost"}, "expected_head":root}),
                                       ("create", {"project": "../bad"}),
                                       ("not_a_tool", {})]:
                        result = await client.call_tool(tool, args)
                        self.assertTrue(result.is_error, result)
                    self.assertEqual(await call("projects"), ["launch"])
                async with Client(parameters, mode="legacy") as client:
                    result = await client.call_tool("history", {"project": "launch", "branch": "main"})
                    self.assertFalse(result.is_error)
                    history = json.loads(result.content[0].text)
                    self.assertEqual(history[0]["id"], merged["head"])
                    self.assertEqual(len(history), 3)


if __name__ == "__main__":
    unittest.main()
