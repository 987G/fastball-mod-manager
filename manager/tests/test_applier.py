import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import __main__ as app_main
from app import applier, config, mods

_INSTALL = "import sys, pathlib; pathlib.Path(sys.argv[1]).write_text('x'); print('installed')"
_UNINSTALL = "import sys, pathlib; pathlib.Path(sys.argv[1]).unlink(); print('removed')"
_STATUS = ("import sys, pathlib; "
           "print('MARKER: ' + ('modded' if pathlib.Path(sys.argv[1]).exists() else 'stock'))")
_FAIL = ("import sys\n"
         "for i in range(30): print('line', i)\n"
         "sys.exit(3)")


class ApplierTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name) / "with space"
        self.settings = dataclasses.replace(
            config.Settings(), mods=self.tmp / "mods", state=self.tmp / "state")
        self.settings.mods.mkdir(parents=True)
        no_game = mock.patch("app.procs.running_games", return_value=[])
        no_game.start()
        self.addCleanup(no_game.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _mod(self, mod_id: str, install: str = _INSTALL, order: int = 100) -> mods.Mod:
        folder = self.settings.mods / mod_id
        folder.mkdir()
        files = {
            "cwd": "{mod}",
            "install": ["python", "-c", install, "{mod}/marker"],
            "uninstall": ["python", "-c", _UNINSTALL, "{mod}/marker"],
            "status": ["python", "-c", _STATUS, "{mod}/marker"],
            "installed_when": "MARKER: modded",
        }
        return mods.Mod(id=mod_id, name=mod_id, description="", folder=folder, art="",
                        files=files, runtime=None, server=None, profile=None, order=order)

    def _plain_mod(self, mod_id: str) -> mods.Mod:
        return mods.Mod(id=mod_id, name=mod_id, description="",
                        folder=self.settings.mods / mod_id, art="", files=None,
                        runtime={}, server=None, profile=None, order=100)

    def _marker(self, mod: mods.Mod) -> Path:
        return mod.folder / "marker"

    def _journal(self) -> list:
        return json.loads((self.settings.state / "journal.json").read_text(encoding="utf-8"))

    def test_is_installed_reads_status(self):
        mod = self._mod("a")
        self.assertFalse(applier.is_installed(mod, self.settings))
        self._marker(mod).write_text("x")
        self.assertTrue(applier.is_installed(mod, self.settings))

    def test_scripts_run_under_python_exe(self):
        mod = self._mod("a")
        real = applier.procs.python_exe()
        with mock.patch("app.procs.python_exe", return_value=real) as exe:
            self.assertFalse(applier.is_installed(mod, self.settings))
        exe.assert_called()

    def test_failing_status_raises_with_mod_named(self):
        mod = self._mod("a")
        mod.files["status"] = ["python", "-c", "import sys; print('cannot read X.XML'); sys.exit(2)"]
        with self.assertRaises(applier.StepFailed) as caught:
            applier.is_installed(mod, self.settings)
        self.assertEqual(caught.exception.mod_id, "a")
        self.assertIn("cannot read X.XML", caught.exception.tail)
        self.assertEqual(str(caught.exception).splitlines()[0],
                         "mod 'a' status check failed (exit 2): cannot read X.XML"
                         " — see Builds & Logs")

    def test_failing_status_with_matching_output_is_not_installed(self):
        mod = self._mod("a")
        mod.files["status"] = ["python", "-c", "import sys; print('MARKER: modded'); sys.exit(1)"]
        with self.assertRaises(applier.StepFailed):
            applier.is_installed(mod, self.settings)

    def test_plan_stops_on_failing_status(self):
        a, b = self._mod("a"), self._mod("b")
        b.files["status"] = ["python", "-c", "import sys; sys.exit(5)"]
        with self.assertRaises(applier.StepFailed) as caught:
            applier.plan(set(), [a, b], self.settings)
        self.assertEqual(caught.exception.mod_id, "b")
        self.assertIn("exit 5", str(caught.exception).splitlines()[0])

    def test_plan_installs_selected(self):
        a, b, plain = self._mod("a"), self._mod("b"), self._plain_mod("p")
        self._marker(b).write_text("x")
        steps = applier.plan({"a", "b", "p"}, [a, b, plain], self.settings)
        self.assertEqual(steps, [("install", "a")])

    def test_plan_uninstalls_unselected_first(self):
        a, b, c = self._mod("a"), self._mod("b"), self._mod("c")
        self._marker(b).write_text("x")
        self._marker(c).write_text("x")
        steps = applier.plan({"a"}, [a, b, c], self.settings)
        self.assertEqual(steps, [("uninstall", "b"), ("uninstall", "c"), ("install", "a")])

    def test_apply_runs_steps_logs_and_journals(self):
        a, b = self._mod("a"), self._mod("b")
        self._marker(b).write_text("x")
        lines = []
        applier.apply([("uninstall", "b"), ("install", "a")], [a, b], self.settings,
                      lines.append)
        self.assertTrue(self._marker(a).exists())
        self.assertFalse(self._marker(b).exists())
        self.assertIn("removed", lines)
        self.assertIn("installed", lines)
        journal = self._journal()
        self.assertEqual([e["step"] for e in journal], [["uninstall", "b"], ["install", "a"]])
        self.assertEqual([e["status"] for e in journal], ["done", "done"])
        self.assertIn("installed", journal[1]["output"])
        self.assertEqual(applier.pending_from_journal(self.settings), [])

    def test_apply_refuses_while_game_runs(self):
        a = self._mod("a")
        game = {"pid": 4242, "exe": r"C:\Games\MLB The Show 26\mlb.exe"}
        with mock.patch("app.procs.running_games", return_value=[game]):
            with self.assertRaises(applier.GameRunning) as caught:
                applier.apply([("install", "a")], [a], self.settings, lambda line: None)
        self.assertEqual(caught.exception.games, [game])
        self.assertIn("4242", str(caught.exception))
        self.assertIn(r"C:\Games\MLB The Show 26\mlb.exe", str(caught.exception))
        self.assertFalse(self._marker(a).exists())
        self.assertFalse((self.settings.state / "journal.json").exists())

    def test_apply_without_steps_runs_beside_a_game_and_keeps_the_journal(self):
        a = self._mod("a")
        journal = self.settings.state / "journal.json"
        journal.parent.mkdir(parents=True)
        text = json.dumps([{"step": ["install", "a"], "status": "failed", "output": "x"}])
        journal.write_text(text, encoding="utf-8")
        game = {"pid": 4242, "exe": r"C:\scratch\mlb.exe"}
        with mock.patch("app.procs.running_games", return_value=[game]):
            applier.apply([], [a], self.settings, lambda line: None)
        self.assertEqual(journal.read_text(encoding="utf-8"), text)
        self.assertFalse(self._marker(a).exists())

    def test_interrupted_step_is_redone(self):
        a = self._mod("a")
        self.settings.state.mkdir()
        (self.settings.state / "journal.json").write_text(json.dumps(
            [{"step": ["install", "a"], "status": "running", "output": ""}]), encoding="utf-8")
        self.assertEqual(applier.pending_from_journal(self.settings), [("install", "a")])
        steps = applier.plan({"a"}, [a], self.settings)
        self.assertEqual(steps, [("install", "a")])
        applier.apply(steps, [a], self.settings, lambda line: None)
        self.assertTrue(self._marker(a).exists())
        self.assertEqual(applier.pending_from_journal(self.settings), [])

    def _interrupted(self, *steps):
        self.settings.state.mkdir(exist_ok=True)
        (self.settings.state / "journal.json").write_text(json.dumps(
            [{"step": list(step), "status": "running", "output": ""} for step in steps]),
            encoding="utf-8")

    def test_interrupted_install_is_redone_although_status_says_installed(self):
        a = self._mod("a")
        self._marker(a).write_text("x")  # the first file changed: status reads "modded"
        self._interrupted(("install", "a"))
        self.assertTrue(applier.is_installed(a, self.settings))
        self.assertEqual(applier.plan({"a"}, [a], self.settings), [("install", "a")])

    def test_interrupted_uninstall_is_redone_although_status_says_original(self):
        a = self._mod("a")  # no marker: status reads "stock" after the first restore
        self._interrupted(("uninstall", "a"))
        self.assertFalse(applier.is_installed(a, self.settings))
        self.assertEqual(applier.plan(set(), [a], self.settings), [("uninstall", "a")])

    def test_interrupted_step_heads_for_the_current_selection(self):
        a, b = self._mod("a"), self._mod("b")
        self._marker(a).write_text("x")
        self._interrupted(("install", "a"), ("uninstall", "b"))
        self.assertEqual(applier.plan({"b"}, [a, b], self.settings),
                         [("uninstall", "a"), ("install", "b")])

    def test_journal_ids_that_are_no_file_mods_are_ignored(self):
        a, plain = self._mod("a"), self._plain_mod("p")
        self._interrupted(("install", "gone"), ("install", "p"))
        self.assertEqual(applier.plan({"p"}, [a, plain], self.settings), [])
        applier.apply([], [a, plain], self.settings, lambda line: None)
        applier.apply([("install", "a")], [a, plain], self.settings, lambda line: None)
        self.assertEqual(applier.pending_from_journal(self.settings), [])

    def test_forced_step_stays_journaled_until_it_has_run(self):
        a, bad = self._mod("a"), self._mod("bad")
        bad.files["uninstall"] = ["python", "-c", "import sys; sys.exit(4)"]
        self._marker(a).write_text("x")
        self._marker(bad).write_text("x")
        self._interrupted(("install", "a"))
        steps = applier.plan({"a"}, [a, bad], self.settings)
        self.assertEqual(steps, [("uninstall", "bad"), ("install", "a")])
        with self.assertRaises(applier.StepFailed):
            applier.apply(steps, [a, bad], self.settings, lambda line: None)
        self.assertEqual(applier.pending_from_journal(self.settings),
                         [("install", "a"), ("uninstall", "bad")])  # failed: still owed
        self.assertEqual(applier.plan({"a"}, [a, bad], self.settings),
                         [("uninstall", "bad"), ("install", "a")])
        applier.apply([("install", "a")], [a, bad], self.settings, lambda line: None)
        self.assertEqual(applier.pending_from_journal(self.settings), [("uninstall", "bad")])
        self.assertEqual(self._journal()[0]["status"], "failed")  # carried as it was

    def _failed(self, *steps):
        self.settings.state.mkdir(exist_ok=True)
        (self.settings.state / "journal.json").write_text(json.dumps(
            [{"step": list(step), "status": "failed", "output": "boom"} for step in steps]),
            encoding="utf-8")

    def test_failed_install_is_redone_although_status_says_installed(self):
        a = self._mod("a")
        self._marker(a).write_text("x")  # failed part-way: status reads "modded"
        self._failed(("install", "a"))
        self.assertTrue(applier.is_installed(a, self.settings))
        self.assertEqual(applier.plan({"a"}, [a], self.settings), [("install", "a")])

    def test_failed_install_heads_for_uninstall_when_deselected(self):
        a = self._mod("a")  # no marker: status reads "stock"
        self._failed(("install", "a"))
        self.assertFalse(applier.is_installed(a, self.settings))
        self.assertEqual(applier.plan(set(), [a], self.settings), [("uninstall", "a")])

    def test_failed_uninstall_is_redone_although_status_says_original(self):
        a = self._mod("a")
        self._failed(("uninstall", "a"))
        self.assertFalse(applier.is_installed(a, self.settings))
        self.assertEqual(applier.plan(set(), [a], self.settings), [("uninstall", "a")])

    def test_failed_uninstall_heads_for_install_when_selected(self):
        a = self._mod("a")
        self._marker(a).write_text("x")
        self._failed(("uninstall", "a"))
        self.assertEqual(applier.plan({"a"}, [a], self.settings), [("install", "a")])

    def test_forced_step_that_fails_again_is_still_forced(self):
        a = self._mod("a", install="import sys; open(sys.argv[1], 'w').write('x'); sys.exit(5)")
        self._interrupted(("install", "a"))
        steps = applier.plan({"a"}, [a], self.settings)
        self.assertEqual(steps, [("install", "a")])
        with self.assertRaises(applier.StepFailed) as caught:
            applier.apply(steps, [a], self.settings, lambda line: None)
        self.assertEqual(caught.exception.mod_id, "a")
        self.assertTrue(applier.is_installed(a, self.settings))  # looks done, is not
        self.assertEqual(applier.plan({"a"}, [a], self.settings), [("install", "a")])
        self.assertEqual(applier.plan(set(), [a], self.settings), [("uninstall", "a")])

    def test_first_step_that_fails_part_way_is_forced_next_time(self):
        a = self._mod("a", install="import sys; open(sys.argv[1], 'w').write('x'); sys.exit(5)")
        with self.assertRaises(applier.StepFailed):
            applier.apply([("install", "a")], [a], self.settings, lambda line: None)
        self.assertEqual(applier.plan({"a"}, [a], self.settings), [("install", "a")])

    def test_forced_step_that_succeeds_ends_forcing(self):
        a = self._mod("a")
        self._marker(a).write_text("x")
        self._failed(("install", "a"))
        steps = applier.plan({"a"}, [a], self.settings)
        self.assertEqual(steps, [("install", "a")])
        applier.apply(steps, [a], self.settings, lambda line: None)
        self.assertEqual(applier.pending_from_journal(self.settings), [])
        self.assertEqual(applier.plan({"a"}, [a], self.settings), [])  # status-based again
        self.assertEqual(applier.plan(set(), [a], self.settings), [("uninstall", "a")])

    def test_failed_entries_for_mods_that_no_longer_exist_are_ignored(self):
        a, plain = self._mod("a"), self._plain_mod("p")
        self._failed(("install", "gone"), ("uninstall", "p"))
        self.assertEqual(applier.plan({"p"}, [a, plain], self.settings), [])
        applier.apply([("install", "a")], [a, plain], self.settings, lambda line: None)
        self.assertEqual(applier.pending_from_journal(self.settings), [])

    def test_pending_from_missing_or_bad_journal_is_empty(self):
        self.assertEqual(applier.pending_from_journal(self.settings), [])
        self.settings.state.mkdir()
        (self.settings.state / "journal.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(applier.pending_from_journal(self.settings), [])

    def test_apply_step_that_cannot_start_fails_cleanly(self):
        a = self._mod("a")
        a.files["cwd"] = "{mod}/missing"
        with self.assertRaises(applier.StepFailed) as caught:
            applier.apply([("install", "a")], [a], self.settings, lambda line: None)
        self.assertEqual(caught.exception.mod_id, "a")
        self.assertTrue(caught.exception.tail)
        journal = self._journal()
        self.assertEqual([e["status"] for e in journal], ["failed"])
        self.assertEqual(journal[0]["output"], caught.exception.tail)
        self.assertEqual(applier.pending_from_journal(self.settings), [("install", "a")])

    def test_plan_status_that_cannot_start_names_mod(self):
        a, b = self._mod("a"), self._mod("b")
        b.files["cwd"] = "{mod}/missing"
        with self.assertRaises(applier.StepFailed) as caught:
            applier.plan({"a", "b"}, [a, b], self.settings)
        self.assertEqual(caught.exception.mod_id, "b")
        self.assertIn("'b'", str(caught.exception))

    def test_failed_step_raises_with_tail(self):
        bad, good = self._mod("bad", install=_FAIL), self._mod("good")
        with self.assertRaises(applier.StepFailed) as caught:
            applier.apply([("install", "bad"), ("install", "good")], [bad, good],
                          self.settings, lambda line: None)
        err = caught.exception
        self.assertEqual(err.mod_id, "bad")
        tail = err.tail.splitlines()
        self.assertEqual(len(tail), 20)
        self.assertEqual(tail[0], "line 10")
        self.assertEqual(tail[-1], "line 29")
        self.assertIn("bad", str(err))
        self.assertEqual(str(err).splitlines()[0],
                         "mod 'bad' failed: line 29 — see Builds & Logs")
        self.assertFalse(self._marker(good).exists())
        journal = self._journal()
        self.assertEqual(journal, [{"step": ["install", "bad"], "status": "failed",
                                    "output": journal[0]["output"]}])
        self.assertIn("line 29", journal[0]["output"])


class RealManifestTests(unittest.TestCase):
    """The real file mods' scripts start under the manager's Python (read-only: status)."""

    def test_real_status_script_runs(self):
        settings = config.load(app_main.SETTINGS)  # this manager's own settings
        if not (Path(settings.mods) / "rtts-quirks" / "mod.json").is_file():
            self.skipTest("real mods not present")
        for mod in mods.load_all(settings):
            if not mod.files:
                continue
            with self.subTest(mod=mod.id):
                code, output = applier._run(mod, "status", settings)
                self.assertEqual(code, 0, output)


if __name__ == "__main__":
    unittest.main()
